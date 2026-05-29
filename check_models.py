"""Тестировщик доступности LLM-моделей поверх `opencode serve`.

Для каждой модели шлёт лёгкий ping и выдаёт чёткий вердикт:
  available — модель ответила (session.idle);
  error     — ошибка провайдера (401/403/Forbidden subscription, 429 и т.п.);
  timeout   — модель не ответила за отведённое время (часто скрытые ретраи 429).

Поднимает ОДИН `opencode serve` и гоняет модели последовательно — так session_id
каждой модели однозначно сопоставляется с её строками в файловом логе opencode,
откуда вытаскивается настоящая причина ошибки.

Двухэтапный таймаут: фаза 1 быстрая (--timeout), таймаутнувшие модели
повторяются с большим --retry-timeout (если не задан --no-retry).

По умолчанию проверяются ТОЛЬКО бесплатные модели (cost.input=0 и cost.output=0
по данным сервера). Флаг --pay-models добавляет к ним платные. Явно перечисленные
модели (--models / --models-file) проверяются как есть, без фильтра цены.

Примеры:
    # все бесплатные модели всех провайдеров (дефолт)
    python check_models.py

    # бесплатные модели одного провайдера
    python check_models.py --provider opencode

    # все модели провайдера, включая платные
    python check_models.py --provider opencode --pay-models

    # конкретные модели (фильтр цены не применяется)
    python check_models.py --models zai-coding-plan/glm-5.1 --models opencode/glm-5.1

    # список из файла
    python check_models.py --models-file models.txt
"""

import argparse
import datetime as _dt
import json
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from agent import (
    PROJECT_ROOT,
    DEFAULT_BASE_PORT,
    DEFAULT_AGENT,
    ensure_server_running,
    probe_session,
    _client,
    _sanitize,
    _fmt_secs,
)

PING_PROMPT = "Ты тут? Ответь одним словом."
AVAILABILITY_ROOT = PROJECT_ROOT / "data" / "availability"
FREE_RULES_PATH = PROJECT_ROOT / "free_models.json"

# code из probe_session → человекочитаемый статус.
_STATUS = {0: "available", 1: "timeout", 2: "error"}


@dataclass
class ModelRef:
    provider: str
    model: str
    free_status: str = "unknown"   # "free" | "paid" | "unknown" (по free_models.json)

    @property
    def key(self) -> str:
        return f"{self.provider}/{self.model}"

    @property
    def free(self) -> bool:
        return self.free_status == "free"


@dataclass
class CheckResult:
    ref: ModelRef
    code: int                 # 0 готово / 1 таймаут / 2 ошибка (как probe_session)
    status: str               # available | timeout | error
    reason: str | None        # причина для code != 0, иначе None
    elapsed: float
    attempt_timeout: float    # с каким таймаутом получен финальный вердикт
    retried: bool
    log_path: str             # путь к per-model логу (относительно run_dir)


# --- получение списка моделей ---------------------------------------------

def load_free_rules() -> dict[str, dict]:
    """Карта стратегий бесплатности по провайдеру из free_models.json рядом со
    скриптом. Если файла нет — пустая карта (всё → unknown)."""
    if not FREE_RULES_PATH.exists():
        return {}
    data = json.loads(FREE_RULES_PATH.read_text(encoding="utf-8")) or {}
    return data.get("providers", {})


def _cost_is_zero(model) -> bool:
    """Цена за вход и выход = 0 (поле cost из API)."""
    cost = getattr(model, "cost", None)
    cd = cost.model_dump() if hasattr(cost, "model_dump") else cost
    if not isinstance(cd, dict):
        return False
    return (cd.get("input") or 0) == 0 and (cd.get("output") or 0) == 0


def classify_model(provider: str, model_id: str, model, rules: dict) -> str:
    """'free' | 'paid' | 'unknown' по стратегии провайдера из rules.

    cost-zero — бесплатна при нулевой цене (у провайдеров с реальным прайсом);
    name-free — бесплатна, если 'free' есть в id/имени (openrouter `:free`);
    list      — бесплатна, только если model_id в перечне rule['models'].
    Провайдер без правила → unknown (не врём про подписочные нули-заглушки)."""
    rule = rules.get(provider)
    if not rule:
        return "unknown"
    strat = rule.get("strategy")
    if strat == "cost-zero":
        return "free" if _cost_is_zero(model) else "paid"
    if strat == "name-free":
        hay = f"{model_id} {getattr(model, 'name', '') or ''}".lower()
        return "free" if "free" in hay else "paid"
    if strat == "list":
        return "free" if model_id in set(rule.get("models", [])) else "paid"
    return "unknown"


def fetch_all_models(port: int) -> list[ModelRef]:
    """Все пары provider/model с работающего сервера (GET /config/providers).

    `app.providers().providers[]` → у каждого `.id` (= providerID) и `.models`
    (dict, ключ = modelID). Бесплатность классифицируем по free_models.json."""
    rules = load_free_rules()
    resp = _client(port).app.providers()
    refs: list[ModelRef] = []
    for prov in resp.providers:
        for model_id, model in (prov.models or {}).items():
            refs.append(ModelRef(
                provider=prov.id, model=model_id,
                free_status=classify_model(prov.id, model_id, model, rules),
            ))
    return refs


def parse_models_arg(values: list[str]) -> list[ModelRef]:
    """'provider/model' → ModelRef (split по первому '/')."""
    refs: list[ModelRef] = []
    for raw in values:
        item = raw.strip()
        if not item:
            continue
        if "/" not in item:
            raise SystemExit(f"Неверный формат модели (нужно provider/model): {raw!r}")
        provider, model = item.split("/", 1)
        refs.append(ModelRef(provider=provider.strip(), model=model.strip()))
    return refs


def load_models_file(path: Path) -> list[ModelRef]:
    """Строки 'provider/model'; пустые строки и '#'-комментарии игнорируются."""
    lines: list[str] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    return parse_models_arg(lines)


def _dedup(refs: list[ModelRef]) -> list[ModelRef]:
    """Убрать дубли с сохранением порядка."""
    seen: set[str] = set()
    out: list[ModelRef] = []
    for ref in refs:
        if ref.key not in seen:
            seen.add(ref.key)
            out.append(ref)
    return out


def resolve_model_list(args: argparse.Namespace, port: int) -> tuple[list[ModelRef], str]:
    """Гибрид: явный список (--models/--models-file) или весь список с сервера.
    Возвращает (модели, источник). Опционально фильтрует по --provider."""
    if args.models_file:
        refs = load_models_file(args.models_file)
        source = "models-file"
    elif args.models:
        refs = parse_models_arg(args.models)
        source = "models-flag"
    else:
        refs = fetch_all_models(port)
        source = "providers-api"

    if args.provider:
        refs = [r for r in refs if r.provider == args.provider]

    # По умолчанию проверяем только бесплатные модели (free_status == "free" по
    # free_models.json); paid и unknown отсеиваются. --pay-models снимает фильтр.
    # Применяется лишь к списку из API; явный список (--models / --models-file)
    # пользователь выбрал сам — не фильтруем.
    if source == "providers-api" and not args.pay_models:
        refs = [r for r in refs if r.free]
        source += "+free-only"

    return _dedup(refs), source


# --- одна проверка ----------------------------------------------------------

def check_one(ref: ModelRef, prompt: str, agent: str, timeout: float, port: int,
              log_dir: Path, run_dir: Path) -> CheckResult:
    """Пингует одну модель через probe_session, пишет подробности в per-model лог."""
    log_path = log_dir / f"{_sanitize(ref.key)}.log"
    start = time.monotonic()
    lock = threading.Lock()
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"=== {ref.key} | timeout={timeout:.0f}s ===\n")

        def write(msg: str) -> None:
            with lock:
                log.write(msg)
                log.flush()

        code, reason = probe_session(
            task=prompt, model=ref.model, provider=ref.provider,
            agent=agent, timeout=timeout, port=port, write=write,
        )

    return CheckResult(
        ref=ref,
        code=code,
        status=_STATUS.get(code, f"code-{code}"),
        reason=reason,
        elapsed=time.monotonic() - start,
        attempt_timeout=timeout,
        retried=False,
        log_path=str(log_path.relative_to(run_dir)),
    )


_STATUS_ICON = {"available": "✅", "timeout": "⏱", "error": "❌"}


def _emit(label: str, res: CheckResult) -> None:
    """Краткий статус одной модели в stdout по ходу прогона."""
    icon = _STATUS_ICON.get(res.status, "?")
    tail = f" — {res.reason}" if res.reason else ""
    print(f"[{label}] {res.ref.key} {icon} {res.status} "
          f"({_fmt_secs(res.elapsed)}){tail}", flush=True)


def _run_phase(refs: list[ModelRef], prompt: str, agent: str, timeout: float,
               port: int, log_dir: Path, run_dir: Path, label: str) -> dict[str, CheckResult]:
    """Последовательный прогон списка моделей; печатает статус по мере."""
    results: dict[str, CheckResult] = {}
    total = len(refs)
    for i, ref in enumerate(refs, 1):
        res = check_one(ref, prompt, agent, timeout, port, log_dir, run_dir)
        _emit(f"{label} {i}/{total}", res)
        results[ref.key] = res
    return results


def check_models(refs: list[ModelRef], prompt: str, agent: str, base_timeout: float,
                 retry_timeout: float, do_retry: bool, port: int,
                 log_dir: Path, run_dir: Path) -> list[CheckResult]:
    """Фаза 1 на base_timeout; таймаутнувшие (code 1) — фаза 2 на retry_timeout.
    Финальный вердикт модели — лучший из попыток (меньший code побеждает)."""
    results = _run_phase(refs, prompt, agent, base_timeout, port, log_dir, run_dir, "фаза1")

    if do_retry:
        retry_refs = [r for r in refs if results[r.key].code == 1]
        if retry_refs:
            print(f"--- фаза 2: ретрай {len(retry_refs)} таймаутнувших "
                  f"(timeout={retry_timeout:.0f}с) ---", flush=True)
            retry = _run_phase(retry_refs, prompt, agent, retry_timeout, port,
                               log_dir, run_dir, "фаза2")
            for key, res in retry.items():
                res.retried = True
                # Лучший из двух: меньший code (0 < 1 < 2) предпочтительнее.
                if res.code <= results[key].code:
                    results[key] = res
                else:
                    results[key].retried = True

    return [results[r.key] for r in refs]


# --- вывод ------------------------------------------------------------------

def print_table(results: list[CheckResult]) -> None:
    """Финальная таблица в stdout: provider/model | статус | причина | время."""
    if not results:
        print("(нет моделей для проверки)")
        return
    key_w = max(len("provider/model"), max(len(r.ref.key) for r in results))
    print("--- доступность моделей ---")
    print(f"{'provider/model':<{key_w}}  {'статус':<10} {'время':>8}  причина")
    for r in results:
        reason = r.reason or ""
        if len(reason) > 80:
            reason = reason[:77] + "..."
        print(f"{r.ref.key:<{key_w}}  {r.status:<10} "
              f"{_fmt_secs(r.elapsed):>8}  {reason}")


def write_availability_json(results: list[CheckResult], path: Path, meta: dict) -> None:
    counts = {"available": 0, "timeout": 0, "error": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    report = {
        **meta,
        "summary": {**counts, "total": len(results)},
        "results": [
            {
                "provider": r.ref.provider,
                "model": r.ref.model,
                "free_status": r.ref.free_status,
                "status": r.status,
                "code": r.code,
                "reason": r.reason,
                "elapsed": r.elapsed,
                "attempt_timeout": r.attempt_timeout,
                "retried": r.retried,
                "log": r.log_path,
            }
            for r in results
        ],
    }
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Тестировщик доступности LLM-моделей (opencode): "
                    "available / error / timeout по каждой модели",
    )
    parser.add_argument("--models", action="append", default=[],
                        help="Модель 'provider/model' (можно повторять)")
    parser.add_argument("--models-file", type=Path,
                        help="Файл со списком 'provider/model' (по строке, # комментарии)")
    parser.add_argument("--provider", help="Фильтр: проверять только этого провайдера")
    parser.add_argument("--pay-models", action="store_true",
                        help="Добавить платные модели к бесплатным (по умолчанию — "
                             "только бесплатные: cost.input=0 и cost.output=0)")
    parser.add_argument("-a", "--agent", default=DEFAULT_AGENT,
                        help=f"Имя агента (default: {DEFAULT_AGENT})")
    parser.add_argument("--prompt", default=PING_PROMPT,
                        help="Пробный запрос (default: лёгкий ping)")
    parser.add_argument("--timeout", type=float, default=20.0,
                        help="Таймаут фазы 1 на одну модель, с (default: 20)")
    parser.add_argument("--retry-timeout", type=float, default=120.0,
                        help="Таймаут фазы 2 (ретрай таймаутнувших), с (default: 120)")
    parser.add_argument("--no-retry", action="store_true",
                        help="Не делать вторую фазу ретрая")
    parser.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT,
                        help=f"Порт opencode serve (default: {DEFAULT_BASE_PORT})")
    args = parser.parse_args()

    started_at = _dt.datetime.now()
    run_dir = AVAILABILITY_ROOT / started_at.strftime("%Y%m%d-%H%M%S")
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    run_dir_rel = run_dir.relative_to(PROJECT_ROOT) if run_dir.is_relative_to(PROJECT_ROOT) else run_dir
    print(f"Папка прогона: {run_dir_rel}")
    print(f"Поднимаю opencode serve на :{args.base_port}")

    def status(msg: str) -> None:
        print(f"[server] {msg}", flush=True)

    # Один сервер на весь прогон; гасится через atexit-обработчик agent._stop_servers.
    if not ensure_server_running(run_dir, args.base_port, status):
        print("Не удалось поднять opencode serve — прерываюсь", file=sys.stderr)
        sys.exit(2)

    refs, source = resolve_model_list(args, args.base_port)
    if not refs:
        print("Нет моделей для проверки (проверь --models / --provider).", file=sys.stderr)
        sys.exit(1)

    # В дефолтном free-режиме предупредим, какие провайдеры пропущены как unknown
    # (нет правила в free_models.json) — это то, что предстоит «разобрать».
    if source.startswith("providers-api") and not args.pay_models:
        unknown = sorted({r.provider for r in fetch_all_models(args.base_port)
                          if r.free_status == "unknown"})
        if unknown:
            print(f"⚠ Пропущены провайдеры без правила в free_models.json "
                  f"(unknown): {', '.join(unknown)}")
            print("  Добавь им strategy в free_models.json или используй "
                  "--provider <id> / --pay-models.")

    print(f"Моделей к проверке: {len(refs)} (источник: {source})")
    print("--- старт ---")

    results = check_models(
        refs=refs, prompt=args.prompt, agent=args.agent,
        base_timeout=args.timeout, retry_timeout=args.retry_timeout,
        do_retry=not args.no_retry, port=args.base_port,
        log_dir=log_dir, run_dir=run_dir,
    )

    print_table(results)
    counts = {"available": 0, "timeout": 0, "error": 0}
    for r in results:
        counts[r.status] = counts.get(r.status, 0) + 1
    print("--- сводка ---")
    print(f"{counts['available']} доступно / {counts['timeout']} таймаут / "
          f"{counts['error']} ошибка (из {len(results)})")

    meta = {
        "started_at": started_at.isoformat(),
        "finished_at": _dt.datetime.now().isoformat(),
        "agent": args.agent,
        "base_port": args.base_port,
        "prompt": args.prompt,
        "timeout": args.timeout,
        "retry_timeout": args.retry_timeout,
        "retry_enabled": not args.no_retry,
        "source": source,
    }
    json_path = run_dir / "availability.json"
    write_availability_json(results, json_path, meta)
    json_rel = json_path.relative_to(PROJECT_ROOT) if json_path.is_relative_to(PROJECT_ROOT) else json_path
    print(f"Отчёт: {json_rel}")


if __name__ == "__main__":
    main()

"""Тестировщик доступности LLM-моделей поверх `opencode serve`.

Для каждой модели шлёт лёгкий ping и выдаёт чёткий вердикт:
  available — модель ответила (session.idle);
  error     — ошибка провайдера (401/403/Forbidden subscription, 429 и т.п.);
  timeout   — модель не ответила за отведённое время (часто скрытые ретраи 429).

Список моделей берётся через `opencode models` без запуска serve. Для реальной
проверки доступности поднимает ОДИН `opencode serve` и гоняет модели
последовательно — так session_id каждой модели однозначно сопоставляется с её
строками в файловом логе opencode, откуда вытаскивается настоящая причина ошибки.

Двухэтапный таймаут: фаза 1 быстрая (--timeout), таймаутнувшие модели
повторяются с большим --retry-timeout (если не задан --no-retry).

По умолчанию проверяются ТОЛЬКО бесплатные модели (cost.input=0 и cost.output=0
по данным сервера). Флаг --pay-models добавляет к ним платные. Явно перечисленные
модели (--models / --models-file) проверяются как есть, без фильтра цены.
Модели из project denylist-а пропускаются, если не задан --include-excluded.

Примеры:
    # все бесплатные модели всех провайдеров (дефолт)
    python check_models.py

    # поиск по effective catalog opencode без serve/ping
    python check_models.py --list-models --pay-models --query gemma

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
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from opencode_runtime import (
    PROJECT_ROOT,
    DEFAULT_BASE_PORT,
    DEFAULT_AGENT,
    RUN_CODES,
    ensure_server_running,
    install_shutdown_handlers,
    probe_session,
    rel_to_root,
    sanitize_name,
    status_printer,
    summary_counts,
    summary_line,
    fmt_secs,
)
from db import active_exclusions_map, model_key, session, split_model_ref
from utils import json_loads_or
from model_catalog import (
    ModelCatalogEntry,
    ModelCatalogError,
    load_opencode_models,
)

PING_PROMPT = "Ты тут? Ответь одним словом."
AVAILABILITY_ROOT = PROJECT_ROOT / "data" / "availability"

# code из probe_session → человекочитаемый статус. Набор кодов берём из единой
# таксономии RUN_CODES; здесь только своя метка для code=0 («available» вместо
# «ok»). code=3 — лимит провайдера (probe_session ретраит HTTP 429 и отдаёт его
# при исчерпании попыток).
_STATUS = {code: key for code, (key, _label) in RUN_CODES.items()} | {0: "available"}
_MODEL_SEARCH_SEPARATOR_RE = re.compile(r"[^0-9a-zа-яё]+")


def tally_statuses(results: "list[CheckResult]") -> dict[str, int]:
    """Сводка по статусам: `{"available": n, "timeout": n, ...}`."""
    counts = {label: 0 for label in _STATUS.values()}
    for r in results:
        # Статус вне таксономии («code-N» из check_one) не должен ронять сводку.
        counts[r.status] = counts.get(r.status, 0) + 1
    return counts


@dataclass
class ModelRef:
    provider: str
    model: str
    free_status: str = "unknown"   # "free" | "paid" | "unknown" (по free_rules)
    name: str | None = None

    @property
    def key(self) -> str:
        return model_key(self.provider, self.model)

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
    """Карта стратегий бесплатности по провайдеру из таблицы `free_rules`.
    Возвращает `{provider: {"strategy": ..., "models": [...]}}`; пустая карта
    при ошибке/отсутствии данных (всё → unknown)."""
    try:
        with session() as conn:
            rows = conn.execute(
                "SELECT provider, strategy, models FROM free_rules").fetchall()
    except Exception as exc:
        # Без вывода ошибка БД/прав/диска неотличима от «правил нет» — все модели
        # молча уходят в unknown. Оставляем след в stderr (ср. load_project).
        print(f"warning: не удалось загрузить free_rules: {exc}", file=sys.stderr)
        return {}
    rules: dict[str, dict] = {}
    for r in rows:
        # Битый JSON в models не должен ронять весь тестер (как в load_library).
        # «or []»: json.loads("null") → None, а downstream ожидает list.
        models = json_loads_or(r["models"] or "[]", default=[]) or []
        rules[r["provider"]] = {"strategy": r["strategy"], "models": models}
    return rules


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


def refs_from_catalog(entries: list[ModelCatalogEntry]) -> list[ModelRef]:
    """ModelCatalogEntry из `opencode models` → ModelRef с free_rules."""
    rules = load_free_rules()
    refs: list[ModelRef] = []
    for entry in entries:
        refs.append(ModelRef(
            provider=entry.provider,
            model=entry.model,
            free_status=classify_model(entry.provider, entry.model, entry, rules),
            name=entry.name,
        ))
    return refs


def parse_models_arg(values: list[str]) -> list[ModelRef]:
    """'provider/model' → ModelRef (split по первому '/')."""
    refs: list[ModelRef] = []
    for raw in values:
        if not raw.strip():
            continue
        try:
            provider, model = split_model_ref(raw)
        except ValueError as exc:
            raise SystemExit(f"Неверный формат модели: {exc}") from exc
        refs.append(ModelRef(provider=provider, model=model))
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


def filter_excluded_models(
    refs: list[ModelRef],
) -> tuple[list[ModelRef], list[tuple[ModelRef, str]]]:
    """Drop project-denylisted models while preserving input order."""
    with session() as conn:
        exclusions = active_exclusions_map(conn)
    allowed: list[ModelRef] = []
    skipped: list[tuple[ModelRef, str]] = []
    for ref in refs:
        reason = exclusions.get((ref.provider, ref.model))
        if reason is None:
            allowed.append(ref)
        else:
            skipped.append((ref, reason))
    return allowed, skipped


def _normalize_model_search_text(value: str) -> str:
    separated = _MODEL_SEARCH_SEPARATOR_RE.sub(" ", value.casefold())
    return " ".join(separated.split())


def filter_model_query(refs: list[ModelRef], query: str | None) -> list[ModelRef]:
    """Фильтр по словам в provider/model и display name."""
    normalized_query = _normalize_model_search_text(query or "")
    if not normalized_query:
        return refs
    compact_query = normalized_query.replace(" ", "")
    terms = normalized_query.split()
    out: list[ModelRef] = []
    for ref in refs:
        haystack = _normalize_model_search_text(f"{ref.key} {ref.name or ''}")
        compact_haystack = haystack.replace(" ", "")
        if all(term in haystack for term in terms) or compact_query in compact_haystack:
            out.append(ref)
    return out


def resolve_model_list(args: argparse.Namespace) -> tuple[list[ModelRef], str, list[ModelRef]]:
    """Гибрид: явный список (--models/--models-file) или catalog opencode.

    Возвращает (отфильтрованные_модели, источник, полный_список_до_фильтра).
    Полный список нужен main() для отчёта про unknown-провайдеров — без второго
    запроса к catalog. Опционально фильтрует по --provider и --query."""
    if args.models_file:
        refs = load_models_file(args.models_file)
        source = "models-file"
    elif args.models:
        refs = parse_models_arg(args.models)
        source = "models-flag"
    else:
        entries = load_opencode_models(
            provider=args.provider,
            refresh=getattr(args, "refresh_models", False),
        )
        refs = refs_from_catalog(entries)
        source = "opencode-models"

    full = list(refs)  # до фильтрации по provider/free — для диагностики unknown

    if args.provider:
        refs = [r for r in refs if r.provider == args.provider]

    # По умолчанию проверяем только бесплатные модели (free_status == "free" по
    # free_rules); paid и unknown отсеиваются. --pay-models снимает фильтр.
    # Применяется лишь к списку из API; явный список (--models / --models-file)
    # пользователь выбрал сам — не фильтруем.
    if source == "opencode-models" and not args.pay_models:
        refs = [r for r in refs if r.free]
        source += "+free-only"

    query = getattr(args, "query", None)
    if query:
        refs = filter_model_query(refs, query)
        source += "+query"

    return _dedup(refs), source, full


# --- одна проверка ----------------------------------------------------------

def check_one(ref: ModelRef, prompt: str, agent: str, timeout: float, port: int,
              log_dir: Path, run_dir: Path, log_suffix: str = "") -> CheckResult:
    """Пингует одну модель через probe_session, пишет подробности в per-model лог.

    `log_suffix` неймспейсит файл лога по фазе (фаза 2 ретрая пишет в
    `<key>.retry.log`), чтобы повторный прогон той же модели не труновал лог
    предыдущей фазы. `CheckResult.log_path` всегда указывает на лог ИМЕННО этой
    попытки — так выбранный вердикт не расходится со своим логом (issue B10)."""
    log_path = log_dir / f"{sanitize_name(ref.key)}{log_suffix}.log"
    start = time.monotonic()
    lock = threading.Lock()
    with log_path.open("w", encoding="utf-8") as log:
        log.write(f"=== {ref.key} | timeout={timeout:.0f}s ===\n")

        def write(msg: str) -> None:
            with lock:
                log.write(msg)
                log.flush()

        # Краш probe_session (упавший сервер, разрыв соединения и т.п.) не должен
        # ронять весь прогон и терять уже собранные результаты — ловим и помечаем
        # модель как error, прогон продолжается, availability.json пишется.
        try:
            session_result = probe_session(
                task=prompt, model=ref.model, provider=ref.provider,
                agent=agent, timeout=timeout, port=port, write=write,
            )
            code = session_result.code
            reason = session_result.reason
        except Exception as exc:
            write(f"\n--- сбой проверки ---\n[{exc.__class__.__name__}] {exc}\n")
            code, reason = 2, f"{exc.__class__.__name__}: {exc}"

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


_STATUS_ICON = {"available": "✅", "timeout": "⏱", "error": "❌", "rate_limited": "🚦"}


def _emit(label: str, res: CheckResult) -> None:
    """Краткий статус одной модели в stdout по ходу прогона."""
    icon = _STATUS_ICON.get(res.status, "?")
    tail = f" — {res.reason}" if res.reason else ""
    print(f"[{label}] {res.ref.key} {icon} {res.status} "
          f"({fmt_secs(res.elapsed)}){tail}", flush=True)


def _run_phase(refs: list[ModelRef], prompt: str, agent: str, timeout: float,
               port: int, log_dir: Path, run_dir: Path, label: str,
               log_suffix: str = "") -> dict[str, CheckResult]:
    """Последовательный прогон списка моделей; печатает статус по мере."""
    results: dict[str, CheckResult] = {}
    total = len(refs)
    for i, ref in enumerate(refs, 1):
        res = check_one(ref, prompt, agent, timeout, port, log_dir, run_dir, log_suffix)
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
            # Фаза 2 пишет в отдельный `<key>.retry.log`, чтобы не затирать лог
            # фазы 1: если её вердикт окажется хуже и будет отброшен, лог фазы 1
            # должен остаться нетронутым (issue B10).
            retry = _run_phase(retry_refs, prompt, agent, retry_timeout, port,
                               log_dir, run_dir, "фаза2", log_suffix=".retry")
            for key, res in retry.items():
                res.retried = True
                # Лучший из двух: меньший code предпочтительнее
                # (0 доступно < 1 таймаут < 2 ошибка < 3 лимит).
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
              f"{fmt_secs(r.elapsed):>8}  {reason}")


def print_model_list(refs: list[ModelRef], source: str) -> None:
    """Печатает выбранный список моделей без ping."""
    print(f"Моделей: {len(refs)} (источник: {source})")
    if not refs:
        print("(нет моделей)")
        return

    key_w = max(len("provider/model"), max(len(r.key) for r in refs))
    print(f"{'provider/model':<{key_w}}  {'free':<8} name")
    for ref in refs:
        print(f"{ref.key:<{key_w}}  {ref.free_status:<8} {ref.name or ''}")


def write_availability_json(results: list[CheckResult], path: Path, meta: dict) -> None:
    counts = tally_statuses(results)
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
    parser.add_argument("--query", help="Поиск по provider/model и имени модели")
    parser.add_argument("--list-models", action="store_true",
                        help="Показать модели из opencode catalog без ping и serve")
    parser.add_argument("--refresh-models", action="store_true",
                        help="Обновить кэш opencode models перед выборкой")
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
    parser.add_argument("--include-excluded", action="store_true",
                        help="Проверять модели из project denylist-а")
    parser.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT,
                        help=f"Порт opencode serve (default: {DEFAULT_BASE_PORT})")
    args = parser.parse_args()

    try:
        refs, source, full_refs = resolve_model_list(args)
    except ModelCatalogError as exc:
        print(f"Не удалось получить список моделей opencode: {exc}", file=sys.stderr)
        sys.exit(2)
    skipped_exclusions: list[tuple[ModelRef, str]] = []
    if not args.include_excluded:
        refs, skipped_exclusions = filter_excluded_models(refs)
    if skipped_exclusions:
        source += "+denylist"
        print(f"Пропущено моделей из denylist-а: {len(skipped_exclusions)}")

    # В дефолтном free-режиме предупредим, какие провайдеры пропущены как unknown
    # (нет правила в free_rules) — это то, что предстоит «разобрать».
    # Берём полный список из resolve_model_list — без повторного запроса к серверу.
    if source.startswith("opencode-models") and not args.pay_models:
        unknown = sorted({r.provider for r in full_refs
                          if r.free_status == "unknown"})
        if unknown:
            print(f"⚠ Пропущены провайдеры без правила в free_rules "
                  f"(unknown): {', '.join(unknown)}")
            print("  Добавь им strategy в таблицу free_rules или используй "
                  "--provider <id> / --pay-models.")

    if args.list_models:
        print_model_list(refs, source)
        return

    if not refs:
        print("Нет моделей для проверки (проверь --models / --provider).", file=sys.stderr)
        sys.exit(1)

    started_at = _dt.datetime.now()
    run_dir = AVAILABILITY_ROOT / started_at.strftime("%Y%m%d-%H%M%S")
    log_dir = run_dir / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    print(f"Папка прогона: {rel_to_root(run_dir)}")
    print(f"Поднимаю opencode serve на :{args.base_port}")

    # Готовый thread-safe writer с префиксом и EPIPE-обработкой (а не inline print).
    status = status_printer("server")

    # Один сервер на весь прогон; гасится через atexit и обработчики сигналов runtime.
    install_shutdown_handlers()
    if not ensure_server_running(run_dir, args.base_port, status):
        print("Не удалось поднять opencode serve — прерываюсь", file=sys.stderr)
        sys.exit(2)

    print(f"Моделей к проверке: {len(refs)} (источник: {source})")
    print("--- старт ---")

    results = check_models(
        refs=refs, prompt=args.prompt, agent=args.agent,
        base_timeout=args.timeout, retry_timeout=args.retry_timeout,
        do_retry=not args.no_retry, port=args.base_port,
        log_dir=log_dir, run_dir=run_dir,
    )

    print_table(results)
    print("--- сводка ---")
    # Сводка из единой таксономии RUN_CODES (summary_counts), с локальной меткой
    # «доступно» вместо «готово» для code=0 — 5-й код подхватится автоматически.
    print(summary_line(summary_counts(r.code for r in results),
                       total=len(results), labels={"ok": "доступно"}))

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
        "skipped_model_exclusions": [
            {
                "provider": ref.provider,
                "model": ref.model,
                "reason": reason,
            }
            for ref, reason in skipped_exclusions
        ],
    }
    json_path = run_dir / "availability.json"
    write_availability_json(results, json_path, meta)
    print(f"Отчёт: {rel_to_root(json_path)}")


if __name__ == "__main__":
    main()

import argparse
import atexit
import datetime as _dt
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import httpx
from opencode_ai import Opencode

from pricing import get_pricing, format_price_display

PROJECT_ROOT = Path(__file__).resolve().parent
WORK_ROOT = PROJECT_ROOT / "data" / "result"
CONFIG_PATH = PROJECT_ROOT / "opencode.json"

DEFAULT_BASE_PORT = 4096
DEFAULT_MODEL = "glm-5.1"
DEFAULT_PROVIDER = "zai-coding-plan"
DEFAULT_AGENT = "coder"
DEFAULT_COPIES = 5
SERVER_CHECK_TIMEOUT = 30
SERVER_CHECK_INTERVAL = 2

# Тип «писателя» прогресса: куда копия пишет подробный вывод (обычно — её run.log).
Writer = Callable[[str], None]


def _base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _client(port: int) -> Opencode:
    return Opencode(base_url=_base_url(port))


# Все поднятые нами серверы: (process, stderr_log_path). Гасятся через atexit.
_server_processes: list[tuple[subprocess.Popen, Path]] = []
_server_lock = threading.Lock()
# Защищает короткий статус-вывод в общий stdout от перемешивания строк.
_print_lock = threading.Lock()


def _sanitize(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name).strip("-")
    return cleaned or "x"


def prepare_work_dirs(project: str, model: str, copies: int) -> list[Path]:
    """Создаёт папку прогона `data/result/<project>_<model>/` и под ней N подпапок
    `<YYYYMMDD>-<HHMMSS>_<i>` — по одной на копию. Возвращает их пути (resolve)."""
    base_name = f"{_sanitize(project)}_{_sanitize(model)}"
    run_root = WORK_ROOT / base_name
    run_root.mkdir(parents=True, exist_ok=True)

    stamp = _dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    dirs: list[Path] = []
    for i in range(1, copies + 1):
        copy_dir = run_root / f"{stamp}_{i}"
        # Маловероятная коллизия (тот же прогон в ту же секунду) — добавим суффикс.
        if copy_dir.exists():
            copy_dir = run_root / f"{stamp}_{i}_{int(time.monotonic() * 1000) % 100000}"
        copy_dir.mkdir(parents=True, exist_ok=False)
        dirs.append(copy_dir.resolve())
    return dirs


def _stop_servers() -> None:
    with _server_lock:
        procs = list(_server_processes)
    for proc, _log in procs:
        if proc.poll() is not None:
            continue
        proc.terminate()
    for proc, _log in procs:
        if proc.poll() is not None:
            continue
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()


atexit.register(_stop_servers)


def _try_connect(port: int) -> bool:
    try:
        _client(port).session.list()
        return True
    except ConnectionError:
        return False
    except Exception as exc:
        # SDK оборачивает httpx-ошибки в свои классы — ловим по имени,
        # чтобы не глотать ошибки авторизации/конфига молча.
        if exc.__class__.__name__ in {"APIConnectionError", "ConnectError"}:
            return False
        raise


def ensure_server_running(work_dir: Path, port: int, status: Writer) -> bool:
    """Поднимает `opencode serve` на `port` с cwd=work_dir, если он ещё не отвечает.
    Возвращает True при успехе, False — если сервер не удалось поднять."""
    if _try_connect(port):
        return True

    status(f"запускаю opencode serve на :{port}")
    stderr_file = tempfile.NamedTemporaryFile(
        prefix=f"opencode-serve-{port}-", suffix=".log", delete=False
    )
    stderr_path = Path(stderr_file.name)
    env = os.environ.copy()
    env["OPENCODE_CONFIG"] = str(CONFIG_PATH)
    proc = subprocess.Popen(
        ["opencode", "serve", "--port", str(port)],
        stdout=subprocess.DEVNULL,
        stderr=stderr_file,
        cwd=str(work_dir),
        env=env,
    )
    with _server_lock:
        _server_processes.append((proc, stderr_path))

    waited = 0
    while waited < SERVER_CHECK_TIMEOUT:
        time.sleep(SERVER_CHECK_INTERVAL)
        waited += SERVER_CHECK_INTERVAL
        if proc.poll() is not None:
            log = stderr_path.read_text(errors="replace").strip()
            status(f"opencode serve упал (код {proc.returncode}):\n{log}")
            return False
        if _try_connect(port):
            status(f"сервер :{port} запущен (ожидал {waited}с)")
            return True

    log = stderr_path.read_text(errors="replace").strip()
    tail = "\n".join(log.splitlines()[-20:]) if log else "(stderr пустой)"
    status(f"opencode serve :{port} не ответил за {SERVER_CHECK_TIMEOUT}с.\n"
           f"Последние строки stderr:\n{tail}")
    return False


def _extract_session_id(payload: dict) -> str | None:
    """В SSE-событиях ID нашей сессии встречается в разных местах: вытаскиваем."""
    props = payload.get("properties", payload)
    if not isinstance(props, dict):
        return None
    # session.* события: properties.info.id
    info = props.get("info")
    if isinstance(info, dict):
        sid = info.get("sessionID") or info.get("id")
        if isinstance(sid, str) and sid.startswith("ses_"):
            return sid
    # message.*, tool.* события: properties.sessionID
    sid = props.get("sessionID")
    if isinstance(sid, str):
        return sid
    return None


def _format_event(payload: dict) -> str | None:
    """Превращает событие SSE в строку для пользователя. None — событие неинтересно."""
    etype = payload.get("type", "")
    props = payload.get("properties", {})

    if etype == "message.part.updated":
        part = props.get("part", {})
        ptype = part.get("type")
        if ptype == "text":
            return part.get("text", "")
        if ptype == "tool":
            tool = part.get("tool") or part.get("name") or "?"
            state = (part.get("state") or {}).get("status", "")
            return f"\n[tool: {tool} {state}]"
        if ptype == "reasoning":
            return None  # внутренние размышления модели не печатаем
        return None

    if etype == "tool.execute.before":
        return f"\n[tool start: {props.get('tool', '?')}]"
    if etype == "tool.execute.after":
        return f"\n[tool done: {props.get('tool', '?')}]"
    if etype == "session.error":
        return f"\n[SESSION ERROR] {json.dumps(props, ensure_ascii=False)[:300]}"
    if etype == "session.idle":
        return None  # сигнал завершения обработаем отдельно
    return None


# Куда opencode пишет структурированные логи (ERROR с HTTP-статусом провайдера,
# ретраи и т.п.). stderr самого процесса при этом пустой, поэтому причину «зависания»
# (429, rate-limit) ищем именно здесь, фильтруя по session.id.
OPENCODE_LOG_DIR = Path.home() / ".local" / "share" / "opencode" / "log"


def _opencode_error_tail(session_id: str, lines: int = 8) -> str | None:
    """Последние строки уровня ERROR из файлового лога opencode для нашей сессии.
    Возвращает компактную сводку (HTTP-статус + текст), либо None, если ничего нет.

    opencode ретраит ретраибельные ошибки провайдера (напр. 429) внутри AI SDK и не
    шлёт session.error по SSE — поэтому копия «зависает» до дедлайна. Здесь достаём
    реальную причину из лога, чтобы она попала в run.log."""
    try:
        log_files = sorted(OPENCODE_LOG_DIR.glob("*.log"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None

    found: list[str] = []
    # Каждая копия поднимает свой `opencode serve` со своим лог-файлом, поэтому
    # сканируем ВСЕ логи, а не только пару последних: при одновременных таймаутах
    # mtimes почти равны и срез мог бы потерять лог нужной копии. Фильтр по
    # session_id отсекает чужие строки, так что лишние файлы просто не совпадут.
    # Читаем построчно (логи append-only и растут — не грузим файл целиком в память).
    for log_file in log_files:
        try:
            with log_file.open(errors="replace") as fh:
                for raw in fh:
                    raw = raw.rstrip("\n")
                    if not raw.startswith("ERROR") or session_id not in raw:
                        continue
                    status = re.search(r'statusCode["\s:=]+(\d+)', raw)
                    err_name = re.search(r'"name":"([^"]+)"', raw)
                    detail = re.search(r'"message":"([^"]{0,160})"', raw)
                    parts = []
                    if status:
                        parts.append(f"HTTP {status.group(1)}")
                    if err_name:
                        parts.append(err_name.group(1))
                    detail_text = detail.group(1) if detail else None
                    # «Too Many Requests» добавляем явно, только если его ещё нет
                    # (иначе при наличии его же в message получался бы дубль).
                    if "Too Many Requests" in raw and not (
                        detail_text and "Too Many Requests" in detail_text
                    ):
                        parts.append("Too Many Requests")
                    if detail_text:
                        parts.append(detail_text)
                    summary = " | ".join(parts) if parts else raw[:200]
                    if summary not in found:
                        found.append(summary)
        except OSError:
            continue
        if found:
            break
    if not found:
        return None
    return "\n".join(found[-lines:])


def _error_text(props: dict) -> str:
    """Достаёт человекочитаемый текст из session.error / info.error."""
    err = props.get("error") or {}
    data = err.get("data") or {}
    msg = data.get("message") or err.get("message") or err.get("name") or "?"
    code = data.get("statusCode")
    return f"{msg}" + (f" (HTTP {code})" if code else "")


def _sse_reader(base: str, session_id: str, done: threading.Event,
                stop: threading.Event, result: dict, write: Writer) -> None:
    """Фон: читает GET /event, фильтрует по нашей сессии, пишет прогресс через `write`,
    выставляет `done`, когда пришёл session.idle/session.error для нашей сессии.
    При ошибке кладёт текст в result["error"]."""
    try:
        with httpx.stream("GET", f"{base}/event", timeout=None) as resp:
            for raw in resp.iter_lines():
                if stop.is_set():
                    return
                if not raw or not raw.startswith("data:"):
                    continue
                try:
                    payload = json.loads(raw[5:].strip())
                except json.JSONDecodeError:
                    continue
                sid = _extract_session_id(payload)
                if sid and sid != session_id:
                    continue
                etype = payload.get("type", "")
                msg = _format_event(payload)
                if msg:
                    write(msg if etype == "message.part.updated" else msg + "\n")
                if etype == "session.error" and sid == session_id:
                    result["error"] = _error_text(payload.get("properties", {}))
                    done.set()
                    return
                if etype == "session.idle" and sid == session_id:
                    done.set()
                    return
    except Exception as exc:
        write(f"\n[SSE reader error] {exc}\n")
        done.set()


def probe_session(task: str, model: str, provider: str, agent: str, timeout: float,
                  port: int, write: Writer) -> tuple[int, str | None]:
    """Ядро `run_task`: гоняет одну сессию и возвращает (code, reason).

    code: 0 — готово, 1 — таймаут, 2 — ошибка сессии.
    reason: человекочитаемая причина для code != 0 (из HTTP-тела, `_error_text`
    или файлового лога opencode), иначе None.

    Подробный прогресс по-прежнему пишется через `write` — поведение для `run_task`
    не меняется.
    """
    base = _base_url(port).rstrip("/")
    deadline = time.monotonic() + timeout

    with httpx.Client(base_url=base, timeout=30.0) as http:
        write(f"Создаю сессию (агент: {agent})...\n")
        sess = http.post("/session", json={}).json()
        session_id = sess["id"]
        write(f"Сессия: {session_id}\n")
        write(f"Модель: {provider}/{model}\n")
        write("--- работа ---\n")

        done = threading.Event()
        stop = threading.Event()
        result: dict = {}
        reader = threading.Thread(
            target=_sse_reader,
            args=(base, session_id, done, stop, result, write),
            daemon=True,
        )
        reader.start()
        # Небольшая фора, чтобы reader точно подписался до отправки сообщения.
        time.sleep(0.3)

        def provider_error_tail() -> str | None:
            """Реальная причина из файлового лога opencode — ретраи и ошибки
            провайдера (429 и т.п.), которые не приходят по SSE. Пишет её в `write`
            и возвращает текст для reason (или None)."""
            tail = _opencode_error_tail(session_id)
            if tail:
                write("\n--- ошибки провайдера из лога opencode ---\n"
                      f"{tail}\n")
            return tail

        def with_tail(reason: str) -> str:
            """Приклеить хвост лога opencode к причине, если он добавляет новое.
            Для явных ошибок (session.error/HTTP) причина уже содержит тот же
            текст сообщения провайдера — не дублируем; хвост ценен в основном
            для таймаутов, где причины по SSE нет."""
            tail = provider_error_tail()
            if not tail:
                return reason
            first_line = tail.splitlines()[0]
            # Хвост и причина часто несут одно сообщение в разном порядке
            # («msg (HTTP 401)» vs «HTTP 401 | … | msg»). Берём самый длинный
            # общий кусок: если значимая часть хвоста уже есть в reason — дубль.
            sig = max(first_line.split(" | "), key=len).strip()
            if sig and sig in reason:
                return reason
            return f"{reason} | {first_line}"

        body = {
            "agent": agent,
            "model": {"providerID": provider, "modelID": model},
            "parts": [{"type": "text", "text": task}],
        }

        # finally гарантирует stop.set() на любом выходе (return ИЛИ исключение),
        # иначе SSE-поток-демон остался бы жить с устаревшим session_id.
        try:
            # Отправляем синхронный POST. Сервер либо вернёт финал, либо ответит
            # рано — в любом случае дальше ждём событие session.idle.
            post_timeout = max(1.0, deadline - time.monotonic())
            post_start = time.monotonic()
            try:
                resp = http.post(
                    f"/session/{session_id}/message",
                    json=body,
                    timeout=post_timeout,
                )
                # Ошибка модели/провайдера приходит в теле (HTTP 200, info.error)
                # ИЛИ как ненулевой HTTP-код. Не ждём session.idle — может не прийти.
                if resp.status_code >= 400:
                    write(f"\n--- ошибка ---\n[HTTP {resp.status_code}] {resp.text[:400]}\n")
                    reason = f"HTTP {resp.status_code}: {resp.text[:200].strip()}"
                    return 2, with_tail(reason)
                try:
                    info = (resp.json() or {}).get("info", {})
                except Exception:
                    info = {}
                if isinstance(info, dict) and info.get("error"):
                    reason = _error_text(info)
                    write(f"\n--- ошибка ---\n[{reason}]\n")
                    return 2, with_tail(reason)
            except httpx.ReadTimeout:
                waited = time.monotonic() - post_start
                write(f"\n[POST /message не ответил за {waited:.1f}с — "
                      "продолжаем ждать события до дедлайна]\n")

            # Ждём окончания работы сессии до общего дедлайна.
            remaining = max(0.0, deadline - time.monotonic())
            idle = done.wait(timeout=remaining)

            if result.get("error"):
                reason = result["error"]
                write(f"\n--- ошибка ---\n[{reason}]\n")
                return 2, with_tail(reason)
            if idle:
                write("\n--- готово ---\n")
                return 0, None
            # Таймаут: причина «зависания» (ретраи, 429) обычно лежит в файловом
            # логе opencode — её достаёт provider_error_tail().
            write("\n--- таймаут ---\n")
            tail = provider_error_tail()
            reason = f"нет ответа за {timeout:.0f}с"
            # При таймауте причина часто только в логе — приклеиваем первую строку.
            return 1, (f"{reason} | {tail.splitlines()[0]}" if tail else reason)
        finally:
            stop.set()


def run_task(task: str, model: str, provider: str, agent: str, timeout: float,
             port: int, write: Writer) -> int:
    """Отправляет задачу в opencode (на `port`) и ждёт окончания работы сессии.
    Подробный прогресс пишется через `write`.

    Возвращает код выхода: 0 — норм, 1 — таймаут, 2 — ошибка сессии.
    Тонкая обёртка над `probe_session` (причину отбрасываем).
    """
    code, _reason = probe_session(task, model, provider, agent, timeout, port, write)
    return code


def _status_printer(label: str) -> Writer:
    """Короткий статус копии в общий stdout, защищённый локом от перемешивания."""
    def emit(msg: str) -> None:
        with _print_lock:
            print(f"[{label}] {msg}", flush=True)
    return emit


_VERDICT = {0: "готово", 1: "таймаут", 2: "ошибка"}


def _fmt_secs(s: float) -> str:
    return f"{s:.1f}с"


def _verdict(code: int) -> str:
    return _VERDICT.get(code, f"код {code}")


def run_copy(index: int, work_dir: Path, port: int, task: str, model: str,
             provider: str, agent: str, timeout: float) -> dict:
    """Один прогон: поднимает сервер на своём порту, гоняет задачу, подробный лог
    пишет в run.log внутри work_dir. В stdout — только краткий статус.

    Возвращает результат-структуру: {index, port, dir, code, elapsed}.
    Время `elapsed` меряется от входа в функцию (вкл. старт сервера) до выхода."""
    start = time.monotonic()
    label = f"copy {index}"
    status = _status_printer(label)
    rel = work_dir.relative_to(PROJECT_ROOT) if work_dir.is_relative_to(PROJECT_ROOT) else work_dir
    status(f"старт → {rel} (:{port})")

    def result(code: int) -> dict:
        return {
            "index": index, "port": port, "dir": str(work_dir),
            "code": code, "elapsed": time.monotonic() - start,
        }

    log_path = work_dir / "run.log"
    with log_path.open("w", encoding="utf-8") as log:
        log_lock = threading.Lock()

        def write(msg: str) -> None:
            with log_lock:
                log.write(msg)
                log.flush()

        if not ensure_server_running(work_dir, port, status):
            write("[не удалось поднять opencode serve]\n")
            res = result(2)
            status(f"ошибка: сервер не поднялся за {_fmt_secs(res['elapsed'])}")
            return res

        rc = run_task(
            task=task, model=model, provider=provider, agent=agent,
            timeout=timeout, port=port, write=write,
        )

    res = result(rc)
    status(f"{_verdict(rc)} за {_fmt_secs(res['elapsed'])} "
           f"(лог: {log_path.relative_to(PROJECT_ROOT) if log_path.is_relative_to(PROJECT_ROOT) else log_path})")
    return res


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Автономный кодинг-агент (opencode): N параллельных копий задачи",
    )
    parser.add_argument("task", nargs="?", help="Задача для агента")
    parser.add_argument("-f", "--file", type=Path, help="Файл с задачей")
    parser.add_argument("--project", required=True,
                        help="Название проекта (используется как имя рабочей папки)")
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL,
                        help=f"Модель (default: {DEFAULT_MODEL})")
    parser.add_argument("-p", "--provider", default=DEFAULT_PROVIDER,
                        help=f"Провайдер (default: {DEFAULT_PROVIDER})")
    parser.add_argument("-a", "--agent", default=DEFAULT_AGENT,
                        help=f"Имя агента (default: {DEFAULT_AGENT})")
    parser.add_argument("-n", "--copies", type=int, default=DEFAULT_COPIES,
                        help=f"Сколько параллельных копий запустить (default: {DEFAULT_COPIES})")
    parser.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT,
                        help=f"Порт первой копии; остальные +1 (default: {DEFAULT_BASE_PORT})")
    parser.add_argument("--timeout", type=float, default=120.0,
                        help="Жёсткий таймаут на одну копию в секундах (default: 120)")

    args = parser.parse_args()

    if not args.task and not args.file:
        parser.error("Укажите задачу или файл с задачей (--file)")
    if args.copies < 1:
        parser.error("--copies должно быть >= 1")

    task = args.task or ""
    if args.file:
        task = args.file.read_text(encoding="utf-8")

    dirs = prepare_work_dirs(args.project, args.model, args.copies)
    run_root = dirs[0].parent
    run_root_rel = run_root.relative_to(PROJECT_ROOT) if run_root.is_relative_to(PROJECT_ROOT) else run_root
    started_at = _dt.datetime.now()
    print(f"Запускаю {args.copies} копий: {args.provider}/{args.model}")
    print(f"Папка прогона: {run_root_rel}")
    print("--- старт ---")

    # Цена нужна только в конце, а её lookup может стучаться в сеть (протух кэш
    # каталога) — гоним параллельно прогону, не блокируя старт копий.
    run_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.copies + 1) as pool:
        pricing_future = pool.submit(get_pricing, args.provider, args.model)
        futures = [
            pool.submit(
                run_copy,
                i + 1, work_dir, args.base_port + i, task,
                args.model, args.provider, args.agent, args.timeout,
            )
            for i, work_dir in enumerate(dirs)
        ]
        results = [f.result() for f in futures]
        pricing = pricing_future.result()
    run_elapsed = time.monotonic() - run_start

    results.sort(key=lambda r: r["index"])
    codes = [r["code"] for r in results]
    elapsed = [r["elapsed"] for r in results]
    ok = codes.count(0)
    timeouts = codes.count(1)
    errors = sum(1 for c in codes if c >= 2)

    # Таблица по копиям.
    print("--- отчёт по времени ---")
    print(f"{'копия':<6} {'статус':<8} {'время':>8}")
    for r in results:
        print(f"{r['index']:<6} {_verdict(r['code']):<8} {_fmt_secs(r['elapsed']):>8}")
    # Итоги.
    print(f"всего (wall-clock): {_fmt_secs(run_elapsed)}")
    if elapsed:
        print(f"быстрее всех:       {_fmt_secs(min(elapsed))}")
        print(f"медленнее всех:     {_fmt_secs(max(elapsed))}")
        print(f"в среднем:          {_fmt_secs(sum(elapsed) / len(elapsed))}")
    price_str = format_price_display(pricing)
    if price_str != "N/A":
        print(f"цена:               {price_str}")
    print("--- сводка ---")
    print(f"{ok} готово / {timeouts} таймаут / {errors} ошибка (из {args.copies})")

    # Машиночитаемый отчёт.
    report = {
        "project": args.project,
        "model": args.model,
        "provider": args.provider,
        "copies": args.copies,
        "started_at": started_at.isoformat(),
        "run_elapsed": run_elapsed,
        "summary": {"ok": ok, "timeout": timeouts, "error": errors},
        "pricing": pricing,
        "runs": [
            {
                "index": r["index"], "port": r["port"], "dir": r["dir"],
                "status": _verdict(r["code"]), "code": r["code"],
                "elapsed": r["elapsed"],
            }
            for r in results
        ],
    }
    report_path = run_root / "report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2),
                           encoding="utf-8")
    print(f"Отчёт: {report_path.relative_to(PROJECT_ROOT) if report_path.is_relative_to(PROJECT_ROOT) else report_path}")

    sys.exit(max(codes) if codes else 0)


if __name__ == "__main__":
    main()

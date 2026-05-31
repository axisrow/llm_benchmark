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
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
import httpx_sse
from opencode_ai import Opencode

from artifacts import collect_report_artifacts, cleanup_collected_artifacts
from pricing import get_pricing, format_price_display
from usage import (
    Usage,
    estimate_usage_cost,
    extract_session_usage,
    extract_usage_from_message,
    format_tokens,
    format_usd_cost,
    summarize_usages,
)

PROJECT_ROOT = Path(__file__).resolve().parent
WORK_ROOT = PROJECT_ROOT / "data" / "result"
CONFIG_PATH = PROJECT_ROOT / "opencode.json"

# db.py живёт в scripts/ — единственный источник правды (data/main.db).
sys.path.insert(0, str(PROJECT_ROOT / "scripts"))
from db import DB_PATH, connect, init_schema, upsert_report

DEFAULT_BASE_PORT = 4096
DEFAULT_MODEL = "glm-5.1"
DEFAULT_PROVIDER = "zai-coding-plan"
DEFAULT_AGENT = "coder"
DEFAULT_COPIES = 5
SERVER_CHECK_TIMEOUT = 30
SERVER_CHECK_INTERVAL = 2

# Тип «писателя» прогресса: куда копия пишет подробный вывод (обычно — её run.log).
Writer = Callable[[str], None]


@dataclass(frozen=True)
class SessionProbeResult:
    code: int
    reason: str | None = None
    usage: Usage | None = None


def _base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def _client(port: int) -> Opencode:
    return Opencode(base_url=_base_url(port))


# Все поднятые нами серверы: (process, stderr_log_path). Гасятся через atexit.
_server_processes: list[tuple[subprocess.Popen, Path]] = []
_server_owners: dict[int, tuple[subprocess.Popen, Path]] = {}
_server_lock = threading.Lock()
# Защищает короткий статус-вывод в общий stdout от перемешивания строк.
_print_lock = threading.Lock()


def _sanitize(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    # Схлопываем последовательности точек: одиночная точка в версии модели
    # (glm-5.1) допустима, но `..` — обход каталога вверх. Имена приходят в т.ч.
    # из содержимого report.json (миграция), поэтому это вопрос безопасности пути.
    cleaned = re.sub(r"\.{2,}", ".", cleaned).strip("-.")
    return cleaned or "x"


def load_project(project: str) -> dict | None:
    """Возвращает запись проекта из таблицы `projects_library` (prompt,
    description, what_it_tests) либо None, если записи нет. Сбой чтения базы
    не должен ронять запуск — возвращаем None."""
    try:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT raw_json FROM projects_library WHERE name = ?",
                (project,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if row is None:
        return None
    try:
        entry = json.loads(row["raw_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    return entry if isinstance(entry, dict) else None


def save_report(report: dict, run_root: Path, artifacts: list[object] | None = None) -> None:
    """Пишет отчёт прогона в базу через общий db.upsert_report.

    `raw_json` хранит сериализованный dict в том же виде, что раньше шёл в
    report.json — это даёт точный round-trip в build_index.load_reports."""
    rel_path = (run_root.relative_to(PROJECT_ROOT).as_posix() + "/report.json"
                if run_root.is_relative_to(PROJECT_ROOT)
                else str(run_root) + "/report.json")
    raw_json = json.dumps(report, ensure_ascii=False, indent=2)

    conn = connect()
    try:
        init_schema(conn)
        with conn:
            upsert_report(conn, report, rel_path, raw_json, artifacts=artifacts)
    finally:
        conn.close()


def _cleanup_index_snapshot(index_path: Path) -> None:
    """Удаляет локальный snapshot дашборда после остановки `bench.py serve`."""
    try:
        index_path.unlink()
    except FileNotFoundError:
        return
    except OSError as exc:
        print(f"[serve] не удалось удалить {index_path}: {exc}", file=sys.stderr)


def serve(port: int = 8000) -> None:
    """Поднимает локальный тестовый веб-сервер: пересобирает docs/data/index.json
    из базы (build_index) и раздаёт папку docs/ через http.server stdlib.

    Фронтенд думает, что читает статический index.json — на деле он только что
    собран из data/main.db. На GitHub Pages используется тот же build_index.

    Новые прогоны видны по F5 без перезапуска: запрос /data/index.json сам
    пересобирает индекс из базы, но только если база реально изменилась (кэш по
    mtime data/main.db + -wal). Без новых данных F5 не пересобирает ничего."""
    import functools
    import http.server
    import socketserver

    # build_index лежит в scripts/ (уже в sys.path с уровня модуля).
    from build_index import build_index

    docs_dir = PROJECT_ROOT / "docs"
    index_path = docs_dir / "data" / "index.json"

    def _db_fingerprint() -> float:
        """Отпечаток свежести базы: максимум mtime среди main.db и main.db-wal.
        WAL обязателен — SQLite дописывает свежие данные в -wal до checkpoint,
        одного .db недостаточно. Отсутствующий файл пропускаем."""
        newest = 0.0
        for p in (DB_PATH, DB_PATH.with_name(DB_PATH.name + "-wal")):
            try:
                newest = max(newest, p.stat().st_mtime)
            except FileNotFoundError:
                pass
        return newest

    last_fp = 0.0
    owns_index_snapshot = False
    try:
        class Handler(http.server.SimpleHTTPRequestHandler):
            def _maybe_rebuild(self) -> None:
                # Пересобираем только перед отдачей самого index.json и только если
                # база изменилась. Прочие пути (html, favicon) — мимо.
                nonlocal last_fp
                if self.path.split("?", 1)[0] != "/data/index.json":
                    return
                fp = _db_fingerprint()
                # last_fp двигается только после успешной пересборки, а та всегда
                # пишет index.json — значит при совпадении отпечатков файл на диске
                # есть, проверять exists() не нужно.
                if fp == last_fp:
                    return
                try:
                    count = build_index()
                    last_fp = fp
                    print(f"[serve] index пересобран ({count} отчётов)", file=sys.stderr)
                except Exception as exc:  # noqa: BLE001 — сервер не должен падать
                    # Битый ряд и т.п.: логируем, отдаём прежний index.json.
                    print(f"[serve] пересборка индекса не удалась: {exc}",
                          file=sys.stderr)

            def do_GET(self):
                self._maybe_rebuild()
                super().do_GET()

            def do_HEAD(self):
                self._maybe_rebuild()
                super().do_HEAD()

        handler = functools.partial(Handler, directory=str(docs_dir))
        # Только loopback: «локальный тестовый сервер» не должен торчать в сеть.
        # Однопоточный TCPServer обрабатывает запросы последовательно — гонок на
        # пересборку нет, Lock не нужен.
        with socketserver.TCPServer(("127.0.0.1", port), handler) as httpd:
            # Стартовая сборка после успешного bind: при занятом порте serve()
            # не создаёт и не удаляет чужой/старый index.json.
            build_index()
            owns_index_snapshot = True
            last_fp = _db_fingerprint()
            print(f"Тестовый сервер: http://localhost:{port}/  (данные из data/main.db)")
            print("Ctrl+C для остановки.")
            try:
                httpd.serve_forever()
            except KeyboardInterrupt:
                print("\nОстановлен.")
    finally:
        if owns_index_snapshot:
            _cleanup_index_snapshot(index_path)


def work_root_for(project: str, provider: str, model: str) -> Path:
    """Папка прогона: `data/result/<project>/<provider>_<model>/`. Единый источник
    правды для раскладки результатов — этот же путь использует скрипт миграции."""
    return WORK_ROOT / _sanitize(project) / f"{_sanitize(provider)}_{_sanitize(model)}"


def prepare_work_dirs(project: str, provider: str, model: str,
                      copies: int) -> list[Path]:
    """Создаёт папку прогона `data/result/<project>/<provider>_<model>/` и под ней
    N подпапок `<YYYYMMDD>-<HHMMSS>_<i>` — по одной на копию. Возвращает их пути
    (resolve). Папка проекта одна на проект, внутри — подпапки по провайдеру и
    модели (провайдер в имени снимает коллизию одной модели у разных провайдеров)."""
    run_root = work_root_for(project, provider, model)
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
    with _server_lock:
        _server_owners.clear()


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
    resolved_work_dir = work_dir.resolve()
    if _try_connect(port):
        with _server_lock:
            owner = _server_owners.get(port)
        if owner is not None:
            proc, owner_dir = owner
            if proc.poll() is None and owner_dir == resolved_work_dir:
                return True
        status(f"порт :{port} уже отвечает, но это не сервер текущей копии")
        return False

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
        _server_owners[port] = (proc, resolved_work_dir)

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


def _fetch_session_usage(http: httpx.Client, session_id: str, write: Writer) -> Usage | None:
    """Fallback: после idle перечитывает сообщения сессии и достаёт usage."""
    try:
        resp = http.get(f"/session/{session_id}/message", timeout=10.0)
    except Exception as exc:
        write(f"\n[usage: не удалось прочитать сообщения: {exc}]\n")
        return None
    if resp.status_code >= 400:
        write(f"\n[usage: GET /message вернул HTTP {resp.status_code}]\n")
        return None
    try:
        return extract_session_usage(resp.json())
    except Exception as exc:
        write(f"\n[usage: не удалось разобрать usage: {exc}]\n")
        return None


def _sse_reader(base: str, session_id: str, done: threading.Event,
                stop: threading.Event, result: dict, write: Writer) -> None:
    """Фон: читает GET /event через httpx-sse, фильтрует по нашей сессии,
    пишет прогресс через `write`, выставляет `done`, когда пришёл
    session.idle/session.error для нашей сессии.
    При ошибке кладёт текст в result["error"]."""
    try:
        with httpx.Client(timeout=None) as client:
            with httpx_sse.connect_sse(client, "GET", f"{base}/event") as source:
                for sse in source.iter_sse():
                    if stop.is_set():
                        return
                    try:
                        payload = json.loads(sse.data)
                    except (json.JSONDecodeError, TypeError):
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
        # stop выставлен — копия уже завершается (probe_session в finally),
        # её run.log вот-вот закроется (или уже закрыт). Ошибка чтения здесь
        # ожидаема (opencode рвёт SSE-стрим) и писать её в закрывающийся лог
        # нельзя — будет «I/O operation on closed file». Молча выходим.
        # Запись в лог — только когда копия ещё жива (guard коммита 1705030:
        # при выставленном stop run.log уже закрывается). А вот done.set()
        # вызываем безусловно: Event в лог не пишет, и это страхует от
        # «вечного ожидания» на done.wait(), если будущий код выставит stop
        # до его завершения.
        if not stop.is_set():
            result["error"] = f"SSE reader error: {exc}"
            try:
                write(f"\n[SSE reader error] {exc}\n")
            except Exception:
                pass
        done.set()


def probe_session(task: str, model: str, provider: str, agent: str, timeout: float,
                  port: int, write: Writer) -> SessionProbeResult:
    """Гоняет одну сессию и возвращает единый результат проверки.

    code: 0 — готово, 1 — таймаут, 2 — ошибка сессии.
    reason: человекочитаемая причина для code != 0 (из HTTP-тела, `_error_text`
    или файлового лога opencode), иначе None.
    usage: нормализованные токены OpenCode для успешной сессии, если провайдер
    их вернул.
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
        usage: Usage | None = None

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
                try:
                    payload = resp.json() or {}
                except Exception:
                    payload = {}
                usage = extract_usage_from_message(payload)
                # Ошибка модели/провайдера приходит в теле (HTTP 200, info.error)
                # ИЛИ как ненулевой HTTP-код. Не ждём session.idle — может не прийти.
                if resp.status_code >= 400:
                    write(f"\n--- ошибка ---\n[HTTP {resp.status_code}] {resp.text[:400]}\n")
                    reason = f"HTTP {resp.status_code}: {resp.text[:200].strip()}"
                    return SessionProbeResult(2, with_tail(reason), usage)
                info = payload.get("info", {}) if isinstance(payload, dict) else {}
                if isinstance(info, dict) and info.get("error"):
                    reason = _error_text(info)
                    write(f"\n--- ошибка ---\n[{reason}]\n")
                    return SessionProbeResult(2, with_tail(reason), usage)
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
                return SessionProbeResult(2, with_tail(reason), usage)
            if idle:
                if usage is None:
                    usage = _fetch_session_usage(http, session_id, write)
                write("\n--- готово ---\n")
                return SessionProbeResult(0, None, usage)
            # Таймаут: причина «зависания» (ретраи, 429) обычно лежит в файловом
            # логе opencode — её достаёт provider_error_tail().
            write("\n--- таймаут ---\n")
            tail = provider_error_tail()
            reason = f"нет ответа за {timeout:.0f}с"
            # При таймауте причина часто только в логе — приклеиваем первую строку.
            return SessionProbeResult(
                1,
                f"{reason} | {tail.splitlines()[0]}" if tail else reason,
                usage,
            )
        finally:
            # Основная защита от «I/O on closed file» — guard по stop в самом
            # _sse_reader (он не пишет в закрывающийся лог). join здесь —
            # вежливое завершение: у reader свой httpx.Client (timeout=None),
            # поэтому он висит в iter_sse() и завершится сам по следующему
            # событию (увидит stop) или по обрыву стрима opencode. Короткий
            # join даёт ему этот шанс, не задерживая закрытие копии; если не
            # успел — daemon=True не даст ему держать процесс.
            stop.set()
            reader.join(timeout=1.0)


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

    Возвращает результат-структуру: {index, port, dir, code, elapsed, usage}.
    Время `elapsed` меряется от входа в функцию (вкл. старт сервера) до выхода."""
    start = time.monotonic()
    label = f"copy {index}"
    status = _status_printer(label)
    rel = work_dir.relative_to(PROJECT_ROOT) if work_dir.is_relative_to(PROJECT_ROOT) else work_dir
    status(f"старт → {rel} (:{port})")

    def result(code: int, usage: Usage | None = None) -> dict:
        return {
            "index": index, "port": port, "dir": str(work_dir),
            "code": code, "elapsed": time.monotonic() - start,
            "usage": usage,
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

        try:
            session_result = probe_session(
                task=task, model=model, provider=provider, agent=agent,
                timeout=timeout, port=port, write=write,
            )
            rc = session_result.code
            usage = session_result.usage
        except Exception as exc:
            write("\n--- сбой копии ---\n")
            write("".join(traceback.format_exception(exc)))
            res = result(2)
            status(f"ошибка: {exc.__class__.__name__}: {exc} "
                   f"за {_fmt_secs(res['elapsed'])}")
            return res

    res = result(rc, usage)
    status(f"{_verdict(rc)} за {_fmt_secs(res['elapsed'])} "
           f"(лог: {log_path.relative_to(PROJECT_ROOT) if log_path.is_relative_to(PROJECT_ROOT) else log_path})")
    return res


def print_usage_report(results: list[dict], usage_summary: dict) -> None:
    print("--- отчёт по токенам ---")
    print(f"{'копия':<6} {'input':>12} {'output':>12} {'reason':>10} "
          f"{'total':>12} {'стоимость':>12}")
    for r in results:
        # main() нормализует r["usage"] через estimate_usage_cost до вызова —
        # здесь это всегда Usage или None, dict сюда не приходит.
        usage_obj = r.get("usage")
        usage = usage_obj.to_report_dict() if usage_obj else {}
        print(
            f"{r['index']:<6} "
            f"{format_tokens(usage.get('input_tokens')):>12} "
            f"{format_tokens(usage.get('output_tokens')):>12} "
            f"{format_tokens(usage.get('reasoning_tokens')):>10} "
            f"{format_tokens(usage.get('total_tokens')):>12} "
            f"{format_usd_cost(usage.get('estimated_cost_usd')):>12}"
        )
    print(f"токены всего:       {format_tokens(usage_summary.get('total_tokens'))}")
    print(f"стоимость всего:    {format_usd_cost(usage_summary.get('estimated_cost_usd'))}")


def main() -> None:
    # Подкоманда serve: локальный тестовый веб-сервер из базы. Разбираем её до
    # основного argparse, чтобы не ломать плоский вызов прогона
    # (`bench.py --project ... "задача"`).
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        sp = argparse.ArgumentParser(prog="bench.py serve",
                                     description="Локальный тестовый веб-сервер из data/main.db")
        sp.add_argument("--port", type=int, default=8000, help="Порт (default: 8000)")
        sargs = sp.parse_args(sys.argv[2:])
        serve(sargs.port)
        return

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

    if args.copies < 1:
        parser.error("--copies должно быть >= 1")

    # Источник задания: библиотека projects.json по --project — это умолчание.
    # Текст из CLI / --file перебивает библиотечный (для разовых экспериментов).
    project_entry = load_project(args.project)
    task = (
        args.file.read_text(encoding="utf-8") if args.file
        else args.task or (project_entry or {}).get("prompt")
    )
    if not task:
        parser.error(
            f"Нет задания: проект {args.project!r} не найден в projects.json "
            "и задача не указана в командной строке/--file"
        )
    description = (project_entry or {}).get("description")

    dirs = prepare_work_dirs(args.project, args.provider, args.model, args.copies)
    run_root = dirs[0].parent
    run_root_rel = run_root.relative_to(PROJECT_ROOT) if run_root.is_relative_to(PROJECT_ROOT) else run_root
    started_at = _dt.datetime.now()
    print(f"Запускаю {args.copies} копий: {args.provider}/{args.model}")
    print(f"Папка прогона: {run_root_rel}")
    print(f"Задание: {task.strip()[:80]}")
    print("--- старт ---")

    # Цена нужна только в конце, а её lookup может стучаться в сеть (протух кэш
    # каталога) — гоним параллельно прогону, не блокируя старт копий.
    run_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.copies + 1) as pool:
        pricing_future = pool.submit(get_pricing, args.provider, args.model)
        futures = [
            (
                pool.submit(
                    run_copy,
                    i + 1, work_dir, args.base_port + i, task,
                    args.model, args.provider, args.agent, args.timeout,
                ),
                i + 1,
                work_dir,
                args.base_port + i,
            )
            for i, work_dir in enumerate(dirs)
        ]
        results = []
        for future, index, work_dir, port in futures:
            try:
                results.append(future.result())
            except Exception as exc:
                log_path = work_dir / "run.log"
                try:
                    with log_path.open("a", encoding="utf-8") as log:
                        log.write("\n--- сбой future ---\n")
                        log.write("".join(traceback.format_exception(exc)))
                except OSError:
                    pass
                print(f"[copy {index}] ошибка future: {exc.__class__.__name__}: {exc}",
                      flush=True)
                results.append({
                    "index": index,
                    "port": port,
                    "dir": str(work_dir),
                    "code": 2,
                    "elapsed": time.monotonic() - run_start,
                    "usage": None,
                })
        # Меряем время прогона здесь: выход из `with` ждёт и pricing_future
        # (shutdown(wait=True)), и сетевой lookup цены раздул бы run_elapsed.
        run_elapsed = time.monotonic() - run_start

    # Получаем цену уже вне пула: сбой lookup'а не должен «съесть» отчёт по
    # завершившимся копиям и запись report.json.
    try:
        pricing = pricing_future.result()
    except Exception as exc:
        print(f"цена: не удалось получить ({exc})")
        pricing = {"prompt_per_1m": None, "completion_per_1m": None}

    results.sort(key=lambda r: r["index"])
    for r in results:
        r["usage"] = estimate_usage_cost(r.get("usage"), pricing)
    usage_summary = summarize_usages([r.get("usage") for r in results])

    codes = [r["code"] for r in results]
    elapsed = [r["elapsed"] for r in results]
    ok = codes.count(0)
    timeouts = codes.count(1)
    errors = sum(1 for c in codes if c >= 2)
    artifact_collection = collect_report_artifacts(results)

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
    print_usage_report(results, usage_summary)
    # Печатаем цену, «Free» и «N/A (пояснение)»; голое «N/A» без причины скрываем.
    if pricing.get("prompt_per_1m") is not None or pricing.get("note"):
        print(f"цена:               {format_price_display(pricing)}")
    print("--- сводка ---")
    print(f"{ok} готово / {timeouts} таймаут / {errors} ошибка (из {args.copies})")

    # Машиночитаемый отчёт.
    report = {
        "project": args.project,
        "model": args.model,
        "provider": args.provider,
        "prompt": task,
        "description": description,
        "copies": args.copies,
        "started_at": started_at.isoformat(),
        "run_elapsed": run_elapsed,
        "summary": {"ok": ok, "timeout": timeouts, "error": errors},
        "pricing": pricing,
        "usage_summary": usage_summary,
        "artifact_summary": artifact_collection.summary(),
        "runs": [
            {
                "index": r["index"], "port": r["port"], "dir": r["dir"],
                "status": _verdict(r["code"]), "code": r["code"],
                "elapsed": r["elapsed"],
                "usage": (
                    r["usage"].to_report_dict()
                    if isinstance(r.get("usage"), Usage) else None
                ),
            }
            for r in results
        ],
    }
    save_report(report, run_root, artifact_collection.artifacts)
    try:
        cleanup_collected_artifacts(artifact_collection)
    except Exception as exc:  # noqa: BLE001 — отчёт уже сохранён, прогон не роняем
        print(f"артефакты сохранены, но очистка диска не удалась: {exc}")
    print("Отчёт сохранён в базу: data/main.db")

    sys.exit(max(codes) if codes else 0)


if __name__ == "__main__":
    main()

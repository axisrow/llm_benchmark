"""Shared OpenCode runtime helpers for benchmark and model checks."""

import atexit
import errno
import json
import os
import re
import signal
import subprocess
import sys
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import httpx
import httpx_sse
from opencode_ai import Opencode

from db import PROJECT_ROOT
from usage import (
    Usage,
    extract_session_usage,
    extract_usage_from_message,
    field,
)

WORK_ROOT = PROJECT_ROOT / "data" / "result"
CONFIG_PATH = PROJECT_ROOT / "opencode.json"

DEFAULT_BASE_PORT = 4096
DEFAULT_MODEL = "glm-5.1"
DEFAULT_PROVIDER = "zai-coding-plan"
DEFAULT_AGENT = "bench_coder"
DEFAULT_COPIES = 5
SERVER_CHECK_TIMEOUT = 30
SERVER_CHECK_INTERVAL = 2
# POST /message - streaming request. It needs a short finite read-timeout even
# for long benchmark runs, otherwise the worker can sit inside http.post until
# the full run timeout and never notice SSE/log provider errors.
POST_MESSAGE_READ_TIMEOUT = 30.0
PROVIDER_LIMIT_LOG_POLL_INTERVAL = 2.0
# Дать SSE-reader потоку секунду на инициализацию перед отправкой сообщения.
SSE_READER_STARTUP_DELAY = 0.3

# Сервер/прокси может gracefully закрыть стрим GET /event (≈120с) задолго до конца
# бюджета прогона — БЕЗ финального session.idle/session.error. Тогда reader обязан
# переподключиться, а не молча выйти (иначе основной цикл досидит до deadline и
# выдаст ложный таймаут). Реконнект ограничен deadline прогона и счётчиком-страховкой.
SSE_RECONNECT_DELAY = 0.5      # пауза между переподключениями к /event
SSE_MAX_RECONNECTS = 1000      # страховка от busy-loop (реальный лимит — deadline)
SSE_EVENT_READ_TIMEOUT = 60.0  # read-timeout на сам GET /event (вместо None)

# Ретрай при лимите провайдера (HTTP 429 / rate limit). Паузы между попытками
# идут «сверх» --timeout прогона: каждая попытка получает свежий полный бюджет.
RATE_LIMIT_MAX_ATTEMPTS = 5          # всего попыток (1 исходная + 4 ретрая)
RATE_LIMIT_BACKOFF_BASE = 5.0        # первая пауза, сек
RATE_LIMIT_BACKOFF_FACTOR = 2.0      # 5 -> 10 -> 20 -> 40
RATE_LIMIT_BACKOFF_CAP = 60.0        # потолок паузы

Writer = Callable[[str], None]


@dataclass(frozen=True)
class SessionProbeResult:
    code: int
    reason: str | None = None
    usage: Usage | None = None
    # True = исход — лимит провайдера, обёртка probe_session может ретраить.
    rate_limited: bool = False


_CONNECT_NOT_READY_ERROR_NAMES = {
    "APIConnectionError",
    "APITimeoutError",
    "ConnectError",
    "ConnectTimeout",
    "PoolTimeout",
    "ReadTimeout",
    "TimeoutException",
    "WriteTimeout",
}

_PROVIDER_RETRYABLE_LIMIT_ERROR_MARKERS = (
    "http 429",
    "too many requests",
    "rate limit",
    "rate_limit",
    "usage limit",
    "quota",
)

_PROVIDER_PERMANENT_ACCOUNT_ERROR_MARKERS = (
    "requires a subscription",
    "upgrade for access",
    "upgrade for higher limits",
    "insufficient credit",
    "insufficient credits",
    "billing",
    "payment method",
)

_PROVIDER_LIMIT_ERROR_MARKERS = (
    _PROVIDER_RETRYABLE_LIMIT_ERROR_MARKERS
    + _PROVIDER_PERMANENT_ACCOUNT_ERROR_MARKERS
)


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def client_for_port(port: int) -> Opencode:
    return Opencode(base_url=base_url(port))


_server_processes: list[tuple[subprocess.Popen, Path]] = []
_server_owners: dict[int, tuple[subprocess.Popen, Path]] = {}
_server_lock = threading.Lock()
_print_lock = threading.Lock()


def sanitize_name(name: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    cleaned = re.sub(r"\.{2,}", ".", cleaned).strip("-.")
    return cleaned or "x"


def work_root_for(project: str, provider: str, model: str) -> Path:
    return WORK_ROOT / sanitize_name(project) / f"{sanitize_name(provider)}_{sanitize_name(model)}"


def prepare_work_dirs(project: str, provider: str, model: str,
                      copies: int) -> list[Path]:
    run_root = work_root_for(project, provider, model)
    run_root.mkdir(parents=True, exist_ok=True)

    # Создаём .git-границу в WORK_ROOT, чтобы opencode не поднимался
    # до корня репозитория бенчмарка при поиске git-root. Без этого
    # агент пишет артефакты в корень репо вместо изолированной папки.
    _git_boundary = WORK_ROOT / ".git"
    if not _git_boundary.exists():
        _git_boundary.write_text(
            "gitdir: /dev/null\n",
            encoding="utf-8",
        )

    stamp = time.strftime("%Y%m%d-%H%M%S")
    dirs: list[Path] = []
    for i in range(1, copies + 1):
        copy_dir = run_root / f"{stamp}_{i}"
        try:
            copy_dir.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            copy_dir = run_root / f"{stamp}_{i}_{int(time.monotonic() * 1000) % 100000}"
            copy_dir.mkdir(parents=True, exist_ok=True)
        dirs.append(copy_dir.resolve())
    return dirs


# Файлы/каталоги в корне проекта, которые cleanup_leaked_artifacts считает
# нормальными (кодовая база + типичные .gitignore-паттерны). При добавлении
# новых файлов в корень проекта — добавить сюда, иначе функция сочтёт их
# утечкой; рассинхрон с *.py ловит тест
# test_safe_names_covers_real_repo_root_modules.
_SAFE_ROOT_NAMES = {
    ".git", ".github", "__pycache__", "data",
    ".gitignore", "CLAUDE.md", "AGENTS.md", "LICENSE",
    "README.md", "pyproject.toml", "pytest.ini", "requirements.txt",
    "bench.py", "benchmark_report.py", "opencode_runtime.py",
    "db.py", "pricing.py", "usage.py", "artifacts.py",
    "dashboard_server.py", "check_models.py", "index_builder.py",
    "opencode.json", "model_catalog.py", "utils.py",
    "docs", "tests", "scripts",
    ".claude", ".python-version", ".pytest_cache", ".ruff_cache",
}


def cleanup_leaked_artifacts(project_root: Path,
                             work_dirs: list[Path]) -> list[Path]:
    """Обнаруживает артефакты агента, «утёкшие» за пределы work_dirs.

    Возвращает список путей (файлов/каталогов) в project_root, которые
    не входят ни в один из work_dirs и не являются ожидаемыми файлами
    репозитория (.git, __pycache__, data/, *.pyc и т.п.).
    """
    leaked: list[Path] = []
    resolved_work_dirs = {wd.resolve() for wd in work_dirs}

    for entry in project_root.iterdir():
        if entry.name in _SAFE_ROOT_NAMES:
            continue
        # .pyc-файлы в корне — тоже не утечка
        if entry.name.endswith(".pyc"):
            continue
        if entry.resolve() in resolved_work_dirs:
            continue
        if any(entry.resolve().is_relative_to(wd) for wd in resolved_work_dirs):
            continue
        leaked.append(entry)
    return leaked


def stop_servers() -> None:
    with _server_lock:
        procs = list(_server_processes)
    for proc, _log_path in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc, log_path in procs:
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    pass
        try:
            log_path.unlink()
        except OSError:
            pass
    with _server_lock:
        _server_processes.clear()
        _server_owners.clear()


atexit.register(stop_servers)


def _handle_shutdown_signal(signum: int, frame: object) -> None:
    stop_servers()
    if signum == signal.SIGINT:
        raise KeyboardInterrupt
    raise SystemExit(128 + signum)


def install_shutdown_handlers() -> None:
    """Перехват SIGTERM/SIGINT для гашения серверов. Зовётся из точки входа."""
    signal.signal(signal.SIGTERM, _handle_shutdown_signal)
    signal.signal(signal.SIGINT, _handle_shutdown_signal)


def _try_connect(port: int) -> bool:
    try:
        client_for_port(port).session.list()
        return True
    except ConnectionError:
        return False
    except TimeoutError:
        return False
    except httpx.TimeoutException:
        return False
    except Exception as exc:
        if exc.__class__.__name__ in _CONNECT_NOT_READY_ERROR_NAMES:
            return False
        raise


def ensure_server_running(work_dir: Path, port: int, status: Writer) -> bool:
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
    try:
        proc = subprocess.Popen(
            ["opencode", "serve", "--port", str(port)],
            stdout=subprocess.DEVNULL,
            stderr=stderr_file,
            cwd=str(work_dir),
            env=env,
        )
    except Exception as exc:
        stderr_file.close()
        try:
            stderr_path.unlink()
        except OSError:
            pass
        status(f"не удалось запустить opencode serve: {exc}")
        return False
    finally:
        if not stderr_file.closed:
            stderr_file.close()
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
    props = payload.get("properties", payload)
    if not isinstance(props, dict):
        return None
    info = props.get("info")
    if isinstance(info, dict):
        sid = info.get("sessionID") or info.get("id")
        if isinstance(sid, str) and sid.startswith("ses_"):
            return sid
    sid = props.get("sessionID")
    if isinstance(sid, str):
        return sid
    return None


def _format_event(payload: dict) -> str | None:
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
        return None

    if etype == "tool.execute.before":
        return f"\n[tool start: {props.get('tool', '?')}]"
    if etype == "tool.execute.after":
        return f"\n[tool done: {props.get('tool', '?')}]"
    if etype == "session.error":
        return f"\n[SESSION ERROR] {json.dumps(props, ensure_ascii=False)[:300]}"
    return None


OPENCODE_LOG_DIR = Path.home() / ".local" / "share" / "opencode" / "log"


def _is_provider_limit_error(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _PROVIDER_LIMIT_ERROR_MARKERS)


def _is_retryable_limit_error(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _PROVIDER_RETRYABLE_LIMIT_ERROR_MARKERS)


def _rate_limit_backoff(attempt: int) -> float:
    """Пауза перед повтором: attempt 1 -> 5с, 2 -> 10, 3 -> 20, 4 -> 40 (потолок 60)."""
    delay = RATE_LIMIT_BACKOFF_BASE * (RATE_LIMIT_BACKOFF_FACTOR ** (attempt - 1))
    return min(delay, RATE_LIMIT_BACKOFF_CAP)


def _message_post_timeout(deadline: float | None, now: float) -> float:
    if deadline is None:
        return POST_MESSAGE_READ_TIMEOUT
    remaining = deadline - now
    if remaining <= 0:
        return 0
    return min(POST_MESSAGE_READ_TIMEOUT, remaining)


def _decode_json_string_field(raw: str, field: str) -> str | None:
    match = re.search(fr'"{re.escape(field)}":"((?:\\.|[^"\\])*)"', raw)
    if not match:
        return None
    encoded = match.group(1)
    try:
        return json.loads(f'"{encoded}"')
    except json.JSONDecodeError:
        return encoded


def _short_error_detail(text: str, limit: int = 180) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit - 1] + "…"


# Шаблоны секрето-/PII-подобных фрагментов, которые нельзя выпускать в публичный
# отчёт. Полный текст причины при этом остаётся в приватном run.log.
_SECRET_PATTERNS = (
    re.compile(r"[Bb]earer\s+\S+"),                 # Bearer <token>
    re.compile(r"\b(?:sk|key|pk|tok|ghp|xoxb)[-_][A-Za-z0-9\-_]{6,}"),  # api keys
    re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),  # email
    re.compile(r"https?://\S+"),                    # URL (могут нести query/токены)
    re.compile(r"\b[A-Za-z0-9_\-]{20,}\b"),         # длинные токено-подобные строки
)
_LOCAL_REASON_PREFIXES = (
    "сбой ",
    "opencode serve не поднялся",
)


def _scrub_secrets(text: str) -> str:
    """Вырезает секрето-/PII-подобные фрагменты, заменяя их на «[скрыто]»."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[скрыто]", text)
    return text


def _is_account_error(text: str) -> bool:
    lowered = text.lower()
    return any(m in lowered for m in _PROVIDER_PERMANENT_ACCOUNT_ERROR_MARKERS)


def public_reason(reason: str | None) -> str | None:
    """Санирует причину исхода для ПУБЛИЧНОГО отчёта (raw_json → дашборд).

    Полная причина (с сырым телом провайдера и tail логов) остаётся в приватном
    run.log. Наружу отдаём безопасный каркас: HTTP-код + распознанная категория, а
    для нераспознанного — короткий хвост со скрабингом секретов/PII. Если категория
    не ясна и текст подозрителен — отдаём только код/«ошибка провайдера», не сырьё.
    """
    if not reason:
        return None
    if reason.startswith(_LOCAL_REASON_PREFIXES):
        # Локальная инфраструктурная причина (запуск сервера, future, crash) не
        # является телом провайдера; проверяем её до keyword-классификации, чтобы
        # случайные слова вроде forbidden в пути не стали «ошибкой авторизации».
        return _short_error_detail(_scrub_secrets(reason), limit=120)

    # Таймаут-причины («нет ответа за 60с …») не содержат тела провайдера — но в
    # хвост мог попасть provider-tail, поэтому всё равно скрабим.
    code_match = re.search(r"HTTP\s+(\d+)", reason)
    code = code_match.group(1) if code_match else None
    prefix = f"HTTP {code}" if code else None

    if code in ("401", "403") or "unauthorized" in reason.lower() \
            or "forbidden" in reason.lower():
        return f"{prefix}: ошибка авторизации" if prefix else "ошибка авторизации"
    if _is_retryable_limit_error(reason):
        return f"{prefix}: превышен лимит/квота" if prefix else "превышен лимит/квота"
    if _is_account_error(reason):
        return f"{prefix}: проблема аккаунта/биллинга" if prefix \
            else "проблема аккаунта/биллинга"
    if reason.startswith("нет ответа"):
        # Чистый таймаут без provider-текста — оставляем как есть; но если к нему
        # приклеен tail (через " | "), берём только безопасную головную часть.
        return _scrub_secrets(reason.split(" | ", 1)[0])

    # Категория не распознана. Если есть HTTP-код, НЕ публикуем тело провайдера:
    # скраббер не является allowlist и не должен решать, какие поля безопасны.
    if prefix:
        return f"{prefix}: ошибка провайдера"
    return "ошибка провайдера"


def _response_body_error(raw: str) -> str | None:
    body = _decode_json_string_field(raw, "responseBody")
    if not body:
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return _short_error_detail(body)
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, str):
            return _short_error_detail(err)
        if isinstance(err, dict):
            msg = err.get("message") or err.get("error") or err.get("name")
            if isinstance(msg, str):
                return _short_error_detail(msg)
        msg = payload.get("message") or payload.get("detail")
        if isinstance(msg, str):
            return _short_error_detail(msg)
    return _short_error_detail(body)


def _log_line_has_agent(raw: str, agent: str) -> bool:
    pattern = rf"(?<!\S)agent={re.escape(agent)}(?=\s|$)"
    return re.search(pattern, raw) is not None


def _opencode_error_tail(session_id: str, lines: int = 8, *,
                         agent: str | None = None) -> str | None:
    try:
        log_files = sorted(OPENCODE_LOG_DIR.glob("*.log"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as exc:
        # Не смогли прочитать каталог provider-логов: причина account/provider
        # ошибки деградирует до обычного timeout. Оставляем след, чтобы это было
        # видно, а не выглядело как «в логах ничего нет».
        print(f"[opencode] не удалось прочитать каталог логов "
              f"{OPENCODE_LOG_DIR}: {exc}", file=sys.stderr)
        return None

    found: list[str] = []
    unread: list[str] = []
    for log_file in log_files:
        try:
            with log_file.open(errors="replace") as fh:
                for raw in fh:
                    raw = raw.rstrip("\n")
                    if not raw.startswith("ERROR") or session_id not in raw:
                        continue
                    if agent is not None and not _log_line_has_agent(raw, agent):
                        continue
                    status = re.search(r'statusCode["\s:=]+(\d+)', raw)
                    err_name = re.search(r'"name":"([^"]+)"', raw)
                    detail = re.search(r'"message":"([^"]{0,160})"', raw)
                    response_error = _response_body_error(raw)
                    parts = []
                    if status:
                        parts.append(f"HTTP {status.group(1)}")
                    if err_name:
                        parts.append(err_name.group(1))
                    detail_text = detail.group(1) if detail else None
                    if "Too Many Requests" in raw and not (
                        detail_text and "Too Many Requests" in detail_text
                    ):
                        parts.append("Too Many Requests")
                    if detail_text:
                        parts.append(detail_text)
                    if response_error and response_error not in parts:
                        parts.append(response_error)
                    summary = " | ".join(parts) if parts else raw[:200]
                    if summary not in found:
                        found.append(summary)
        except OSError as exc:
            # Конкретный лог-файл не открылся (удалён/нет прав) — копим, чтобы не
            # спамить stderr на каждый файл в цикле; сообщим один раз ниже.
            unread.append(f"{log_file}: {exc}")
            continue
        if found:
            break
    # Логируем пропущенные файлы один раз и только если причину так и не нашли:
    # иначе провайдерская причина могла потеряться именно в нечитаемом логе.
    if unread and not found:
        print(f"[opencode] не удалось прочитать логи ({len(unread)}): "
              f"{'; '.join(unread)}", file=sys.stderr)
    if not found:
        return None
    return "\n".join(found[-lines:])


def _error_text(props: dict) -> str:
    err = props.get("error") or {}
    if isinstance(err, str):
        return err
    if not isinstance(err, dict):
        return "?"
    data = err.get("data") or {}
    msg = data.get("message") or err.get("message") or err.get("name") or "?"
    code = data.get("statusCode")
    return f"{msg}" + (f" (HTTP {code})" if code else "")


def _fetch_session_usage(http: httpx.Client, session_id: str, write: Writer) -> Usage | None:
    # Возвращаем None при любой проблеме (успешный прогон не падает из-за usage),
    # но дублируем причину на stderr: при недоступном run.log она иначе пропала бы,
    # а успешный run молча получил бы N/A по токенам.
    def note(msg: str) -> None:
        write(f"\n[{msg}]\n")
        print(f"[usage] {msg}", file=sys.stderr)

    try:
        resp = http.get(f"/session/{session_id}/message", timeout=10.0)
    except Exception as exc:
        note(f"usage: не удалось прочитать сообщения: {exc}")
        return None
    if resp.status_code >= 400:
        note(f"usage: GET /message вернул HTTP {resp.status_code}")
        return None
    try:
        return extract_session_usage(resp.json())
    except Exception as exc:
        note(f"usage: не удалось разобрать usage: {exc}")
        return None


def _safe_write(write: Writer, msg: str) -> None:
    """write может бросить, если лог уже закрыт (поток-reader живёт дольше)."""
    try:
        write(msg)
    except Exception:
        # Молчим осознанно: единственный потребитель этого сообщения — уже
        # закрытый run.log; гнать ошибку некуда и она не диагностична.
        pass


def _session_looks_idle(base: str, session_id: str, write: Writer) -> bool:
    """True, если последнее assistant-сообщение сессии завершено (time.completed).

    Используется когда SSE-стрим закрылся штатно, чтобы не пропустить
    session.idle, случившийся в окне между закрытием и переподключением.
    Консервативно: при любой неоднозначности возвращает False (→ реконнект),
    чтобы никогда не выдать ещё работающую сессию за ложный успех.
    """
    try:
        with httpx.Client(base_url=base, timeout=10.0) as http:
            resp = http.get(f"/session/{session_id}/message")
        if resp.status_code >= 400:
            return False
        messages = resp.json()
    except Exception as exc:
        # Консервативно False (→ реконнект), но оставляем след в обоих каналах:
        # иначе ошибка доступа к сессии неотличима от штатного «ещё работает».
        _safe_write(write, f"\n[idle-check: не удалось проверить сессию: {exc}]\n")
        print(f"[idle-check] не удалось проверить сессию: {exc}", file=sys.stderr)
        return False
    if not isinstance(messages, list) or not messages:
        return False
    for entry in reversed(messages):
        info = field(entry, "info")
        if info is None:
            info = entry
        if field(info, "role") != "assistant":
            continue
        # Завершено с ошибкой — не «idle-успех»: потерянный session.error нельзя
        # выдать за code 0; основной цикл поднимет причину через provider-tail.
        if field(info, "error"):
            return False
        time_info = field(info, "time") or {}
        # сессия закончила работу: последнее assistant-сообщение завершено.
        return bool(field(time_info, "completed"))
    return False


def _sse_reader(base: str, session_id: str, done: threading.Event,
                stop: threading.Event, result: dict, write: Writer,
                deadline: float | None = None) -> None:
    reconnects = 0
    while not stop.is_set():
        if deadline is not None and time.monotonic() >= deadline:
            # Бюджет исчерпан — пусть основной цикл вынесет честный таймаут.
            return
        try:
            sse_timeout = httpx.Timeout(
                connect=10.0, read=SSE_EVENT_READ_TIMEOUT, write=10.0, pool=10.0)
            with httpx.Client(timeout=sse_timeout) as client:
                with httpx_sse.connect_sse(client, "GET", f"{base}/event") as source:
                    for sse in source.iter_sse():
                        if stop.is_set():
                            return
                        try:
                            payload = json.loads(sse.data)
                        except (json.JSONDecodeError, TypeError):
                            # Служебные/keepalive SSE-кадры без JSON — пропускаем
                            # осознанно; настоящие ошибки сессии приходят
                            # отдельным session.error и логируются ниже.
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
                    # iter_sse исчерпан ШТАТНО без финального события сессии.
        except Exception as exc:
            # Сетевой обрыв соединения — это ошибка. Реконнектим, пока есть
            # бюджет; если бюджет исчерпан или слишком много обрывов подряд —
            # фиксируем ошибку (битый SSE != молчаливый таймаут).
            if stop.is_set():
                return
            # session.idle мог прийтись на окно обрыва (или тихий период до
            # ReadTimeout) — проверяем статус сессии, как и при graceful-close,
            # иначе завершившийся прогон превратится в ложный таймаут/ошибку.
            if _session_looks_idle(base, session_id, write):
                done.set()
                return
            reconnects += 1
            # Если до дедлайна не успеем переподключиться — нет смысла ждать,
            # фиксируем ошибку сразу (битый SSE != молчаливый таймаут).
            no_budget_left = (deadline is not None
                              and deadline - time.monotonic() <= SSE_RECONNECT_DELAY)
            if reconnects > SSE_MAX_RECONNECTS or no_budget_left:
                result["error"] = f"SSE reader error: {exc}"
                _safe_write(write, f"\n[SSE reader error] {exc}\n")
                done.set()
                return
            _safe_write(write, f"\n[SSE: соединение оборвалось ({exc}), переподключаюсь]\n")
            stop.wait(SSE_RECONNECT_DELAY)
            continue

        # --- штатное закрытие стрима сервером без session.idle/session.error ---
        # Это НЕ ошибка: стрим GET /event — глобальная шина, сервер может его
        # gracefully закрыть, пока сессия ещё работает. Реконнектим, пока есть
        # бюджет; при исчерпании бюджета/лимита реконнектов просто выходим молча,
        # чтобы основной цикл вынес ЧЕСТНЫЙ таймаут (а не подменяем его ошибкой).
        if stop.is_set() or done.is_set():
            return
        _safe_write(write, "\n[SSE: сервер закрыл /event без session.idle, "
                           "проверяю статус сессии и переподключаюсь]\n")
        if _session_looks_idle(base, session_id, write):
            done.set()
            return
        reconnects += 1
        if reconnects > SSE_MAX_RECONNECTS:
            return
        if deadline is not None and time.monotonic() >= deadline:
            return
        stop.wait(SSE_RECONNECT_DELAY)


def probe_session(task: str, model: str, provider: str, agent: str, timeout: float,
                  port: int, write: Writer) -> SessionProbeResult:
    """Гоняет сессию агента, ретраит при лимите провайдера с backoff.

    Каждая попытка получает свежий полный бюджет `timeout` (паузы между
    попытками идут «сверх» него). После исчерпания ретраев — отдельный
    статус «лимит» (code=3), а не обычная «ошибка».
    """
    # Цикл всегда делает ≥1 итерацию (RATE_LIMIT_MAX_ATTEMPTS >= 1), а выйти из
    # него без return можно лишь через rate_limited-результат → `last` тут не None.
    last = None
    for attempt in range(1, RATE_LIMIT_MAX_ATTEMPTS + 1):
        res = _probe_session_once(task, model, provider, agent, timeout, port, write)
        if not res.rate_limited:
            return res
        last = res
        if attempt < RATE_LIMIT_MAX_ATTEMPTS:
            delay = _rate_limit_backoff(attempt)
            write(f"\n[rate limit] попытка {attempt}/{RATE_LIMIT_MAX_ATTEMPTS} "
                  f"упёрлась в лимит провайдера, жду {delay:.0f}с и повторяю...\n")
            time.sleep(delay)
    write("\n--- лимит провайдера: retry исчерпан ---\n")
    return SessionProbeResult(3, last.reason, last.usage)


def _probe_session_once(task: str, model: str, provider: str, agent: str,
                        timeout: float, port: int, write: Writer) -> SessionProbeResult:
    base = base_url(port).rstrip("/")
    deadline = None if timeout <= 0 else time.monotonic() + timeout

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
            args=(base, session_id, done, stop, result, write, deadline),
            daemon=True,
        )
        reader.start()
        time.sleep(SSE_READER_STARTUP_DELAY)
        usage: Usage | None = None

        def provider_error_tail() -> str | None:
            tail = (_opencode_error_tail(session_id, agent=agent)
                    or _opencode_error_tail(session_id))
            if tail:
                write("\n--- ошибки провайдера из лога opencode ---\n"
                      f"{tail}\n")
            return tail

        def provider_limit_tail() -> str | None:
            tail = _opencode_error_tail(session_id, agent=agent)
            if not tail or not _is_provider_limit_error(tail):
                return None
            write("\n--- лимит провайдера из лога opencode ---\n"
                  f"{tail}\n")
            return tail

        def with_tail(reason: str) -> str:
            tail = provider_error_tail()
            if not tail:
                return reason
            first_line = tail.splitlines()[0]
            sig = max(first_line.split(" | "), key=len).strip()
            if sig and sig in reason:
                return reason
            return f"{reason} | {first_line}"

        body = {
            "agent": agent,
            "model": {"providerID": provider, "modelID": model},
            "parts": [{"type": "text", "text": task}],
        }

        try:
            post_timeout = _message_post_timeout(deadline, time.monotonic())
            if post_timeout > 0:
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
                        # Битое тело ответа не теряет причину: при HTTP>=400 reason
                        # ниже берётся из status_code/resp.text, не из payload.
                        payload = {}
                    usage = extract_usage_from_message(payload)
                    if resp.status_code >= 400:
                        write(f"\n--- ошибка ---\n[HTTP {resp.status_code}] {resp.text[:400]}\n")
                        reason = f"HTTP {resp.status_code}: {resp.text[:200].strip()}"
                        tailed = with_tail(reason)
                        is_limit = (resp.status_code == 429
                                    or _is_retryable_limit_error(tailed))
                        return SessionProbeResult(2, tailed, usage, rate_limited=is_limit)
                    info = payload.get("info", {}) if isinstance(payload, dict) else {}
                    if isinstance(info, dict) and info.get("error"):
                        reason = with_tail(_error_text(info))
                        is_limit = _is_retryable_limit_error(reason)
                        write(f"\n--- ошибка ---\n[{reason}]\n")
                        return SessionProbeResult(2, reason, usage, rate_limited=is_limit)
                except httpx.ReadTimeout:
                    waited = time.monotonic() - post_start
                    write(f"\n[POST /message не ответил за {waited:.1f}с — "
                          "продолжаем ждать события до дедлайна]\n")

            idle = False
            while True:
                if result.get("error"):
                    break
                if done.is_set():
                    idle = True
                    break
                limit_tail = provider_limit_tail()
                if result.get("error"):
                    break
                if done.is_set():
                    idle = True
                    break
                if limit_tail:
                    first_line = limit_tail.splitlines()[0]
                    is_limit = _is_retryable_limit_error(first_line)
                    label = "provider limit" if is_limit else "provider error"
                    reason = f"{label} | {first_line}"
                    write(f"\n--- ошибка ---\n[{reason}]\n")
                    return SessionProbeResult(2, reason, usage, rate_limited=is_limit)

                remaining = None
                if deadline is not None:
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        break

                wait_for = PROVIDER_LIMIT_LOG_POLL_INTERVAL
                if remaining is not None:
                    wait_for = min(wait_for, remaining)
                if done.wait(timeout=wait_for):
                    idle = True
                    break

            if result.get("error"):
                reason = result["error"]
                write(f"\n--- ошибка ---\n[{reason}]\n")
                tailed = with_tail(reason)
                return SessionProbeResult(
                    2, tailed, usage,
                    rate_limited=_is_retryable_limit_error(tailed),
                )
            if idle:
                full_usage = _fetch_session_usage(http, session_id, write)
                if full_usage is not None:
                    usage = full_usage
                write("\n--- готово ---\n")
                return SessionProbeResult(0, None, usage)

            write("\n--- таймаут ---\n")
            tail = provider_error_tail()
            reason = ("нет ответа" if deadline is None
                      else f"нет ответа за {timeout:.0f}с")
            return SessionProbeResult(
                1,
                f"{reason} | {tail.splitlines()[0]}" if tail else reason,
                usage,
            )
        finally:
            stop.set()
            reader.join(timeout=1.0)


def status_printer(label: str) -> Writer:
    def emit(msg: str) -> None:
        with _print_lock:
            try:
                print(f"[{label}] {msg}", flush=True)
            except BrokenPipeError:
                return
            except OSError as exc:
                if exc.errno == errno.EPIPE:
                    return
                raise
    return emit


# Единый источник правды по кодам исхода прогона: code -> (ключ summary, русский ярлык).
# Любой новый код исхода добавляется только здесь; summary в benchmark_report,
# verdict() и regenerate_raw_json берут таксономию отсюда.
RUN_CODES: dict[int, tuple[str, str]] = {
    0: ("ok", "готово"),
    1: ("timeout", "таймаут"),
    2: ("error", "ошибка"),
    3: ("rate_limited", "лимит"),
}
_VERDICT = {code: label for code, (_key, label) in RUN_CODES.items()}


def fmt_secs(seconds: float) -> str:
    return f"{seconds:.1f}с"


def rel_to_root(path: Path, root: Path = PROJECT_ROOT) -> Path:
    """Путь относительно `root`, либо сам `path`, если он вне корня."""
    return path.relative_to(root) if path.is_relative_to(root) else path


def verdict(code: int) -> str:
    return _VERDICT.get(code, f"код {code}")

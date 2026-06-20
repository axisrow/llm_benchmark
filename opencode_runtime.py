"""Shared OpenCode runtime helpers for benchmark and model checks."""

import atexit
import errno
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import IO

import httpx

from db import PROJECT_ROOT
# Ре-экспорт классификации ошибок (issue #53). opencode_errors — листовой модуль
# (не импортирует runtime), поэтому тянем его сверху без цикла. Имена остаются
# доступны как opencode_runtime.X для потребителей (public_reason) и тестов.
from opencode_errors import (  # noqa: F401
    OPENCODE_LOG_DIR,
    _decode_json_string_field,
    _is_account_error,
    _is_provider_limit_error,
    _is_retryable_limit_error,
    _log_line_has_agent,
    _opencode_error_tail,
    _response_body_error,
    _scrub_secrets,
    _short_error_detail,
    public_reason,
)
# Чистые утилиты вынесены в utils (issue #53); ре-экспорт — потребители тянут
# sanitize_name/fmt_secs из opencode_runtime, work_root_for ниже зовёт sanitize_name.
from utils import fmt_secs, sanitize_name  # noqa: F401
# Базовые примитивы вынесены в opencode_base (issue #53): тип результата сессии,
# Writer, соединение, константы-настройки. base — ЛИСТ (не тянет runtime),
# поэтому импорт сверху без цикла; ре-экспорт сохраняет opencode_runtime.X для
# потребителей и тестов (а также для session-функций, пока живущих в runtime).
from opencode_base import (  # noqa: F401
    POST_MESSAGE_READ_TIMEOUT,
    PROVIDER_LIMIT_LOG_POLL_INTERVAL,
    RATE_LIMIT_BACKOFF_BASE,
    RATE_LIMIT_BACKOFF_CAP,
    RATE_LIMIT_BACKOFF_FACTOR,
    RATE_LIMIT_MAX_ATTEMPTS,
    SSE_EVENT_READ_TIMEOUT,
    SSE_IDLE_CHECK_TIMEOUT,
    SSE_MAX_RECONNECTS,
    SSE_READER_STARTUP_DELAY,
    SSE_RECONNECT_DELAY,
    SessionProbeResult,
    Writer,
    base_url,
    client_for_port,
)
# SSE/сессии вынесены в opencode_session (issue #53). session импортирует base/
# errors (НЕ runtime), поэтому цикла нет и ре-экспорт держим сверху. Имена остаются
# доступны как opencode_runtime.X для потребителей (probe_session) и прямых вызовов
# в тестах (runtime._exit_state/_wait_for_session и т.п.).
from opencode_session import (  # noqa: F401
    _error_text,
    _exit_state,
    _extract_session_id,
    _fetch_session_usage,
    _format_event,
    _message_post_timeout,
    _probe_session_once,
    _rate_limit_backoff,
    _safe_write,
    _session_looks_idle,
    _sse_reader,
    _wait_for_session,
    probe_session,
)
# Usage ре-экспортируется (benchmark_report и тесты тянут его из opencode_runtime).
from usage import Usage  # noqa: F401

WORK_ROOT = PROJECT_ROOT / "data" / "result"
CONFIG_PATH = PROJECT_ROOT / "opencode.json"

DEFAULT_BASE_PORT = 4096
DEFAULT_MODEL = "glm-5.1"
DEFAULT_PROVIDER = "zai-coding-plan"
DEFAULT_AGENT = "bench_coder"
DEFAULT_COPIES = 5
SERVER_CHECK_TIMEOUT = 30
SERVER_CHECK_INTERVAL = 2


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

_server_processes: list[tuple[subprocess.Popen, Path]] = []
_server_owners: dict[int, tuple[subprocess.Popen, Path]] = {}
_server_lock = threading.Lock()
_print_lock = threading.Lock()


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


def cleanup_leaked_artifacts(project_root: Path,
                             work_dirs: list[Path]) -> list[Path]:
    """Обнаруживает артефакты агента, «утёкшие» за пределы work_dirs.

    «Утечка» — любой untracked или модифицированный путь в git-дереве
    `project_root`, не лежащий ни в одном work_dir и не внутри служебного
    каталога `data/` (туда бенчмарк сам пишет main.db и складывает прогоны).

    Опора на `git status --porcelain` вместо ручного allowlist (ср. issue #42,
    источник рассинхронов) даёт два выигрыша: автоматически учитывается
    .gitignore (`__pycache__/`, кэши инструментов, `data/result/*` отсекаются
    сами), и видны записи ВГЛУБЬ любых каталогов — `tests/`, `docs/`, особенно
    `.github/workflows/` (незамеченный файл там = потенциальная CI-инъекция),
    чего обход верхнего уровня корня не ловил.

    Если `project_root` — не git-репозиторий или git недоступен, возвращает
    пустой список: детектор — лишь вторая линия обороны (первичная — .git-граница
    в WORK_ROOT + `external_directory: deny`), а без git честно судить о «лишних»
    путях нельзя.
    """
    resolved_work_dirs = {wd.resolve() for wd in work_dirs}
    # data/main.db трекается и штатно переписывается самим бенчмарком после
    # прогона — его модификация НЕ утечка. Сопутствующие WAL/SHM эфемерны и
    # gitignored. Всё ОСТАЛЬНОЕ под data/ (untracked/modified) — утечка: work_dir
    # агента лежит под data/result/*, так что побег изоляции вероятнее всего
    # приземлится именно сюда, и глотать весь data/ целиком — слепое пятно.
    allowed_paths = {
        (project_root / rel).resolve()
        for rel in ("data/main.db", "data/main.db-wal", "data/main.db-shm")
    }

    try:
        proc = subprocess.run(
            # -z: NUL-разделённый вывод без кавычек/экранирования — корректно
            # отдаёт пути с пробелами и не-ASCII (иначе git берёт их в кавычки).
            ["git", "-c", "core.quotePath=false", "status", "--porcelain", "-z"],
            cwd=str(project_root),
            capture_output=True, text=True, timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if proc.returncode != 0:
        return []  # не git-репозиторий / git недоступен

    # В -z записи разделены \0. Обычная запись: "XY <path>". Rename/copy (R/C)
    # занимает ДВА \0-поля: "R  <new>\0<old>" — берём <new> и пропускаем <old>.
    # Агент rename в индексе project_root не делает (работает офлайн в своей
    # папке без git-доступа), но обрабатываем честно, чтобы <old> не утёк в путь.
    fields = proc.stdout.split("\0")
    leaked: list[Path] = []
    i = 0
    while i < len(fields):
        entry = fields[i]
        i += 1
        if len(entry) < 4:
            continue
        status = entry[:2]
        rel_path = entry[3:]
        if status[0] in ("R", "C") or status[1] in ("R", "C"):
            i += 1  # пропустить поле <old> у переименования/копии
        candidate = (project_root / rel_path).resolve()
        if candidate in allowed_paths:
            continue
        if candidate in resolved_work_dirs:
            continue
        if any(candidate.is_relative_to(wd) for wd in resolved_work_dirs):
            continue
        leaked.append(project_root / rel_path)
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
                    # Процесс не reaped даже после SIGKILL (zombie/NFS). Это
                    # atexit-handler — падать нельзя, но след в stderr поможет
                    # при отладке зависшего shutdown.
                    print(f"[shutdown] процесс {proc.pid} не завершился даже "
                          "после SIGKILL", file=sys.stderr)
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


def locked_writer(fh: IO[str]) -> Writer:
    """Thread-safe `Writer` поверх открытого файла: пишет + flush под общим lock.

    Контракт `Writer` для probe_session: параллельные SSE-события и статусы пишут
    в один лог. Создаётся вокруг уже открытого файла внутри его `with`-блока
    (benchmark_report.run_copy, check_models.check_one)."""
    lock = threading.Lock()

    def write(msg: str) -> None:
        with lock:
            fh.write(msg)
            fh.flush()
    return write


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


def rel_to_root(path: Path, root: Path = PROJECT_ROOT) -> Path:
    """Путь относительно `root`, либо сам `path`, если он вне корня."""
    return path.relative_to(root) if path.is_relative_to(root) else path


def verdict(code: int) -> str:
    return _VERDICT.get(code, f"код {code}")


def summary_counts(codes: Iterable[int]) -> dict[str, int]:
    """Счётчики исходов по ключам RUN_CODES: `{'ok': n, 'timeout': n, ...}`.

    Единый построитель сводки рядом с RUN_CODES — writer (benchmark_report),
    check_models и regenerate берут таксономию отсюда, а не хардкодят коды.
    Новый код исхода в RUN_CODES автоматически появляется во всех сводках."""
    codes = list(codes)
    return {key: codes.count(code) for code, (key, _label) in RUN_CODES.items()}


def summary_line(counts: Mapping[str, int], *, total: int | None = None,
                 labels: Mapping[str, str] | None = None) -> str:
    """Человекочитаемая сводка `'N готово / N таймаут / ...'` по RUN_CODES.

    counts — по ключам RUN_CODES (из summary_counts). labels переопределяет
    ярлык отдельных ключей (check_models показывает 'доступно' вместо 'готово'
    для ok). total, если задан, добавляет хвост `(из N)`."""
    labels = labels or {}
    parts = " / ".join(
        f"{counts.get(key, 0)} {labels.get(key, label)}"
        for _code, (key, label) in RUN_CODES.items())
    return parts if total is None else f"{parts} (из {total})"

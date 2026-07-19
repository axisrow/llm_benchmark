"""Shared OpenCode runtime helpers for benchmark and model checks."""

import errno
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import IO

import httpx  # noqa: F401  (compatibility re-export for tests/consumers)
import httpx_sse  # noqa: F401  (compatibility re-export for tests/consumers)

from artifacts import (
    RUN_ACTIVE_MARKER,
    cleanup_abandoned_work_dirs,
    hold_marker_lock,
    write_run_active_marker,
)
from db import PROJECT_ROOT, list_run_dirs, session
# Ре-экспорт reaper'а осиротевших serve (issue #155). opencode_reaper — листовой
# модуль (тянет только artifacts/stdlib), поэтому импорт сверху цикла не даёт;
# потребители (bench.py, scripts/) зовут его как opencode_runtime.X.
from opencode_reaper import (  # noqa: F401
    ReapResult,
    ServeCandidate,
    reap_orphan_serves,
)
# Ре-экспорт классификации ошибок (issue #53). opencode_errors — листовой модуль
# (не импортирует runtime), поэтому тянем его сверху без цикла. Имена остаются
# доступны как opencode_runtime.X для потребителей (public_reason) и тестов.
from opencode_errors import (  # noqa: F401
    HUNG_POST_REASON,
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
    _capture_questions_and_abort,
    _error_text,
    _exit_state,
    _extract_session_id,
    _fetch_session_usage,
    _format_event,
    _message_post_timeout,
    _reply_to_question,
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
# Управление serve-процессами вынесено в opencode_process (issue #53). process
# импортирует base/db (НЕ runtime) → standalone, цикла нет. Ре-экспорт сохраняет
# opencode_runtime.X для потребителей (ensure_server_running/install_shutdown_handlers)
# и тестов (_server_processes/_server_owners мутируются как один объект).
from opencode_process import (  # noqa: F401
    _server_owners,
    _server_processes,
    ensure_server_running,
    install_shutdown_handlers,
    stop_server,
    stop_servers,
)

WORK_ROOT = PROJECT_ROOT / "data" / "result"

# Открытые файловые дескрипторы marker'ов под advisory-lock (issue #155).
# Список держит ссылки на время жизни процесса: закрытие файла отпустило бы
# flock, и reaper чужого bench.py счёл бы наши живые serve осиротевшими.
_marker_locks: list[IO[bytes]] = []

DEFAULT_BASE_PORT = 4096
DEFAULT_MODEL = "glm-5.1"
DEFAULT_PROVIDER = "zai-coding-plan"
DEFAULT_AGENT = "build"
DEFAULT_COPIES = 5

_print_lock = threading.Lock()


def find_free_port_range(n: int, start: int = DEFAULT_BASE_PORT) -> int:
    """Найти свободный диапазон из n sequential портов на 127.0.0.1.

    bind-проверяет каждый порт последовательно от start; SO_REUSEADDR=0,
    поэтому только реально свободные порты считаются доступными.

    Args:
        n: Число портов в диапазоне. Должно быть > 0.
        start: Стартовый порт для поиска (default=DEFAULT_BASE_PORT).

    Returns:
        Первый порт свободного диапазона.

    Raises:
        ValueError: Если n <= 0 или нет свободного диапазона до 65535.
    """
    if n <= 0:
        raise ValueError(f"n должно быть > 0, получено {n}")
    if start < 1 or start > 65535:
        raise ValueError(f"start должен быть в 1..65535, получено {start}")

    max_end = 65535 - n + 1
    if start > max_end:
        # Даже start уже слишком близко к концу для диапазона
        raise ValueError(f"Нет места для диапазона из {n} портов, начиная с {start}")

    # Проверяем каждое возможное начало диапазона
    for candidate in range(start, max_end + 1):
        range_free = True
        # Проверяем все порты диапазона candidate..candidate+n-1
        for offset in range(n):
            port = candidate + offset
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                    s.bind(("127.0.0.1", port))
            except OSError:
                # Порт занят, пробуем следующий кандидат
                range_free = False
                break
        if range_free:
            return candidate

    raise ValueError(f"Нет свободного диапазона из {n} портов, начиная с {start} (до 65535)")


def work_root_for(project: str, provider: str, model: str) -> Path:
    return WORK_ROOT / sanitize_name(project) / f"{sanitize_name(provider)}_{sanitize_name(model)}"


def _cleanup_abandoned_before_run() -> None:
    """Подмести старые orphan-каталоги, не затрагивая сохранённые прогоны."""
    try:
        with session() as conn:
            known = [Path(path) for path in list_run_dirs(conn)]
    except Exception as exc:
        print(f"warning: автоматическая очистка хвостов пропущена: {exc}",
              file=sys.stderr)
        return

    result = cleanup_abandoned_work_dirs(WORK_ROOT, known, apply=True)
    if result.removed:
        print(f"cleanup: удалено заброшенных work_dir: {len(result.removed)}")
    for error in result.errors:
        print(f"warning: cleanup хвостов: {error}", file=sys.stderr)


def prepare_work_dirs(project: str, provider: str, model: str,
                      copies: int) -> list[Path]:
    _cleanup_abandoned_before_run()
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
        try:
            marker = write_run_active_marker(copy_dir)
            # issue #155: держим advisory-lock на marker до конца процесса.
            # Ядро отпустит его САМО даже при SIGKILL, когда atexit/finally не
            # отрабатывают, — по свободному lock-у reaper отличит наш orphan от
            # копии живого параллельного bench.py. Ссылка хранится в
            # module-level списке: закрытие файла отпустило бы lock.
            handle = hold_marker_lock(marker)
            if handle is None:
                # hold_marker_lock не бросает исключений (ловит OSError внутри),
                # поэтому молчаливый None пропустил бы копию БЕЗ lock: reaper
                # увидел бы свободный lock, счёл копию осиротевшей и убил бы её
                # ЖИВОЙ serve. Fail-closed: нет lock — нет копии (ревью PR #157).
                raise RuntimeError(
                    "не удалось взять advisory-lock на marker "
                    f"{marker}: копия не запускается без защиты от reaper'а")
            _marker_locks.append(handle)
        except Exception:
            # Не запускаем копию без marker: через 24 часа другой процесс мог бы
            # принять её за orphan и удалить во время работы.
            # rmdir не годится: marker уже создан, а на непустом каталоге rmdir
            # молча падает в OSError — хвост оставался бы на диске (ревью #157).
            try:
                marker_path = copy_dir / RUN_ACTIVE_MARKER
                marker_path.unlink(missing_ok=True)
                copy_dir.rmdir()
            except OSError:
                pass
            raise
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

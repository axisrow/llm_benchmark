"""Управление процессами `opencode serve` (issue #53).

Подъём/учёт/гашение serve-процессов: запуск, проверка готовности, остановка по
atexit и сигналам. Module-level состояние (_server_processes/_server_owners под
_server_lock) и atexit-регистрация ЖИВУТ ЗДЕСЬ ЦЕЛИКОМ — их нельзя разрывать при
переносе. Импортирует base (соединение) и db (PROJECT_ROOT для CONFIG_PATH); НЕ
тянет runtime, поэтому работает standalone (цикла нет).
"""

import atexit
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import httpx

from db import PROJECT_ROOT
from opencode_base import Writer, client_for_port

CONFIG_PATH = PROJECT_ROOT / "opencode.json"
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


def _server_environment(*, planning: bool) -> dict[str, str]:
    env = os.environ.copy()
    env["OPENCODE_CONFIG"] = str(CONFIG_PATH)
    # Каждый serve-процесс должен иметь собственную SQLite. Общая стандартная
    # opencode.db конфликтует на параллельной инициализации и миграциях.
    # Сессии бенчмарка после остановки процесса не нужны: отчёт и артефакты
    # сохраняются в data/main.db.
    env["OPENCODE_DB"] = ":memory:"
    # В OpenCode 1.17 plan_exit регистрируется только экспериментальным native
    # plan mode. Значение задаётся и для off, чтобы внешний env не менял смысл
    # CLI-флага --planning.
    env["OPENCODE_EXPERIMENTAL_PLAN_MODE"] = "1" if planning else "0"
    return env


def ensure_server_running(work_dir: Path, port: int, status: Writer, *,
                          planning: bool = False) -> bool:
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
    env = _server_environment(planning=planning)
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

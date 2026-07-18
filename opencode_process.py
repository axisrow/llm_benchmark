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
# issue #150: при одновременном старте нескольких bench.py opencode serve падает
# сам (ServeError, exit 1 за ~3с) — конкуренция за ресурсы на старте. Одна
# неудача не должна валить копию: ретраим подъём с паузой между попытками.
SERVER_START_ATTEMPTS = 3
SERVER_START_RETRY_DELAY = 3
# Ретраи ждут меньше первой попытки: сценарий #150 — быстрый крах (~3с), а
# по-настоящему зависший serve не стоит ждать полный таймаут трижды.
SERVER_START_RETRY_TIMEOUT = 10

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


def _reap(procs: list[tuple[subprocess.Popen, Path]]) -> None:
    """Погасить процессы и удалить их логи. Общий код stop_servers/stop_server.

    Сначала terminate всем, потом ожидание каждого: так параллельные serve
    гасятся одновременно, а не последовательно по 5с. Зовётся в том числе из
    atexit-handler — падать нельзя ни на одном шаге.
    """
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


def stop_servers() -> None:
    with _server_lock:
        procs = list(_server_processes)
        _server_processes.clear()
        _server_owners.clear()
    _reap(procs)


def stop_server(port: int) -> None:
    """Погасить serve ОДНОЙ копии по её порту, не трогая чужие (issue #139).

    Зовётся из run_copy по завершении копии (успех/таймаут/ошибка), чтобы её
    serve не висел до конца всего прогона. Учёт снимается под _server_lock ДО
    гашения, поэтому последующий stop_servers (atexit) этот процесс уже не
    увидит — двойного kill нет. Неизвестный порт — no-op.
    """
    with _server_lock:
        owner = _server_owners.pop(port, None)
        if owner is None:
            return
        proc, _work_dir = owner
        entries = [entry for entry in _server_processes if entry[0] is proc]
        for entry in entries:
            _server_processes.remove(entry)
    _reap(entries)


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


def _server_cwd(port: int) -> str | None:
    """cwd ответившего на порту serve (через GET /app), или None.

    ``_try_connect`` отвечает лишь «на порту кто-то есть» — не «наш ли это
    процесс». Сверка cwd этого ответа с work_dir нашей копии закрывает окно
    захвата чужого serve (issue #152): даже если чужак занял порт в момент,
    когда наш proc уже мёртв, его cwd не совпадёт. Любая ошибка запроса — None
    (значит, личность подтвердить не удалось, попытку считаем проваленной).
    """
    try:
        return client_for_port(port).app.get().path.cwd
    except Exception:
        return None


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
    """Поднимает opencode serve для копии; ретраит падение подъёма (issue #150).

    opencode serve при одновременном старте нескольких bench.py падает сам
    (``ServeError``, exit 1 за ~3с) — конкуренция за ресурсы. Одна такая неудача
    не должна валить копию, поэтому подъём повторяется до
    ``SERVER_START_ATTEMPTS`` раз с паузой ``SERVER_START_RETRY_DELAY``.
    Ретраятся только восстановимые неудачи; если не стартовал сам ``Popen``
    (напр. нет ``opencode`` в PATH), исключение пробрасывается наверх — ретрай
    детерминированную ошибку не починит.
    """
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

    for attempt in range(1, SERVER_START_ATTEMPTS + 1):
        # Первая попытка ждёт полный таймаут; ретраи — укороченный: если serve
        # не поднялся сразу, ждать его полный таймаут ещё дважды бессмысленно.
        check_timeout = (SERVER_CHECK_TIMEOUT if attempt == 1
                         else SERVER_START_RETRY_TIMEOUT)
        if _start_server_once(work_dir, resolved_work_dir, port, status,
                              planning=planning, check_timeout=check_timeout):
            return True
        if attempt < SERVER_START_ATTEMPTS:
            status(f"повторяю подъём serve :{port} "
                   f"(попытка {attempt + 1}/{SERVER_START_ATTEMPTS}) "
                   f"через {SERVER_START_RETRY_DELAY}с")
            time.sleep(SERVER_START_RETRY_DELAY)
    return False


def _read_serve_log(stderr_path: Path) -> str:
    """stderr упавшего serve; пусто, если лог уже удалён/недоступен.

    Лог чистится в ``_reap`` вместе с гашением процесса, поэтому при ретрае
    (issue #150) файл предыдущей попытки может уже не существовать — читать
    надо мягко, иначе диагностика падает вместо того, чтобы показать причину.
    """
    try:
        return stderr_path.read_text(errors="replace").strip()
    except OSError:
        return ""


def _start_server_once(work_dir: Path, resolved_work_dir: Path, port: int,
                       status: Writer, *, planning: bool = False,
                       check_timeout: int = SERVER_CHECK_TIMEOUT) -> bool:
    """Одна попытка поднять serve. False — не поднялся (процесс уже погашен).

    ``check_timeout`` — сколько ждать ответа порта; ретраи ждут меньше первой
    попытки (см. ``ensure_server_running``).
    """
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
        # Popen не стартовал (напр. нет opencode в PATH) — детерминированная
        # ошибка, ретрай её не починит. Пробрасываем: run_copy отчитается
        # code=2 с точной причиной, а не жжёт паузы на безнадёжных попытках.
        status(f"не удалось запустить opencode serve: {exc}")
        raise
    finally:
        if not stderr_file.closed:
            stderr_file.close()
    with _server_lock:
        _server_processes.append((proc, stderr_path))
        _server_owners[port] = (proc, resolved_work_dir)

    waited = 0
    while waited < check_timeout:
        time.sleep(SERVER_CHECK_INTERVAL)
        waited += SERVER_CHECK_INTERVAL
        if proc.poll() is not None:
            log = _read_serve_log(stderr_path)
            status(f"opencode serve упал (код {proc.returncode}):\n{log}")
            # Процесс мёртв, но записи о нём остались бы в реестрах и мешали
            # следующей попытке (порт считался бы «нашим»). Чистим (issue #150).
            stop_server(port)
            return False
        if _try_connect(port):
            # issue #152: порт отвечает, но это может быть ЧУЖОЙ serve, занявший
            # порт после старта нашего proc. Подтверждаем личность по cwd из
            # GET /app — он обязан совпадать с work_dir нашей копии. Несовпадение
            # или невыясненная личность — чужой сервер: гасим наш (мёртвый) proc и
            # проваливаем попытку, чтобы ретрай/провал отработали штатно.
            server_cwd = _server_cwd(port)
            if server_cwd == str(resolved_work_dir):
                status(f"сервер :{port} запущен (ожидал {waited}с)")
                return True
            status(f"порт :{port} отвечает, но это чужой serve "
                   f"(cwd={server_cwd!r}, ожидали {str(resolved_work_dir)!r}) — "
                   f"не используем его")
            stop_server(port)
            return False

    log = _read_serve_log(stderr_path)
    tail = "\n".join(log.splitlines()[-20:]) if log else "(stderr пустой)"
    status(f"opencode serve :{port} не ответил за {check_timeout}с.\n"
           f"Последние строки stderr:\n{tail}")
    # issue #150: процесс ЖИВ, но не отвечает — гасим, иначе остаётся
    # осиротевший serve (держит порт и ресурсы до конца жизни bench-процесса).
    stop_server(port)
    return False

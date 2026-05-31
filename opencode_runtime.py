"""Shared OpenCode runtime helpers for benchmark and model checks."""

from __future__ import annotations

import atexit
import json
import os
import re
import signal
import subprocess
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
)

WORK_ROOT = PROJECT_ROOT / "data" / "result"
CONFIG_PATH = PROJECT_ROOT / "opencode.json"

DEFAULT_BASE_PORT = 4096
DEFAULT_MODEL = "glm-5.1"
DEFAULT_PROVIDER = "zai-coding-plan"
DEFAULT_AGENT = "coder"
DEFAULT_COPIES = 5
SERVER_CHECK_TIMEOUT = 30
SERVER_CHECK_INTERVAL = 2

Writer = Callable[[str], None]


@dataclass(frozen=True)
class SessionProbeResult:
    code: int
    reason: str | None = None
    usage: Usage | None = None


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

    stamp = time.strftime("%Y%m%d-%H%M%S")
    dirs: list[Path] = []
    for i in range(1, copies + 1):
        copy_dir = run_root / f"{stamp}_{i}"
        if copy_dir.exists():
            copy_dir = run_root / f"{stamp}_{i}_{int(time.monotonic() * 1000) % 100000}"
        copy_dir.mkdir(parents=True, exist_ok=False)
        dirs.append(copy_dir.resolve())
    return dirs


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
    except Exception as exc:
        if exc.__class__.__name__ in {"APIConnectionError", "ConnectError"}:
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


def _opencode_error_tail(session_id: str, lines: int = 8) -> str | None:
    try:
        log_files = sorted(OPENCODE_LOG_DIR.glob("*.log"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError:
        return None

    found: list[str] = []
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
    err = props.get("error") or {}
    data = err.get("data") or {}
    msg = data.get("message") or err.get("message") or err.get("name") or "?"
    code = data.get("statusCode")
    return f"{msg}" + (f" (HTTP {code})" if code else "")


def _fetch_session_usage(http: httpx.Client, session_id: str, write: Writer) -> Usage | None:
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
        if not stop.is_set():
            result["error"] = f"SSE reader error: {exc}"
            try:
                write(f"\n[SSE reader error] {exc}\n")
            except Exception:
                pass
        done.set()


def probe_session(task: str, model: str, provider: str, agent: str, timeout: float,
                  port: int, write: Writer) -> SessionProbeResult:
    base = base_url(port).rstrip("/")
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
        time.sleep(0.3)
        usage: Usage | None = None

        def provider_error_tail() -> str | None:
            tail = _opencode_error_tail(session_id)
            if tail:
                write("\n--- ошибки провайдера из лога opencode ---\n"
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

            write("\n--- таймаут ---\n")
            tail = provider_error_tail()
            reason = f"нет ответа за {timeout:.0f}с"
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
            print(f"[{label}] {msg}", flush=True)
    return emit


_VERDICT = {0: "готово", 1: "таймаут", 2: "ошибка"}


def fmt_secs(seconds: float) -> str:
    return f"{seconds:.1f}с"


def rel_to_root(path: Path, root: Path = PROJECT_ROOT) -> Path:
    """Путь относительно `root`, либо сам `path`, если он вне корня."""
    return path.relative_to(root) if path.is_relative_to(root) else path


def verdict(code: int) -> str:
    return _VERDICT.get(code, f"код {code}")

"""Гашение осиротевших `opencode serve` после насильственной смерти bench.py (issue #155).

При штатном выходе serve гасятся надёжно (`finally: stop_server` + `atexit` +
SIGTERM/SIGINT handler). Но SIGKILL (OOM-kill, `kill -9`, пропадание питания)
обходит и `atexit`, и `finally` — serve остаются осиротевшими, держат RAM
(сотни МБ каждый) и порты.

Почему нельзя проще:

* `pkill -f "opencode serve"` бьёт по всей таблице процессов — убьёт serve
  чужого параллельного bench.py или вручную запущенный serve.
* `pkill -P $$` бесполезен: после смерти родителя orphans reparented к PID 1.
* Ancestry (`ps -o ppid=`) хорошо доказывает «serve живого bench → защищаем»,
  но плохо доказывает обратное: PPID=1 может быть нашим orphan, а может быть
  чужим демоном.

Надёжное авто-гашение ЧУЖИХ процессов невозможно без сохранённого факта прежнего
владения. Поэтому путь C: marker (`.bench-active.json` с `copies[]`) + advisory
`fcntl.flock`. Ядро отпускает lock при SIGKILL само, даже когда Python-код не
отработал — «lock свободен» надёжнее, чем «PID не жив».

Кандидат без marker'а или с любым расхождением идентичности → `ambiguous`,
автоматически НЕ уничтожается (fail-closed).
"""

import json
import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from pathlib import Path

from artifacts import RUN_ACTIVE_MARKER, marker_lock_is_free

# Как долго ждать смерти процесса после SIGTERM, прежде чем эскалировать в SIGKILL.
SIGTERM_GRACE_SECONDS = 5.0
SIGTERM_POLL_INTERVAL = 0.2

# Инъектируемые зависимости. При None разрешаются ВНУТРИ функции (а не в
# default-arg), чтобы mock.patch на уровне модуля не ломался.
CommandRunner = Callable[[list[str]], "CommandResult"]
SignalSender = Callable[[int, int], None]
LockChecker = Callable[[Path], bool]


@dataclass(frozen=True)
class CommandResult:
    """Результат внешней команды: код возврата + stdout."""

    returncode: int
    stdout: str = ""


@dataclass(frozen=True)
class ServeCandidate:
    """Найденный процесс `opencode serve` и всё, что о нём удалось выяснить."""

    pid: int
    lstart: str | None = None
    comm: str | None = None
    command: str | None = None
    cwd: str | None = None
    stat: str | None = None
    port: int | None = None
    marker_path: Path | None = None
    reason: str = ""


@dataclass
class ReapResult:
    """Раздельные корзины исхода: что нашли, что погасили, что не тронули."""

    candidates: list[ServeCandidate] = field(default_factory=list)
    reaped: list[ServeCandidate] = field(default_factory=list)
    protected_live: list[ServeCandidate] = field(default_factory=list)
    ambiguous: list[ServeCandidate] = field(default_factory=list)
    zombies: list[ServeCandidate] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (
            f"кандидатов: {len(self.candidates)}, погашено: {len(self.reaped)}, "
            f"защищено (живой владелец): {len(self.protected_live)}, "
            f"неоднозначных: {len(self.ambiguous)}, "
            f"zombie: {len(self.zombies)}, ошибок: {len(self.errors)}"
        )


def _run_command(argv: list[str]) -> CommandResult:
    """Единая точка внешних вызовов: всегда ``shell=False``, никогда не падает.

    Отсутствие инструмента (нет в PATH) и ошибка запуска дают returncode=127 —
    вызывающий код трактует это как «выяснить не удалось» (fail-closed).
    """
    if not argv or shutil.which(argv[0]) is None:
        return CommandResult(returncode=127)
    try:
        proc = subprocess.run(
            argv,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
            text=True,
        )
    except OSError:
        return CommandResult(returncode=127)
    return CommandResult(returncode=proc.returncode, stdout=proc.stdout or "")


def _ps_field(pid: int, spec: str, run: CommandRunner) -> str | None:
    """Одно поле ``ps -o <spec>=`` по PID. None — ps недоступен/PID исчез."""
    result = run(["ps", "-o", f"{spec}=", "-p", str(pid)])
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _proc_cwd(pid: int, run: CommandRunner) -> str | None:
    """cwd процесса через ``lsof -a -p PID -d cwd -Fn``. None — не выяснено."""
    result = run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"])
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            return line[1:].strip() or None
    return None


def _probe_identity(pid: int, run: CommandRunner) -> ServeCandidate | None:
    """Снять текущую идентичность процесса. None — процесс исчез/ps недоступен."""
    lstart = _ps_field(pid, "lstart", run)
    if lstart is None:
        return None
    return ServeCandidate(
        pid=pid,
        lstart=lstart,
        comm=_ps_field(pid, "comm", run),
        command=_ps_field(pid, "command", run),
        cwd=_proc_cwd(pid, run),
        stat=_ps_field(pid, "stat", run),
    )


def _is_dead(candidate: ServeCandidate) -> bool:
    """Процесс уже мёртв, хотя PID ещё виден в таблице (zombie).

    После SIGTERM процесс становится zombie, пока родитель не сделал wait().
    ``ps`` его показывает, но ни сокета, ни RAM он не держит — цель достигнута.
    Без этой проверки успешное гашение выглядело бы как «PID пережил SIGTERM»,
    а последующая сверка идентичности (у zombie меняется lstart) ложно
    сообщала бы о PID reuse.
    """
    return bool(candidate.stat and candidate.stat.startswith("Z"))


def _discover_serve_pids(run: CommandRunner) -> tuple[list[int], list[str]]:
    """PID-ы живых ``opencode serve``. exit 1 у pgrep = «ничего не найдено»."""
    result = run(["pgrep", "-f", "opencode serve"])
    if result.returncode == 1:
        return [], []
    if result.returncode != 0:
        # 127 — pgrep недоступен; иное — ошибка. Ни в том, ни в другом случае
        # мы не знаем таблицу процессов, а гасить вслепую нельзя (fail-closed).
        return [], ["pgrep недоступен или завершился ошибкой — гашение пропущено"]
    pids: list[int] = []
    for line in result.stdout.split():
        try:
            pids.append(int(line))
        except ValueError:
            continue
    return pids, []


def _iter_markers(work_root: Path) -> Iterable[Path]:
    """Все `.bench-active.json` под корнем рабочих папок, без симлинков."""
    if not work_root.is_dir() or work_root.is_symlink():
        return []
    try:
        return [
            marker
            for marker in work_root.rglob(RUN_ACTIVE_MARKER)
            if marker.is_file() and not marker.is_symlink()
        ]
    except OSError:
        return []


@dataclass(frozen=True)
class _MarkerCopy:
    """Запись о serve одной копии, прочитанная из marker'а."""

    marker_path: Path
    serve_pid: int
    port: int | None
    serve_lstart: str | None
    work_dir: str | None


def _load_marker_copies(work_root: Path) -> tuple[dict[int, _MarkerCopy], list[str]]:
    """Сопоставление serve_pid → запись marker'а. Битые marker'ы пропускаем."""
    copies: dict[int, _MarkerCopy] = {}
    errors: list[str] = []
    for marker_path in _iter_markers(work_root):
        try:
            payload = json.loads(marker_path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            errors.append(f"{marker_path}: повреждённый marker: {exc}")
            continue
        if not isinstance(payload, dict):
            continue
        for entry in payload.get("copies") or []:
            if not isinstance(entry, dict):
                continue
            serve_pid = entry.get("serve_pid")
            if not isinstance(serve_pid, int) or serve_pid <= 0:
                continue
            port = entry.get("port")
            copies[serve_pid] = _MarkerCopy(
                marker_path=marker_path,
                serve_pid=serve_pid,
                port=port if isinstance(port, int) else None,
                serve_lstart=entry.get("serve_lstart"),
                work_dir=entry.get("work_dir"),
            )
    return copies, errors


def _identity_matches(current: ServeCandidate, record: _MarkerCopy,
                      run: CommandRunner) -> str | None:
    """None — идентичность совпала; иначе строка с причиной расхождения.

    Проверяются все доступные признаки: время старта (защита от PID reuse),
    имя процесса, cwd и владение портом.
    """
    if record.serve_lstart and current.lstart != record.serve_lstart:
        return "время старта процесса изменилось (PID reuse)"
    if current.comm is not None and "opencode" not in current.comm:
        return f"comm не похож на opencode: {current.comm}"
    if current.command is not None and "serve" not in current.command:
        return f"command не содержит serve: {current.command}"
    if record.work_dir and current.cwd is not None:
        # Пути сравниваем ПОСЛЕ resolve: lsof отдаёт cwd уже с раскрытыми
        # симлинками (на macOS /tmp → /private/tmp), а в marker'е путь записан
        # так, как его видел bench.py. Без нормализации наш же serve считался бы
        # чужим и оставался бы висеть как ambiguous.
        if _resolved(current.cwd) != _resolved(record.work_dir):
            return "cwd не совпадает с work_dir из marker'а"
    if record.port is not None and not _port_held_by_pid(record.port, current.pid, run):
        return f"порт {record.port} не принадлежит процессу"
    return None


def _resolved(path: str) -> Path:
    """Путь с раскрытыми симлинками; при ошибке — как есть (сравнение строгое)."""
    try:
        return Path(path).resolve(strict=False)
    except OSError:
        return Path(path)


def _port_held_by_pid(port: int, pid: int, run: CommandRunner) -> bool:
    """Держит ли ``pid`` listening-сокет ``port``. Инструмент недоступен → False.

    Тот же приём, что в ``opencode_process._port_owned_by_proc``: lsof (macOS/BSD)
    → ss (Linux/iproute2). Fail-closed: не подтвердили владение — не гасим.
    """
    result = run(["lsof", "-nP", "-a", "-p", str(pid),
                  f"-iTCP:{port}", "-sTCP:LISTEN"])
    if result.returncode == 0:
        return True
    if result.returncode != 127:
        return False
    result = run(["ss", "-ltnp", f"sport = :{port}"])
    if result.returncode != 0:
        return False
    return f"pid={pid}," in result.stdout or f"pid={pid}\n" in result.stdout


def _terminate(candidate: ServeCandidate, record: _MarkerCopy, *,
               send_signal: SignalSender, run: CommandRunner,
               errors: list[str]) -> bool:
    """SIGTERM ровно одному PID, при необходимости SIGKILL. True — процесс мёртв.

    Перед SIGKILL идентичность СВЕРЯЕТСЯ ПОВТОРНО: между SIGTERM и эскалацией
    процесс мог умереть, а его PID — достаться чужому процессу.
    """
    try:
        send_signal(candidate.pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError as exc:
        errors.append(f"pid {candidate.pid}: SIGTERM не отправлен: {exc}")
        return False

    deadline = time.monotonic() + SIGTERM_GRACE_SECONDS
    while time.monotonic() < deadline:
        current = _probe_identity(candidate.pid, run)
        if current is None or _is_dead(current):
            return True
        time.sleep(SIGTERM_POLL_INTERVAL)

    current = _probe_identity(candidate.pid, run)
    if current is None or _is_dead(current):
        return True
    if _identity_matches(current, record, run) is not None:
        # PID уже не наш — эскалировать в SIGKILL нельзя.
        errors.append(f"pid {candidate.pid}: идентичность изменилась после "
                      "SIGTERM — SIGKILL не отправлен")
        return False
    try:
        send_signal(candidate.pid, signal.SIGKILL)
    except ProcessLookupError:
        return True
    except OSError as exc:
        errors.append(f"pid {candidate.pid}: SIGKILL не отправлен: {exc}")
        return False
    return True


def reap_orphan_serves(
    *,
    work_root: Path,
    apply: bool = False,
    command_runner: CommandRunner | None = None,
    signal_sender: SignalSender | None = None,
    lock_checker: LockChecker | None = None,
) -> ReapResult:
    """Найти и (при ``apply``) погасить осиротевшие `opencode serve`.

    Подтверждённый orphan — процесс, для которого выполнены ВСЕ условия:

    1. Есть marker с записью про этот serve (``serve_pid`` совпал).
    2. Lock на marker свободен — владелец-bench.py мёртв (при SIGKILL ядро
       отпустило lock само).
    3. Повторная сверка идентичности прямо перед сигналом: lstart/comm/command/
       cwd/порт не изменились (защита от PID reuse).
    4. ``stat != Z`` — zombie не сигналим, он и так не держит сокет и RAM.

    Любое расхождение или отсутствие marker'а → ``ambiguous``, не гасим.

    Args:
        work_root: корень рабочих папок (``data/result``), где лежат marker'ы.
        apply: False (по умолчанию) — только перечислить кандидатов.
        command_runner: подмена внешних вызовов (``ps``/``pgrep``/``lsof``).
        signal_sender: подмена ``os.kill``.
        lock_checker: подмена проверки «lock на marker свободен».
    """
    # Зависимости разрешаются здесь, а не в default-arg: иначе mock.patch на
    # уровне модуля не подхватится (сигнатура связалась бы при импорте).
    run = command_runner if command_runner is not None else _run_command
    send_signal = signal_sender if signal_sender is not None else os.kill
    lock_free = lock_checker if lock_checker is not None else marker_lock_is_free

    result = ReapResult()
    pids, discovery_errors = _discover_serve_pids(run)
    result.errors.extend(discovery_errors)
    if not pids:
        return result

    marker_copies, marker_errors = _load_marker_copies(Path(work_root))
    result.errors.extend(marker_errors)

    for pid in pids:
        record = marker_copies.get(pid)
        current = _probe_identity(pid, run)
        if current is None:
            # ps недоступен или процесс уже исчез — идентичность не выяснена.
            result.ambiguous.append(ServeCandidate(
                pid=pid, reason="идентичность не выяснена (ps недоступен)"))
            continue

        if record is None:
            result.ambiguous.append(_with_reason(
                current, "нет marker'а с записью про этот serve"))
            continue

        current = _attach_record(current, record)
        if not lock_free(record.marker_path):
            result.protected_live.append(_with_reason(
                current, "lock на marker занят — владелец жив"))
            continue

        if current.stat and current.stat.startswith("Z"):
            result.zombies.append(_with_reason(current, "zombie"))
            continue

        mismatch = _identity_matches(current, record, run)
        if mismatch is not None:
            result.ambiguous.append(_with_reason(current, mismatch))
            continue

        result.candidates.append(_with_reason(current, "подтверждённый orphan"))

    if not apply:
        return result

    for candidate in list(result.candidates):
        record = marker_copies[candidate.pid]
        if _terminate(candidate, record, send_signal=send_signal, run=run,
                      errors=result.errors):
            result.reaped.append(candidate)
    return result


def _with_reason(candidate: ServeCandidate, reason: str) -> ServeCandidate:
    return replace(candidate, reason=reason)


def _attach_record(candidate: ServeCandidate,
                   record: _MarkerCopy) -> ServeCandidate:
    return replace(candidate, port=record.port, marker_path=record.marker_path)

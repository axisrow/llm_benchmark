"""Collect and clean benchmark run artifacts."""

import hashlib
import json
import os
import shutil
import time
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path


ARTIFACT_KIND_LOG = "log"
ARTIFACT_KIND_AGENT_FILE = "agent_file"
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024
RUN_ACTIVE_MARKER = ".bench-active.json"
ABANDONED_RUN_GRACE_SECONDS = 24 * 60 * 60

_EXCLUDED_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
# report.json НЕ исключаем по basename: оркестраторский report.json пишется в
# run_root (родитель папок копий), вне обходимого collect_run_artifacts дерева,
# поэтому исключение по имени защищало только агентский вывод — теряя его (B8).
_EXCLUDED_FILE_NAMES = {".DS_Store", RUN_ACTIVE_MARKER}
_EXCLUDED_SUFFIXES = {".pyc"}


@dataclass(frozen=True)
class RunArtifact:
    run_idx: int
    path: str
    kind: str
    size_bytes: int
    sha256: str
    content: bytes
    source_path: Path


@dataclass(frozen=True)
class ArtifactCollection:
    artifacts: list[RunArtifact]
    trash_paths: list[Path]
    errors: list[str]
    # Папки копий, по которым шёл сбор. Нужны cleanup-у, чтобы удалить саму
    # опустевшую папку копии даже когда в ней не было top-level артефакта
    # (только вложенные файлы или только trash) — иначе пустые
    # data/result/<proj>/<prov_model>/<ts>_<N>/ копятся на диске.
    work_dirs: list[Path] = field(default_factory=list)

    def summary(self) -> dict[str, int | list[str]]:
        log_count = agent_file_count = total_bytes = 0
        for artifact in self.artifacts:
            if artifact.kind == ARTIFACT_KIND_LOG:
                log_count += 1
            elif artifact.kind == ARTIFACT_KIND_AGENT_FILE:
                agent_file_count += 1
            total_bytes += artifact.size_bytes
        return {
            "files": len(self.artifacts),
            "logs": log_count,
            "agent_files": agent_file_count,
            "bytes": total_bytes,
            "errors": self.errors,
        }


@dataclass
class AbandonedCleanupResult:
    """Результат безопасного поиска заброшенных рабочих каталогов."""

    candidates: list[Path] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    skipped_active: list[Path] = field(default_factory=list)
    skipped_young: list[Path] = field(default_factory=list)
    skipped_known: list[Path] = field(default_factory=list)
    skipped_invalid: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def write_run_active_marker(
    work_dir: Path,
    *,
    pid: int | None = None,
    started_at: float | None = None,
) -> Path:
    """Записать marker живого benchmark-процесса в каталог копии."""
    marker = work_dir / RUN_ACTIVE_MARKER
    payload = {
        "pid": os.getpid() if pid is None else pid,
        "started_at": time.time() if started_at is None else started_at,
    }
    marker.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")
    return marker


def _pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        # Неоднозначная системная ошибка: безопаснее считать процесс живым.
        return True
    return True


def _iter_work_dirs(work_root: Path) -> Iterable[Path]:
    """Каталоги копий строго на глубине project/model/run, без симлинков."""
    try:
        project_dirs = list(work_root.iterdir())
    except OSError:
        return
    for project_dir in project_dirs:
        if project_dir.name == ".git" or project_dir.is_symlink():
            continue
        if not project_dir.is_dir():
            continue
        try:
            model_dirs = list(project_dir.iterdir())
        except OSError:
            continue
        for model_dir in model_dirs:
            if model_dir.is_symlink() or not model_dir.is_dir():
                continue
            try:
                copy_dirs = list(model_dir.iterdir())
            except OSError:
                continue
            for copy_dir in copy_dirs:
                if copy_dir.is_symlink() or not copy_dir.is_dir():
                    continue
                yield copy_dir


def cleanup_abandoned_work_dirs(
    work_root: Path,
    known_run_dirs: Iterable[Path],
    *,
    apply: bool,
    now: float | None = None,
    grace_seconds: float = ABANDONED_RUN_GRACE_SECONDS,
) -> AbandonedCleanupResult:
    """Найти или удалить старые orphan work_dir, не затрагивая живые прогоны.

    Каталоги, уже упомянутые в БД, здесь не удаляются целиком: их содержимое
    требует послойной сверки пути и SHA. Эта функция отвечает только за хвосты
    процессов, которые не успели сохранить отчёт.
    """
    result = AbandonedCleanupResult()
    if not work_root.exists() or work_root.is_symlink():
        return result
    root = work_root.resolve()
    known = {Path(path).resolve(strict=False) for path in known_run_dirs}
    now_ts = time.time() if now is None else now

    for work_dir in _iter_work_dirs(root):
        try:
            resolved = work_dir.resolve(strict=True)
            resolved.relative_to(root)
            age = now_ts - work_dir.stat().st_mtime
        except (OSError, ValueError) as exc:
            result.errors.append(f"{work_dir}: {exc}")
            continue

        if age < grace_seconds:
            result.skipped_young.append(work_dir)
            continue

        marker = work_dir / RUN_ACTIVE_MARKER
        if marker.exists() or marker.is_symlink():
            if marker.is_symlink():
                result.skipped_invalid.append(work_dir)
                continue
            try:
                payload = json.loads(marker.read_text(encoding="utf-8"))
                pid = payload.get("pid")
                if not isinstance(pid, int):
                    raise ValueError("marker.pid должен быть int")
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                result.skipped_invalid.append(work_dir)
                result.errors.append(f"{marker}: повреждённый marker: {exc}")
                continue
            if _pid_is_alive(pid):
                result.skipped_active.append(work_dir)
                continue

        if resolved in known:
            result.skipped_known.append(work_dir)
            continue

        result.candidates.append(work_dir)
        if not apply:
            continue
        try:
            # work_dir и все его предки проверены как обычные каталоги; rmtree
            # не следует по симлинкам, встретившимся уже внутри дерева.
            shutil.rmtree(work_dir)
            result.removed.append(work_dir)
        except FileNotFoundError:
            pass
        except OSError as exc:
            result.errors.append(f"{work_dir}: {exc}")

    if apply:
        _prune_empty_dirs(root)
    return result


def _is_excluded_file(path: Path) -> bool:
    return path.name in _EXCLUDED_FILE_NAMES or path.suffix in _EXCLUDED_SUFFIXES


def _relative_posix(path: Path, root: Path) -> str:
    return path.relative_to(root).as_posix()


def collect_run_artifacts(run_idx: int, work_dir: Path) -> ArtifactCollection:
    """Collect run.log and agent-created files from one copy directory."""
    artifacts: list[RunArtifact] = []
    trash_paths: list[Path] = []
    errors: list[str] = []
    root = work_dir.resolve()
    if not root.exists():
        return ArtifactCollection([], [], [f"{work_dir}: missing"])

    for current_root, dir_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        kept_dirs = []
        for dir_name in dir_names:
            path = current / dir_name
            rel_parts = path.relative_to(root).parts
            if rel_parts[:2] == (".opencode", "plans"):
                # Native OpenCode plan files are durable benchmark output. They
                # are referenced from reports and intentionally remain on disk.
                continue
            if dir_name in _EXCLUDED_DIR_NAMES:
                trash_paths.append(path)
                continue
            if path.is_symlink():
                errors.append(f"{path}: symlink directory skipped")
                continue
            kept_dirs.append(dir_name)
        dir_names[:] = kept_dirs

        for file_name in file_names:
            path = current / file_name
            if path.is_symlink():
                errors.append(f"{path}: symlink file skipped")
                continue

            rel = _relative_posix(path, root)
            if rel == "run.log":
                kind = ARTIFACT_KIND_LOG
            elif _is_excluded_file(path):
                trash_paths.append(path)
                continue
            else:
                kind = ARTIFACT_KIND_AGENT_FILE

            try:
                stat_size = path.stat().st_size
                if stat_size > MAX_ARTIFACT_BYTES:
                    errors.append(
                        f"{path}: skipped, size {stat_size} exceeds "
                        f"{MAX_ARTIFACT_BYTES} bytes"
                    )
                    continue
                content = path.read_bytes()
            except OSError as exc:
                errors.append(f"{path}: {exc}")
                continue

            artifacts.append(RunArtifact(
                run_idx=run_idx,
                path=rel,
                kind=kind,
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                content=content,
                source_path=path,
            ))

    return ArtifactCollection(artifacts, trash_paths, errors, [root])


def collect_artifacts_from_dirs(
    run_dirs: Iterable[tuple[int, Path]],
) -> ArtifactCollection:
    """Собрать артефакты из итерируемого объекта пар (run_idx, work_dir).

    Обобщает цикл агрегации, общий для collect_report_artifacts
    (извлекает пары из словарей результатов) и cmd_backfill (читает из БД).
    """
    artifacts: list[RunArtifact] = []
    trash_paths: list[Path] = []
    errors: list[str] = []
    work_dirs: list[Path] = []
    for run_idx, work_dir in run_dirs:
        collection = collect_run_artifacts(run_idx, work_dir)
        artifacts.extend(collection.artifacts)
        trash_paths.extend(collection.trash_paths)
        errors.extend(collection.errors)
        work_dirs.extend(collection.work_dirs)
    return ArtifactCollection(artifacts, trash_paths, errors, work_dirs)


def collect_report_artifacts(results: list[dict]) -> ArtifactCollection:
    valid_dirs: list[tuple[int, Path]] = []
    errors: list[str] = []
    for result in results:
        run_idx = result.get("index")
        work_dir = result.get("dir")
        if not isinstance(run_idx, int) or not work_dir:
            errors.append(f"bad run result: index={run_idx!r} dir={work_dir!r}")
            continue
        valid_dirs.append((run_idx, Path(work_dir)))
    collection = collect_artifacts_from_dirs(valid_dirs)
    return ArtifactCollection(
        collection.artifacts, collection.trash_paths, errors + collection.errors,
        collection.work_dirs,
    )


def _prune_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for current_root, dir_names, _file_names in os.walk(root, topdown=False):
        current = Path(current_root)
        for dir_name in dir_names:
            path = current / dir_name
            try:
                path.rmdir()
            except OSError:
                # Непустая/занятая папка — штатное условие выхода обхода, не сбой.
                pass


def cleanup_collected_artifacts(collection: ArtifactCollection) -> None:
    """Remove files already stored in DB plus known generated trash."""
    roots = {artifact.source_path.parent for artifact in collection.artifacts}
    roots.update(path.parent if path.is_file() else path for path in collection.trash_paths)
    # Сама папка копии — на случай, когда top-level артефакта не было (только
    # вложенные файлы или только trash): её родителя (папку модели) не трогаем.
    roots.update(collection.work_dirs)

    # Ловим только FileNotFoundError (файл уже удалён — идемпотентность); любая
    # другая ошибка (нет прав, read-only FS) НЕ глушится и всплывёт наверх.
    for artifact in collection.artifacts:
        try:
            artifact.source_path.unlink()
        except FileNotFoundError:
            pass

    for path in sorted(collection.trash_paths, key=lambda p: len(p.parts), reverse=True):
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()
        except FileNotFoundError:
            pass

    for root in sorted(roots, key=lambda p: len(p.parts), reverse=True):
        _prune_empty_dirs(root)
        # os.walk не отдаёт сам корень — опустевшую папку копии удаляем явно,
        # иначе пустые data/result/<proj>/<prov_model>/<ts>_<N>/ копятся.
        # Непустая (несобранные файлы) переживает rmdir через OSError.
        try:
            root.rmdir()
        except OSError:
            pass

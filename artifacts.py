"""Collect and clean benchmark run artifacts."""

import hashlib
import os
import shutil
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path


ARTIFACT_KIND_LOG = "log"
ARTIFACT_KIND_AGENT_FILE = "agent_file"
MAX_ARTIFACT_BYTES = 10 * 1024 * 1024

_EXCLUDED_DIR_NAMES = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
_EXCLUDED_FILE_NAMES = {".DS_Store", "report.json"}
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

    return ArtifactCollection(artifacts, trash_paths, errors)


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
    for run_idx, work_dir in run_dirs:
        collection = collect_run_artifacts(run_idx, work_dir)
        artifacts.extend(collection.artifacts)
        trash_paths.extend(collection.trash_paths)
        errors.extend(collection.errors)
    return ArtifactCollection(artifacts, trash_paths, errors)


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

"""Безопасная очистка сохранённых артефактов и заброшенных work_dir.

Dry-run используется по умолчанию. ``--apply`` удаляет файлы известных
прогонов только при совпадении пути и SHA, а также старые orphan-каталоги без
живого PID-marker. База открывается строго read-only.
"""

import argparse
import hashlib
import os
import shutil
import sqlite3
import sys
from collections.abc import Iterator
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from artifacts import (  # noqa: E402
    ABANDONED_RUN_GRACE_SECONDS,
    RUN_ACTIVE_MARKER,
    _EXCLUDED_DIR_NAMES,
    cleanup_abandoned_work_dirs,
)
from db import DB_PATH  # noqa: E402


_DEFAULT_RESULT_ROOT = Path(__file__).resolve().parent.parent / "data" / "result"


def _conn(path: Path) -> sqlite3.Connection:
    """Открыть только существующую SQLite БД без файловых изменений."""
    if not path.is_file():
        raise FileNotFoundError(f"база не существует: {path}")
    uri = f"{path.resolve().as_uri()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _load_known_artifacts(
    conn: sqlite3.Connection,
) -> dict[Path, dict[str, str]]:
    """Сопоставление work_dir -> относительный путь -> SHA из БД."""
    rows = conn.execute(
        """
        SELECT runs.dir AS run_dir, ra.path AS rel_path, ra.sha256 AS sha256
        FROM run_artifacts AS ra
        JOIN runs ON runs.report_id = ra.report_id AND runs.idx = ra.run_idx
        """,
    ).fetchall()
    known: dict[Path, dict[str, str]] = {}
    for row in rows:
        if not row["run_dir"]:
            continue
        run_dir = Path(row["run_dir"]).resolve(strict=False)
        known.setdefault(run_dir, {})[row["rel_path"]] = row["sha256"]
    return known


def _load_known_run_dirs(conn: sqlite3.Connection) -> set[Path]:
    return {
        Path(row["dir"]).resolve(strict=False)
        for row in conn.execute(
            "SELECT DISTINCT dir FROM runs WHERE dir IS NOT NULL AND dir != ''",
        ).fetchall()
    }


def _walk_entries(root: Path) -> Iterator[tuple[str, Path]]:
    """Обход без следования по симлинкам и служебным каталогам."""
    for current_root, dir_names, file_names in os.walk(root, followlinks=False):
        current = Path(current_root)
        kept: list[str] = []
        for name in dir_names:
            path = current / name
            if path.is_symlink():
                yield "symlink_dir", path
            elif name in _EXCLUDED_DIR_NAMES:
                yield "trash_dir", path
            else:
                kept.append(name)
        dir_names[:] = kept
        for name in file_names:
            path = current / name
            if path == root / ".git" or name == RUN_ACTIVE_MARKER:
                continue
            yield "file", path


def _resolved_within(path: Path, root: Path) -> Path | None:
    try:
        resolved = path.resolve(strict=True)
        resolved.relative_to(root)
    except (OSError, ValueError):
        return None
    return resolved


def _classify_file(
    path: Path,
    known: dict[Path, dict[str, str]],
    root: Path,
) -> tuple[str, object | None]:
    if path.is_symlink():
        return "symlink", None
    resolved = _resolved_within(path, root)
    if resolved is None:
        return "unsafe", None
    try:
        content = path.read_bytes()
    except OSError as exc:
        return "unreadable", exc
    sha = hashlib.sha256(content).hexdigest()

    for run_dir, files in known.items():
        try:
            rel = resolved.relative_to(run_dir).as_posix()
        except ValueError:
            continue
        known_sha = files.get(rel)
        if known_sha is None:
            continue
        return ("confirmed", None) if known_sha == sha else ("mismatch", rel)
    return "unknown", path.name


def _inside_known_run(path: Path, known_run_dirs: set[Path]) -> bool:
    try:
        resolved = path.resolve(strict=True)
    except OSError:
        return False
    for run_dir in known_run_dirs:
        try:
            resolved.relative_to(run_dir)
            return True
        except ValueError:
            continue
    return False


def _safe_unlink(path: Path, root: Path) -> bool:
    if path.is_symlink() or _resolved_within(path, root) is None:
        return False
    path.unlink()
    return True


def _safe_rmtree(path: Path, root: Path) -> bool:
    if path.is_symlink() or _resolved_within(path, root) is None:
        return False
    shutil.rmtree(path)
    return True


def _prune_empty_dirs(root: Path) -> None:
    if not root.exists():
        return
    for current_root, dir_names, _files in os.walk(root, topdown=False):
        current = Path(current_root)
        for name in dir_names:
            path = current / name
            if path.is_symlink():
                continue
            try:
                path.rmdir()
            except OSError:
                pass


def cmd_cleanup(args: argparse.Namespace) -> int:
    result_root: Path = args.result_root
    if not result_root.exists():
        print(f"result_root не существует: {result_root}")
        return 0
    if result_root.is_symlink() or not result_root.is_dir():
        print(f"error: result_root должен быть обычным каталогом: {result_root}",
              file=sys.stderr)
        return 2
    root = result_root.resolve(strict=True)

    try:
        conn = _conn(args.db)
        try:
            known = _load_known_artifacts(conn)
            known_run_dirs = _load_known_run_dirs(conn)
        finally:
            conn.close()
    except (OSError, sqlite3.Error) as exc:
        print(f"error: не удалось открыть БД read-only: {exc}", file=sys.stderr)
        return 2

    confirmed: list[Path] = []
    mismatched: list[Path] = []
    unknown: list[Path] = []
    symlinks: list[Path] = []
    symlink_dirs: list[Path] = []
    unsafe: list[Path] = []
    unreadable: list[tuple[Path, str]] = []
    trash_dirs: list[Path] = []

    for kind, path in _walk_entries(root):
        if kind == "symlink_dir":
            symlink_dirs.append(path)
            continue
        if kind == "trash_dir":
            if _inside_known_run(path, known_run_dirs):
                trash_dirs.append(path)
            else:
                unknown.append(path)
            continue
        category, detail = _classify_file(path, known, root)
        if category == "confirmed":
            confirmed.append(path)
        elif category == "mismatch":
            mismatched.append(path)
        elif category == "unknown":
            unknown.append(path)
        elif category == "symlink":
            symlinks.append(path)
        elif category == "unsafe":
            unsafe.append(path)
        elif category == "unreadable":
            unreadable.append((path, str(detail)))

    hours = float(getattr(args, "abandoned_after_hours", 24.0))
    abandoned = cleanup_abandoned_work_dirs(
        root,
        known_run_dirs,
        apply=bool(args.apply),
        grace_seconds=max(0.0, hours * 60 * 60),
    )

    artifact_count = sum(len(files) for files in known.values())
    print(f"result_root: {root}")
    print(f"Записей артефактов в БД: {artifact_count}")
    print(f"Подтверждено по SHA (к удалению): {len(confirmed)}")
    print(f"Служебных cache-каталогов: {len(trash_dirs)}")
    print(f"Заброшенных orphan work_dir: {len(abandoned.candidates)}")
    print(f"Несовпадающих по SHA (mismatched): {len(mismatched)}")
    print(f"Неизвестных (нет в БД): {len(unknown)}")
    print(f"Симлинков: {len(symlinks) + len(symlink_dirs)}")
    print(f"Небезопасных путей: {len(unsafe)}")
    print(f"Нечитаемых: {len(unreadable)}")

    for label, paths in (
        ("mismatch", mismatched),
        ("unknown", unknown),
        ("symlink", symlinks),
        ("symdir", symlink_dirs),
        ("unsafe", unsafe),
    ):
        for path in sorted(paths):
            print(f"  [{label}] {path}")
    for path, exc in sorted(unreadable):
        print(f"  [unread] {path}: {exc}")
    for error in abandoned.errors:
        print(f"  [marker] {error}")

    if not args.apply:
        print("\n[dry-run] изменений не внесено. Передайте --apply для удаления.")
        return 0

    removed = 0
    for path in confirmed:
        try:
            if _safe_unlink(path, root):
                removed += 1
            else:
                print(f"warning: небезопасный путь пропущен: {path}", file=sys.stderr)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"warning: не удалось удалить {path}: {exc}", file=sys.stderr)
    for path in sorted(trash_dirs, key=lambda item: len(item.parts), reverse=True):
        try:
            _safe_rmtree(path, root)
        except FileNotFoundError:
            pass
        except OSError as exc:
            print(f"warning: не удалось удалить cache {path}: {exc}", file=sys.stderr)
    _prune_empty_dirs(root)
    print(f"\nУдалено подтверждённых файлов: {removed}")
    print(f"Удалено заброшенных work_dir: {len(abandoned.removed)}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH)
    parser.add_argument("--result-root", type=Path, default=_DEFAULT_RESULT_ROOT)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument(
        "--abandoned-after-hours",
        type=float,
        default=ABANDONED_RUN_GRACE_SECONDS / 3600,
        help="Возраст orphan work_dir для удаления (default: 24)",
    )
    raise SystemExit(cmd_cleanup(parser.parse_args()))


if __name__ == "__main__":
    main()

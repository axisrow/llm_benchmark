"""Безопасная очистка исторических остатков под data/result/.

Follow-up к пункту 4 issue #42 и issue #99. После каждого прогона `bench.py`
удаляет уже сохранённые в базу файлы копий (`_finalize` → `cleanup_collected_artifacts`).
Но накопленные ДО этого фикса остатки — `run.log` и исходники старых прогонов —
остались под `data/result/`, хотя отчёты и артефакты давно живут в `data/main.db`.

Этот скрипт разово подметает такие остатки, не прибегая к слепому
`rm -rf data/result`:

  - dry-run по умолчанию: печатает проверяемый план, ничего не удаляя;
  - `--apply` — удаляет только файлы, подтверждённые по пути И SHA содержимого
    записью в `run_artifacts` (каталог копии runs.dir + path + sha256);
  - неизвестные (каталог/путь не известны БД), несовпадающие по SHA, нечитаемые
    файлы и симлинки НЕ удаляются, а перечисляются отдельно;
  - удаление не выходит за границу `--result-root` (по умолчанию `data/result/`);
    опустевшие каталоги зачищаются в этих же границах.

Запуск:
    python scripts/cleanup_result_dir.py                 # dry-run по умолчанию
    python scripts/cleanup_result_dir.py --apply         # удалить подтверждённое
    python scripts/cleanup_result_dir.py --result-root /other/root --apply
"""

import argparse
import hashlib
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # корень — import db

from artifacts import _EXCLUDED_DIR_NAMES  # noqa: E402 — единый список мусорных каталогов
from db import DB_PATH, connect, init_schema  # noqa: E402

# По умолчанию чистим рабочий корень прогонов (data/result/). Совпадает с
# WORK_ROOT из opencode_runtime, но тащить зависимость не нужно — скрипт
# самодостаточен и работает с любым result_root.
_DEFAULT_RESULT_ROOT = Path(__file__).resolve().parent.parent / "data" / "result"


def _conn(path: Path):
    conn = connect(path)
    init_schema(conn)
    return conn


def _load_known_artifacts(conn) -> dict[Path, dict[str, str]]:
    """{resolved_runs_dir: {rel_posix_path: sha256_hex}} по всем run_artifacts БД.

    Ключ верхнего уровня — каталог копии (runs.dir) с развёрнутыми симлинками
    (resolve): именно по нему файл на диске ищет «свой» прогон (файл лежит
    внутри этого каталога). Внутренний dict — относительные пути артефактов
    внутри work_dir (run_artifacts.path, напр. «run.log» или «nested/data.bin»)
    → sha256 для сверки содержимого. Дубликаты rel внутри одного runs.dir
    невозможны (PK run_artifacts = report_id+run_idx+path); один и тот же путь
    в разных копиях живут под разными ключами runs.dir.
    """
    rows = conn.execute(
        """
        SELECT runs.dir AS run_dir, ra.path AS rel_path, ra.sha256 AS sha256
        FROM run_artifacts AS ra
        JOIN runs ON runs.report_id = ra.report_id AND runs.idx = ra.run_idx
        """,
    ).fetchall()
    known: dict[Path, dict[str, str]] = {}
    for row in rows:
        run_dir = row["run_dir"]
        if not run_dir:
            continue
        try:
            key = Path(run_dir).resolve(strict=False)
        except OSError:
            continue
        known.setdefault(key, {})[row["rel_path"]] = row["sha256"]
    return known


def _walk_entries(root: Path):
    """Обход root: обычные файлы и каталоги-симлинки (без спуска в симлинки).

    Возвращает (kind, path), где kind — «file» или «symlink_dir». Служебные
    кэш-каталоги (__pycache__ и т.п.) пропускаем: это мусор, а не артефакты,
    поодиночке о них не сообщаем — подметаются вместе с опустевшим родителем.
    Каталоги-симлинки os.walk не обходит (followlinks=False), но мы их всё
    равно фиксируем отдельно: пользователь должен видеть, что под ними
    контент не проверялся."""
    for current_root, dir_names, file_names in os.walk(root):
        kept: list[str] = []
        for d in dir_names:
            path = Path(current_root) / d
            if d in _EXCLUDED_DIR_NAMES:
                continue
            if path.is_symlink():
                yield ("symlink_dir", path)
                continue  # не спускаемся — симлинк могли подменить.
            kept.append(d)
        dir_names[:] = kept
        for name in file_names:
            yield ("file", Path(current_root) / name)


def _classify_file(path: Path, known: dict[Path, dict[str, str]]):
    """Сопоставляет файл на диске с записями БД.

    Возвращает (category, detail):
      - ("confirmed", None) — каталог копии + rel + SHA совпали → кандидат;
      - ("mismatch", rel)   — каталог копии + rel известны, но SHA другой;
      - ("unknown", rel)    — нет такого (runs.dir, path) в БД;
      - ("symlink", None)   — симлинк (не обычный файл);
      - ("unreadable", exc) — OSError при чтении.
    """
    if path.is_symlink():
        return ("symlink", None)
    try:
        content = path.read_bytes()
    except OSError as exc:
        return ("unreadable", exc)
    sha = hashlib.sha256(content).hexdigest()

    # Файл может лежать прямо в work_dir (rel «run.log») или во вложенной папке
    # (rel «nested/data.bin»). Ищем каталог копии (runs.dir), которому файл
    # принадлежит: runs.dir — это предок файла внутри result_root. rel берётся
    # относительно этого каталога, что совпадает с run_artifacts.path.
    try:
        resolved = path.resolve(strict=False)
    except OSError:
        return ("unknown", path.name)

    for run_dir, files in known.items():
        try:
            rel = resolved.relative_to(run_dir).as_posix()
        except ValueError:
            continue  # файл не внутри этого каталога копии.
        known_sha = files.get(rel)
        if known_sha is None:
            continue  # в этом прогоне нет такого rel — но, возможно, есть в другом.
        # Каталог копии и rel совпали — это точно артефакт этого прогона;
        # SHA решает, удалять (confirmed) или файл менялся (mismatch).
        return ("confirmed", None) if known_sha == sha else ("mismatch", rel)
    return ("unknown", path.name)


def _prune_empty_dirs(root: Path) -> None:
    """Удаляет пустые каталоги под root снизу вверх, не трогая сам root.

    Выход за границу root физически невозможен: os.walk обходится внутри root,
    а сам root (и его возможный родитель) исключены из rmdir."""
    if not root.exists():
        return
    for current_root, dir_names, _files in os.walk(root, topdown=False):
        current = Path(current_root)
        for dir_name in dir_names:
            path = current / dir_name
            if path == root:
                continue
            try:
                path.rmdir()
            except OSError:
                pass  # непустая/занятая — штатный выход обхода.


def cmd_cleanup(args: argparse.Namespace) -> int:
    result_root: Path = args.result_root
    if not result_root.exists():
        print(f"result_root не существует: {result_root}")
        return 0

    conn = _conn(args.db)
    try:
        known = _load_known_artifacts(conn)
    finally:
        conn.close()

    confirmed: list[Path] = []
    mismatched: list[Path] = []
    unknown: list[Path] = []
    symlinks: list[Path] = []
    symlink_dirs: list[Path] = []
    unreadable: list[tuple[Path, str]] = []

    for kind, path in _walk_entries(result_root):
        if kind == "symlink_dir":
            symlink_dirs.append(path)
            continue
        category, detail = _classify_file(path, known)
        if category == "confirmed":
            confirmed.append(path)
        elif category == "mismatch":
            mismatched.append(path)
        elif category == "unknown":
            unknown.append(path)
        elif category == "symlink":
            symlinks.append(path)
        elif category == "unreadable":
            unreadable.append((path, str(detail)))

    print(f"result_root: {result_root}")
    print(f"Записей артефактов в БД: {len(known)}")
    print(f"Подтверждено по SHA (к удалению): {len(confirmed)}")
    print(f"Несовпадающих по SHA (mismatched): {len(mismatched)}")
    print(f"Неизвестных (нет в БД): {len(unknown)}")
    print(f"Симлинков-файлов: {len(symlinks)}")
    print(f"Каталогов-симлинков: {len(symlink_dirs)}")
    print(f"Нечитаемых: {len(unreadable)}")

    for p in sorted(mismatched):
        print(f"  [mismatch] {p}")
    for p in sorted(unknown):
        print(f"  [unknown]  {p}")
    for p in sorted(symlinks):
        print(f"  [symlink]  {p}")
    for p in sorted(symlink_dirs):
        print(f"  [symdir]   {p}")
    for p, exc in sorted(unreadable):
        print(f"  [unread]   {p}: {exc}")

    if not args.apply:
        print("\n[dry-run] изменений не внесено. Передайте --apply для удаления "
              "подтверждённых файлов.")
        return 0

    removed = 0
    for path in confirmed:
        try:
            path.unlink()
            removed += 1
        except FileNotFoundError:
            pass  # идемпотентность — файл уже исчез.
        except OSError as exc:
            print(f"warning: не удалось удалить {path}: {exc}", file=sys.stderr)
    _prune_empty_dirs(result_root)
    print(f"\nУдалено подтверждённых файлов: {removed}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH,
                        help="Путь к базе SQLite (по умолчанию: data/main.db)")
    parser.add_argument("--result-root", type=Path, default=_DEFAULT_RESULT_ROOT,
                        help="Корень очистки (по умолчанию: data/result/)")
    parser.add_argument("--apply", action="store_true",
                        help="Удалить подтверждённые файлы (без флага — dry-run)")
    args = parser.parse_args()
    raise SystemExit(cmd_cleanup(args))


if __name__ == "__main__":
    main()

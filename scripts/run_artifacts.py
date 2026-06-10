#!/usr/bin/env python3
"""List and export benchmark run artifacts stored in data/main.db."""

import argparse
import io
import sys
import zipfile
from collections import defaultdict
from pathlib import Path, PurePosixPath

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from artifacts import (  # noqa: E402
    cleanup_collected_artifacts,
    collect_artifacts_from_dirs,
)
from db import (  # noqa: E402
    DB_PATH,
    connect,
    init_schema,
    iter_artifact_contents,
    list_artifacts,
    read_artifact,
    replace_report_artifacts,
)


def _conn(path: Path):
    conn = connect(path)
    init_schema(conn)
    return conn


def _write_bytes(data: bytes, output: Path | None) -> None:
    if output is None:
        sys.stdout.buffer.write(data)
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(data)


def _report_json(conn, report_id: int) -> bytes:
    row = conn.execute(
        "SELECT raw_json FROM reports WHERE id = ?", (report_id,),
    ).fetchone()
    if row is None:
        raise SystemExit(f"report not found: {report_id}")
    return row["raw_json"].encode("utf-8")


def _safe_zip_name(path: str) -> str:
    posix = PurePosixPath(path)
    if posix.is_absolute() or ".." in posix.parts:
        raise ValueError(f"unsafe artifact path: {path}")
    return posix.as_posix()


def _zip_report(conn, report_id: int, run_idx: int | None = None) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("report.json", _report_json(conn, report_id))
        for artifact_idx, path, content in iter_artifact_contents(conn, report_id, run_idx):
            name = f"runs/{artifact_idx}/{_safe_zip_name(path)}"
            zf.writestr(name, content)
    return buffer.getvalue()


def cmd_list(args: argparse.Namespace) -> int:
    conn = _conn(args.db)
    try:
        rows = list_artifacts(conn, args.report_id, args.run_idx)
    finally:
        conn.close()
    for row in rows:
        print(
            f"{row['run_idx']}\t{row['kind']}\t{row['size_bytes']}\t"
            f"{row['sha256']}\t{row['path']}"
        )
    return 0


def cmd_extract(args: argparse.Namespace) -> int:
    conn = _conn(args.db)
    try:
        data = read_artifact(conn, args.report_id, args.run_idx, args.path)
    finally:
        conn.close()
    _write_bytes(data, args.output)
    return 0


def cmd_report_json(args: argparse.Namespace) -> int:
    conn = _conn(args.db)
    try:
        data = _report_json(conn, args.report_id)
    finally:
        conn.close()
    _write_bytes(data, args.output)
    return 0


def cmd_zip(args: argparse.Namespace) -> int:
    conn = _conn(args.db)
    try:
        data = _zip_report(conn, args.report_id, args.run_idx)
    finally:
        conn.close()
    _write_bytes(data, args.output)
    return 0


def _report_run_dirs(conn, report_id: int | None) -> dict[int, list[tuple[int, Path]]]:
    query = """
        SELECT reports.id AS report_id, runs.idx, runs.dir
        FROM reports
        JOIN runs ON runs.report_id = reports.id
    """
    params: list[object] = []
    if report_id is not None:
        query += " WHERE reports.id = ?"
        params.append(report_id)
    query += " ORDER BY reports.id, runs.idx"

    grouped: dict[int, list[tuple[int, Path]]] = defaultdict(list)
    for row in conn.execute(query, params):
        if row["dir"]:
            dir_path = Path(row["dir"])
            if not dir_path.is_absolute() or ".." in dir_path.parts:
                continue
            grouped[row["report_id"]].append((row["idx"], dir_path))
    return dict(grouped)


def cmd_backfill(args: argparse.Namespace) -> int:
    conn = _conn(args.db)
    total_reports = 0
    total_artifacts = 0
    total_missing = 0
    try:
        grouped = _report_run_dirs(conn, args.report_id)
        for report_id, runs in grouped.items():
            missing = 0
            existing_dirs: list[tuple[int, Path]] = []
            for run_idx, work_dir in runs:
                if not work_dir.exists():
                    missing += 1
                    continue
                existing_dirs.append((run_idx, work_dir))

            collection = collect_artifacts_from_dirs(existing_dirs)

            # Без артефактов базу не трогаем: пустой replace стёр бы уже
            # сохранённые артефакты отчёта (а папка с одним .DS_Store — обычное
            # дело после штатной зачистки). Мусор с диска всё же подметаем.
            if not collection.artifacts:
                if collection.trash_paths and not args.keep_files:
                    cleanup_collected_artifacts(collection)
                total_missing += missing
                continue
            # partial: трогаем только run_idx с реально собранными артефактами —
            # копии с уже зачищенными папками сохраняют свои маппинги в базе.
            with conn:
                replace_report_artifacts(conn, report_id, collection.artifacts,
                                         partial=True)
            if not args.keep_files:
                cleanup_collected_artifacts(collection)

            total_reports += 1
            total_artifacts += len(collection.artifacts)
            total_missing += missing
            print(
                f"report {report_id}: {len(collection.artifacts)} artifacts, "
                f"{len(collection.trash_paths)} trash, {missing} missing dirs"
            )
            for error in collection.errors:
                print(f"  warning: {error}", file=sys.stderr)
    finally:
        conn.close()

    print(
        f"done: {total_reports} reports, {total_artifacts} artifacts, "
        f"{total_missing} missing dirs"
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH,
                        help="SQLite DB path (default: data/main.db)")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List artifacts for a report")
    p_list.add_argument("report_id", type=int)
    p_list.add_argument("--run-idx", type=int)
    p_list.set_defaults(func=cmd_list)

    p_extract = sub.add_parser("extract", help="Extract one stored artifact")
    p_extract.add_argument("report_id", type=int)
    p_extract.add_argument("run_idx", type=int)
    p_extract.add_argument("path")
    p_extract.add_argument("-o", "--output", type=Path)
    p_extract.set_defaults(func=cmd_extract)

    p_report = sub.add_parser("report-json", help="Export report.json from raw_json")
    p_report.add_argument("report_id", type=int)
    p_report.add_argument("-o", "--output", type=Path)
    p_report.set_defaults(func=cmd_report_json)

    p_zip = sub.add_parser("zip", help="Export report.json and artifacts as zip")
    p_zip.add_argument("report_id", type=int)
    p_zip.add_argument("--run-idx", type=int)
    p_zip.add_argument("-o", "--output", type=Path)
    p_zip.set_defaults(func=cmd_zip)

    p_backfill = sub.add_parser("backfill", help="Import existing data/result files")
    p_backfill.add_argument("--report-id", type=int)
    p_backfill.add_argument("--keep-files", action="store_true",
                            help="Store artifacts but leave files on disk")
    p_backfill.set_defaults(func=cmd_backfill)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()

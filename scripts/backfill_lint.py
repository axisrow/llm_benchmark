#!/usr/bin/env python3
"""Дозаписывает/пересчитывает lint-метрику (runs[].linters, runs[].ruff,
lint_summary, ruff_summary) в raw_json отчётов — БЕЗ перепрогона моделей.

Гоняет ТЕКУЩИЙ реестр LINTERS (ruff/tidy/jq + js/node) по собранным артефактам
каждой code==0 копии. Цели:
  - диагностика: тест линтера на РЕАЛЬНЫХ артефактах базы (урок #130 — гонять
    на реальных данных, не только синтетических тестах);
  - backfill: отчёты, прогнанные ДО добавления нового линтера (напр. js), не
    имеют runs[].linters.<name> — этот скрипт пересчитывает все линтеры и
    вписывает их единообразно (формат байт-в-байт как benchmark_report._build_report).

Совпадает с bench.py: runs[].linters = {имя → {status, errors}}, runs[].ruff —
производный от линтера 'ruff' (#100), lint_summary = summarize_linters,
ruff_summary = summarize_lint. Артефакты читает из БД (file_blobs/run_artifacts);
НЕ трогает runs[].fine, usage, code, status — только lint-поля.

Запуск:
    python scripts/backfill_lint.py                       # dry-run по всем
    python scripts/backfill_lint.py --report-id 276       # точечно
    python scripts/backfill_lint.py --project library_fine
    python scripts/backfill_lint.py --apply               # записать
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from artifacts import ARTIFACT_KIND_AGENT_FILE, RunArtifact  # noqa: E402
from db import DB_PATH, upsert_report  # noqa: E402
from lint_metrics import (  # noqa: E402
    lint_copy_artifacts,
    summarize_lint,
    summarize_linters,
)


def _load_run_artifacts(conn: sqlite3.Connection,
                        report_id: int) -> dict[int, list[RunArtifact]]:
    """Артефакты отчёта, сгруппированные по run_idx (RunArtifact с .content).
    Только agent_file (как lint_metrics._artifacts_for фильтрует)."""
    from db import list_artifacts, read_artifact
    by_idx: dict[int, list[RunArtifact]] = {}
    for row in list_artifacts(conn, report_id):
        if row["kind"] != ARTIFACT_KIND_AGENT_FILE:
            continue
        content = read_artifact(conn, report_id, row["run_idx"], row["path"])
        by_idx.setdefault(row["run_idx"], []).append(RunArtifact(
            run_idx=row["run_idx"],
            path=row["path"],
            kind=row["kind"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            content=content,
            source_path=Path(row["path"]),
        ))
    return by_idx


def backfill_report(conn: sqlite3.Connection, report_id: int
                    ) -> tuple[dict | None, str]:
    """Пересчитывает runs[].linters/runs[].ruff/lint_summary/ruff_summary для
    отчёта. Возвращает (new_report|None, заметка). None — нет артефактов/копий."""
    row = conn.execute(
        "SELECT raw_json, rel_path FROM reports WHERE id=?", (report_id,)
    ).fetchone()
    if row is None:
        return None, f"report {report_id} не найден"
    report = json.loads(row["raw_json"])

    artifacts_by_idx = _load_run_artifacts(conn, report_id)
    if not artifacts_by_idx:
        return None, f"report {report_id}: нет agent_file-артефактов"

    any_lint = False
    for run in report.get("runs", []):
        idx = run.get("index")
        code = run.get("code")
        # gate code==0 — как bench.py (фейловые копии linters={} не получают).
        if code == 0 and idx in artifacts_by_idx:
            per_copy = lint_copy_artifacts(artifacts_by_idx[idx])
            run["linters"] = {
                name: {"status": r.status, "errors": r.errors}
                for name, r in per_copy.items()
            }
            ruff = per_copy.get("ruff")
            run["ruff"] = ({"status": ruff.status, "errors": ruff.errors}
                           if ruff is not None else None)
            run["lint"] = run["ruff"]  # синоним #100
            any_lint = True
        else:
            # фейловая копия — linters пуст, как в bench.py (ключи убираем).
            run["linters"] = {}
            run["ruff"] = None
            run["lint"] = None

    if not any_lint:
        return None, f"report {report_id}: нет code==0 копий с артефактами"

    report["lint_summary"] = summarize_linters(report["runs"])
    report["ruff_summary"] = summarize_lint(report["runs"])
    return report, ""


def _select_report_ids(conn: sqlite3.Connection, args) -> list[int]:
    if args.report_id is not None:
        return [args.report_id]
    base = "SELECT id FROM reports WHERE 1=1"
    params: list = []
    if args.project:
        base += " AND project=?"
        params.append(args.project)
    base += " ORDER BY id"
    return [r["id"] for r in conn.execute(base, params)]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Пересчёт lint-метрики (runs[].linters/lint_summary) в raw_json "
                    "отчётов по текущему реестру LINTERS (без перепрогона моделей).")
    ap.add_argument("--report-id", type=int, help="точечно по id отчёта")
    ap.add_argument("--project", help="только отчёты проекта (напр. library_fine)")
    ap.add_argument("--apply", action="store_true",
                    help="записать в БД (по умолчанию — dry-run)")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    ids = _select_report_ids(conn, args)
    if not ids:
        print("Нет отчётов для пересчёта lint-метрики.")
        return 0

    print(f"{'mode':8} reviews | lint-сводка по отчёту".replace("reviews", "report"))
    changed = 0
    for rid in ids:
        report, note = backfill_report(conn, rid)
        if report is None:
            print(f"  skip {rid}: {note}")
            continue
        ls = report.get("lint_summary", {})
        parts = []
        for name, s in ls.items():
            if not s or not s.get("checked"):
                continue
            parts.append(f"{name}={s['total_errors']}/{s['checked']}copy")
        summary_str = " ".join(parts) if parts else "(нет checked-линтеров)"
        print(f"  report {rid} [{report.get('project')}]: {summary_str}")
        if not args.apply:
            continue
        new_raw = json.dumps(report, ensure_ascii=False, indent=2)
        rel_path = conn.execute(
            "SELECT rel_path FROM reports WHERE id=?", (rid,)).fetchone()["rel_path"]
        with conn:
            upsert_report(conn, report, rel_path, new_raw)
        changed += 1

    print()
    if args.apply:
        print(f"ЗАПИСАНО: пересчитано {changed} отчётов(а)")
    else:
        print(f"(dry-run; --apply чтобы записать пересчёт для {len(ids)} отобранных)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Дозаписывает функциональную оценку library_fine (#126) в raw_json уже-собранных
отчётов — БЕЗ перепрогона моделей.

Грейдер library_fine подключён к bench.py (#126) и считается в момент построения
отчёта по собранным HTML-артефактам копий. Но отчёты, прогнанные ДО подключения
грейдера, остались без `runs[].fine` / `fine_summary` — на дашборде fine пуст.
HTML-артефакты при этом в базе ЕСТЬ (таблица run_artifacts/file_blobs), так что
оценку можно пересчитать из них.

Скрипт для каждого library_fine-отчёта:
  1. группирует собранные артефакты по run_idx;
  2. для code==0 копий гоняет grade_copy_artifacts (переиспользуем ядро #126 —
     тот же выбор лучшего HTML, те же статусы checked/na/unavailable/parse_error);
     фейловые копии (code!=0) fine не получают, как в bench.py;
  3. вписывает runs[].fine в raw_json и пересчитывает fine_summary = summarize_fine;
  4. пишет через db.upsert_report (атомарно raw_json + summary_* + перезапись runs;
     artifacts не передаём — run_artifacts/file_blobs уже выверены).

Совпадает с bench.py байт-в-байт: структура run.fine {status,passed,total,
autonomous,errors} (benchmark_report.py:538) и fine_summary (summarize_fine).
Byte-for-byte json.dumps(report, ensure_ascii=False, indent=2) — как save_report.

Read-only грейдер scripts/grade_library_fine.py — ЭТАЛОН: сухой прогон backfill
должен давать те же X/34 на артефактах, что и grade_library_fine.py.

Запуск:
    python scripts/backfill_fine.py --dry-run        # показать, что впишется
    python scripts/backfill_fine.py                  # точечно по id
    python scripts/backfill_fine.py --report-id 303
    python scripts/backfill_fine.py --all            # все library_fine-отчёты

БЕЗ --dry-run по умолчанию НЕ пишет (требует --apply), как delete_reports.py —
дозапись в закоммиченную data/main.db осознанна.
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
from library_fine_grading import (  # noqa: E402
    PROJECT_NAME,
    RunFineGradeResult,
    grade_copy_artifacts,
    summarize_fine,
)


def _fine_to_report(fine: RunFineGradeResult) -> dict:
    """RunFineGradeResult → runs[].fine (как benchmark_report.py:538)."""
    return {
        "status": fine.status,
        "passed": fine.passed,
        "total": fine.total,
        "autonomous": fine.autonomous,
        "errors": list(fine.errors),
    }


def _load_run_artifacts(conn: sqlite3.Connection,
                        report_id: int) -> dict[int, list[RunArtifact]]:
    """Артефакты отчёта, сгруппированные по run_idx (RunArtifact с .content)."""
    from db import list_artifacts, read_artifact  # поздний импорт
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
    """Считает runs[].fine + fine_summary для отчёта. Возвращает (new_report|None,
    заметка). None — отчёт не library_fine или нет артефактов/изменений."""
    row = conn.execute(
        "SELECT raw_json, rel_path FROM reports WHERE id=?", (report_id,)
    ).fetchone()
    if row is None:
        return None, f"report {report_id} не найден"
    report = json.loads(row["raw_json"])
    if report.get("project") != PROJECT_NAME:
        return None, f"report {report_id}: не library_fine ({report.get('project')})"

    artifacts_by_idx = _load_run_artifacts(conn, report_id)
    if not artifacts_by_idx:
        return None, f"report {report_id}: нет agent_file-артефактов"

    # summarize_fine читает .fine как ОБЪЕКТ (RunFineGradeResult.status), а не
    # dict — как в bench.py, где она зовётся ДО сериализации runs[].fine. Поэтому
    # сначала считаем сводку из объектов, потом сериализуем в dict.
    fine_results: dict[int, RunFineGradeResult] = {}
    for run in report.get("runs", []):
        idx = run.get("index")
        code = run.get("code")
        # gate code==0 — как bench.py: фейловые копии fine не получают.
        if code == 0 and idx in artifacts_by_idx:
            fine_results[idx] = grade_copy_artifacts(artifacts_by_idx[idx])
    if not fine_results:
        return None, f"report {report_id}: нет code==0 копий с артефактами"

    runs_for_summary = [{"fine": fr} for fr in fine_results.values()]
    report["fine_summary"] = summarize_fine(runs_for_summary)
    for run in report.get("runs", []):
        idx = run.get("index")
        if idx in fine_results:
            run["fine"] = _fine_to_report(fine_results[idx])
    return report, ""


def _select_report_ids(conn: sqlite3.Connection, args) -> list[int]:
    if args.report_id is not None:
        return [args.report_id]
    if args.all:
        return [r["id"] for r in conn.execute(
            "SELECT id FROM reports WHERE project=? ORDER BY id", (PROJECT_NAME,)
        )]
    # по умолчанию — library_fine-отчёты БЕЗ fine_summary (кандидаты на дозапись)
    return [r["id"] for r in conn.execute(
        """SELECT id FROM reports
           WHERE project=? AND json_extract(raw_json,'$.fine_summary') IS NULL
           ORDER BY id""", (PROJECT_NAME,)
    )]


def main() -> int:
    ap = argparse.ArgumentParser(
        description=(__doc__ or "").splitlines()[0] if __doc__ else "")
    ap.add_argument("--report-id", type=int, help="точечно по id отчёта")
    ap.add_argument("--all", action="store_true",
                    help="все library_fine-отчёты (даже с fine_summary)")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="показать, что впишется (по умолчанию)")
    ap.add_argument("--apply", dest="dry_run", action="store_false",
                    help="реально записать в БД")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    ids = _select_report_ids(conn, args)
    if not ids:
        print("Нет library_fine-отчётов для дозаписи fine.")
        return 0

    changed = 0
    for rid in ids:
        report, note = backfill_report(conn, rid)
        if report is None:
            print(f"  skip {rid}: {note}")
            continue
        summary = report["fine_summary"]
        graded = [(r["index"], r["fine"]["passed"], r["fine"]["total"])
                  for r in report["runs"]
                  if r.get("fine", {}).get("status") == "checked"]
        scores = " ".join(f"#{i}={p}/{t}" for i, p, t in graded)
        print(f"  report {rid}: fine_summary passed/total="
              f"{summary.get('passed')}/{summary.get('total')} "
              f"checked={summary.get('checked')} | {scores}")
        if args.dry_run:
            continue
        new_raw = json.dumps(report, ensure_ascii=False, indent=2)
        rel_path = conn.execute(
            "SELECT rel_path FROM reports WHERE id=?", (rid,)).fetchone()["rel_path"]
        with conn:
            upsert_report(conn, report, rel_path, new_raw)
        changed += 1

    mode = "DRY-RUN" if args.dry_run else "APPLIED"
    print(f"\n{mode}: обработано {len(ids)} отчётов"
          + (f", записано {changed}" if not args.dry_run else ""))
    if args.dry_run and changed == 0 and any(True for _ in ids):
        print("Перезапустите с --apply для записи в БД.")
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Чистка отдельных прогонов (runs) из базы: ошибки и ложные таймауты.

Удаляет:
  - все прогоны со status='ошибка';
  - ложные таймауты (status='таймаут' AND elapsed<130с — кластер graceful-close SSE).
Настоящие таймауты (>=130с: ~454с по бюджету и аномалия ~1804с) НЕ трогаются.

Правит ТОЛЬКО SQL-таблицы (runs, summary_* колонки, reports.copies, run_artifacts,
file_blobs). raw_json НЕ трогается (осознанный выбор: index_builder читает рейтинг
из raw_json, так что рейтинг на Pages при этом подходе не изменится).

Затронутые отчёты получают пересчитанные summary_*/copies из оставшихся runs.
Отчёты, опустевшие после удаления, удаляются целиком (каскад уберёт их артефакты).

Запуск:
    python scripts/cleanup_runs.py --dry-run
    python scripts/cleanup_runs.py
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db

# Что считаем мусором на уровне отдельного прогона.
JUNK_RUN = "status = 'ошибка' OR (status = 'таймаут' AND elapsed < 130)"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    conn = db.connect()
    try:
        errors = conn.execute(
            "SELECT count(*) FROM runs WHERE status='ошибка'").fetchone()[0]
        false_to = conn.execute(
            "SELECT count(*) FROM runs WHERE status='таймаут' AND elapsed<130"
        ).fetchone()[0]
        affected_reports = conn.execute(
            f"SELECT count(DISTINCT report_id) FROM runs WHERE {JUNK_RUN}"
        ).fetchone()[0]
        arts = conn.execute(
            f"SELECT count(*) FROM run_artifacts ra WHERE EXISTS ("
            f"  SELECT 1 FROM runs ru WHERE ru.report_id=ra.report_id "
            f"  AND ru.idx=ra.run_idx AND ({JUNK_RUN}))"
        ).fetchone()[0]

        print(f"К удалению: ошибок={errors}, ложных таймаутов={false_to}; "
              f"затронуто отчётов={affected_reports}, артефактов={arts}")

        if errors == 0 and false_to == 0:
            print("Чистить нечего.")
            return 0

        if args.dry_run:
            # Покажем, какие отчёты опустеют (останется 0 runs).
            empties = conn.execute(
                f"SELECT r.id, r.project, r.model FROM reports r WHERE "
                f"(SELECT count(*) FROM runs ru WHERE ru.report_id=r.id) = "
                f"(SELECT count(*) FROM runs ru WHERE ru.report_id=r.id AND ({JUNK_RUN}))"
                f" AND EXISTS (SELECT 1 FROM runs ru WHERE ru.report_id=r.id)"
            ).fetchall()
            print(f"Отчётов опустеет (будут удалены целиком): {len(empties)}")
            for rid, project, model in empties:
                print(f"  - id={rid} {project}/{model}")
            print("\n[dry-run] изменений не внесено.")
            return 0

        with conn:
            # 1) Артефакты удаляемых прогонов (точечно по report_id+run_idx).
            conn.execute(
                f"DELETE FROM run_artifacts WHERE (report_id, run_idx) IN ("
                f"  SELECT report_id, idx FROM runs WHERE {JUNK_RUN})")
            # 2) Сами прогоны.
            conn.execute(f"DELETE FROM runs WHERE {JUNK_RUN}")
            # 3) Опустевшие отчёты — удалить целиком (каскад уберёт их артефакты).
            empties = conn.execute(
                "DELETE FROM reports WHERE id NOT IN "
                "(SELECT DISTINCT report_id FROM runs)")
            # 4) Пересчёт summary_*/copies из оставшихся runs.
            conn.execute("""
                UPDATE reports SET
                    summary_ok = (SELECT count(*) FROM runs ru
                                  WHERE ru.report_id=reports.id AND ru.code=0),
                    summary_timeout = (SELECT count(*) FROM runs ru
                                  WHERE ru.report_id=reports.id AND ru.code=1),
                    summary_error = (SELECT count(*) FROM runs ru
                                  WHERE ru.report_id=reports.id AND ru.code=2),
                    copies = (SELECT count(*) FROM runs ru
                                  WHERE ru.report_id=reports.id)
            """)
            # 5) Осиротевшие блобы.
            blobs = db.prune_orphan_blobs(conn)
            print(f"\nУдалено: прогонов={errors + false_to}, "
                  f"опустевших отчётов={empties.rowcount}, "
                  f"осиротевших блобов={blobs}")

        rep = conn.execute("SELECT count(*) FROM reports").fetchone()[0]
        runs = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
        print(f"Осталось: reports={rep}, runs={runs}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

"""Удаление полностью ложных таймаут-отчётов (баг graceful-close SSE, ~124с).

Ложный таймаут — прогон со status='таймаут' и elapsed<130с (кластер 123.7-124.5с,
сервер закрывал GET /event без session.idle). Удаляем ТОЛЬКО отчёты, где ВСЕ копии
такие (полностью ложные) — частичные отчёты не трогаем. runs/run_artifacts уходят
каскадом (ON DELETE CASCADE), осиротевшие file_blobs чистим вручную.

Запуск:
    python scripts/cleanup_false_timeouts.py --dry-run   # только показать
    python scripts/cleanup_false_timeouts.py             # удалить
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db

FALSE_TIMEOUT = "status = 'таймаут' AND elapsed < 130"

# Отчёты, где КАЖДАЯ копия — ложный таймаут.
FULLY_FALSE_REPORTS_SQL = f"""
SELECT r.id
FROM reports r
JOIN runs ru ON ru.report_id = r.id
GROUP BY r.id
HAVING count(*) = sum(CASE WHEN {FALSE_TIMEOUT} THEN 1 ELSE 0 END)
"""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true",
                        help="показать, что будет удалено, ничего не меняя")
    args = parser.parse_args()

    conn = db.connect()
    try:
        report_ids = [row[0] for row in conn.execute(FULLY_FALSE_REPORTS_SQL)]
        if report_ids:
            placeholders = ",".join("?" * len(report_ids))
            rows = conn.execute(
                f"SELECT project, provider, model, started_at, copies "
                f"FROM reports WHERE id IN ({placeholders}) "
                f"ORDER BY project, model", report_ids).fetchall()
            print(f"Полностью ложных отчётов: {len(report_ids)}")
            for project, provider, model, started_at, copies in rows:
                print(f"  - {project} | {provider}/{model} | {started_at} | "
                      f"{copies} копий")
        else:
            print("Полностью ложных отчётов нет.")

        # Осиротевшие runs/run_artifacts: ссылаются на report_id, которых уже нет
        # в reports (наследие удалений при выключенных foreign keys — каскад тогда
        # не отработал). Дочищаем вместе с ложными таймаутами.
        orphan_runs = conn.execute(
            "SELECT count(*) FROM runs ru WHERE NOT EXISTS "
            "(SELECT 1 FROM reports r WHERE r.id = ru.report_id)").fetchone()[0]
        orphan_arts = conn.execute(
            "SELECT count(*) FROM run_artifacts a WHERE NOT EXISTS "
            "(SELECT 1 FROM reports r WHERE r.id = a.report_id)").fetchone()[0]
        if orphan_runs or orphan_arts:
            print(f"Осиротевших записей без отчёта: runs={orphan_runs}, "
                  f"run_artifacts={orphan_arts}")

        if args.dry_run:
            print("\n[dry-run] изменений не внесено.")
            return 0

        if not report_ids and not orphan_runs and not orphan_arts:
            print("Чистить нечего.")
            return 0

        with conn:
            # delete_report сносит runs/run_artifacts каскадом и подметает блобы.
            for rid in report_ids:
                db.delete_report(conn, rid)
            # Осиротевшие строки без родительского отчёта (каскад тут не поможет).
            conn.execute(
                "DELETE FROM runs AS ru WHERE NOT EXISTS "
                "(SELECT 1 FROM reports r WHERE r.id = ru.report_id)")
            conn.execute(
                "DELETE FROM run_artifacts AS a WHERE NOT EXISTS "
                "(SELECT 1 FROM reports r WHERE r.id = a.report_id)")
            # Блобы, осиротевшие после дочистки run_artifacts выше.
            orphan_blobs = db.prune_orphan_blobs(conn)
            print(f"\nУдалено отчётов: {len(report_ids)}; "
                  f"осиротевших runs: {orphan_runs}, "
                  f"run_artifacts: {orphan_arts}, блобов: {orphan_blobs}")

        remaining = conn.execute("SELECT count(*) FROM reports").fetchone()[0]
        remaining_runs = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
        print(f"Осталось: reports={remaining}, runs={remaining_runs}")
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

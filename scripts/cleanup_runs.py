"""Чистка отдельных прогонов (runs) из базы: ошибки и ложные таймауты.

Удаляет:
  - все прогоны с code=2 («ошибка»);
  - ложные таймауты (code=1 AND elapsed<130с — кластер graceful-close SSE).
Настоящие таймауты (>=130с: ~454с по бюджету и аномалия ~1804с) НЕ трогаются.

Правит SQL-таблицы (runs, run_artifacts, file_blobs) и пересобирает raw_json
затронутых отчётов из выживших runs — через ту же таксономию RUN_CODES, что и
regenerate_raw_json. Так дашборд (index_builder читает рейтинг ТОЛЬКО из raw_json)
и SQL-срез summary_*/copies остаются согласованными, включая выжившие code=3
(rate_limited), для которых отдельной summary-колонки нет (живёт лишь в raw_json).

Отчёты, опустевшие после удаления, удаляются целиком (каскад уберёт их артефакты).

NB: отчёты, уже почищенные СТАРОЙ версией скрипта (когда raw_json не трогался),
этот проход не подхватит — их junk-прогонов в `runs` уже нет, поэтому они не
попадут в `affected_surviving`. Для разовой починки такого legacy-рассинхрона
прогони `python scripts/regenerate_raw_json.py --all` (идемпотентно).

Запуск:
    python scripts/cleanup_runs.py --dry-run
    python scripts/cleanup_runs.py
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # корень — import db
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts — regenerate_raw_json

import db
from _common import add_dry_run
from regenerate_raw_json import regenerate_one

# Порог ложного таймаута — единый доменный инвариант из db (db.FALSE_TIMEOUT_SQL).
FALSE_TIMEOUT = db.FALSE_TIMEOUT_SQL
# Что считаем мусором на уровне отдельного прогона.
JUNK_RUN = f"code = 2 OR ({FALSE_TIMEOUT})"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_dry_run(parser)
    args = parser.parse_args()

    with db.session() as conn:
        errors = conn.execute("SELECT count(*) FROM runs WHERE code=2").fetchone()[0]
        false_to = conn.execute(
            f"SELECT count(*) FROM runs WHERE {FALSE_TIMEOUT}"
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

        # Отчёты, которые опустеют ИМЕННО в этом проходе: у них есть runs, и ВСЕ
        # они — junk. Это единая выборка для dry-run preview и реального DELETE
        # (см. баг B11: раньше реальный DELETE сносил любой отчёт без runs,
        # включая легитимные пустые, а preview показывал только опустевшие тут).
        EMPTIED_THIS_PASS = (
            "(SELECT count(*) FROM runs ru WHERE ru.report_id=r.id) = "
            f"(SELECT count(*) FROM runs ru WHERE ru.report_id=r.id AND ({JUNK_RUN}))"
            " AND EXISTS (SELECT 1 FROM runs ru WHERE ru.report_id=r.id)"
        )

        if args.dry_run:
            # Покажем, какие отчёты опустеют (останется 0 runs).
            empties = conn.execute(
                f"SELECT r.id, r.project, r.model FROM reports r "
                f"WHERE {EMPTIED_THIS_PASS}"
            ).fetchall()
            print(f"Отчётов опустеет (будут удалены целиком): {len(empties)}")
            for rid, project, model in empties:
                print(f"  - id={rid} {project}/{model}")
            print("\n[dry-run] изменений не внесено.")
            return 0

        with conn:
            # ДО удаления junk-прогонов фиксируем id отчётов, которые опустеют
            # в этом проходе — после DELETE FROM runs отличить их от легитимно
            # пустых отчётов уже нельзя.
            empty_ids = [
                row[0] for row in conn.execute(
                    f"SELECT r.id FROM reports r WHERE {EMPTIED_THIS_PASS}")
            ]
            # Отчёты с junk-прогонами, которые НЕ опустеют — им (ДО удаления runs)
            # фиксируем id, чтобы потом пересобрать raw_json из выживших.
            empty_set = set(empty_ids)
            affected_surviving = [
                row[0] for row in conn.execute(
                    f"SELECT DISTINCT report_id FROM runs WHERE {JUNK_RUN}")
                if row[0] not in empty_set
            ]
            # 1) Артефакты удаляемых прогонов (точечно по report_id+run_idx).
            conn.execute(
                f"DELETE FROM run_artifacts WHERE (report_id, run_idx) IN ("
                f"  SELECT report_id, idx FROM runs WHERE {JUNK_RUN})")
            # 2) Сами прогоны.
            conn.execute(f"DELETE FROM runs WHERE {JUNK_RUN}")
            # 3) Опустевшие В ЭТОМ ПРОХОДЕ отчёты — удалить целиком (каскад уберёт
            #    их артефакты). Легитимные отчёты без runs не трогаем.
            if empty_ids:
                placeholders = ", ".join("?" * len(empty_ids))
                empties = conn.execute(
                    f"DELETE FROM reports WHERE id IN ({placeholders})",
                    empty_ids)
            else:
                empties = conn.execute(
                    "DELETE FROM reports WHERE 0")
            # 4) Пересобрать raw_json/summary/copies затронутых выживших отчётов
            #    из выживших runs тем же путём, что и regenerate_raw_json — единая
            #    таксономия RUN_CODES, чтобы SQL-срез и raw_json сошлись, а code=3
            #    (без своей summary-колонки) учёлся в summary внутри raw_json.
            for rid in affected_surviving:
                regenerate_one(conn, rid, dry_run=False)
            # 5) Осиротевшие блобы.
            blobs = db.prune_orphan_blobs(conn)
            print(f"\nУдалено: прогонов={errors + false_to}, "
                  f"опустевших отчётов={empties.rowcount}, "
                  f"осиротевших блобов={blobs}")

        rep = conn.execute("SELECT count(*) FROM reports").fetchone()[0]
        runs = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
        print(f"Осталось: reports={rep}, runs={runs}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

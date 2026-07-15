"""Чистка отдельных прогонов (runs) из базы по предикату.

Два режима (предикат выбора мусора):

  (по умолчанию)  code=2 («ошибка») и ложные таймауты
                   (code=1 AND elapsed<FALSE_TIMEOUT_MAX_ELAPSED — кластер
                   graceful-close SSE). Настоящие таймауты (>=130с) НЕ трогаются.
  --no-html       прогоны проекта library_fine БЕЗ HTML-копии (.html/.htm
                   agent_file) — лог-мусор, не оцениваемый по матрице 34 кейсов
                   (#126). Неуспешные копии и прочие проекты не затрагивает.

Механика обоих режимов общая: удаляются прогоны по предикату, пересобирается
raw_json выживших (через regenerate_raw_json — единая таксономия RUN_CODES, включая
пересчёт lint/ruff/artifact_summary), опустевшие отчёты сносятся целиком (каскад
убирает их артефакты), осиротевшие file_blobs подметаются. Так дашборд
(index_builder читает рейтинг ТОЛЬКО из raw_json) и SQL-срез summary_*/copies
остаются согласованными.

NB: отчёты, уже почищенные СТАРОЙ версией скрипта (когда raw_json не трогался),
этот проход не подхватит — их junk-прогонов в `runs` уже нет, поэтому они не
попадут в `affected_surviving`. Для разовой починки такого legacy-рассинхрона
прогони `python scripts/regenerate_raw_json.py --all` (идемпотентно).

Запуск:
    python scripts/cleanup_runs.py --dry-run            # code=2/ложные таймауты
    python scripts/cleanup_runs.py                      # применить (code=2/…)
    python scripts/cleanup_runs.py --no-html --dry-run  # library_fine без HTML
    python scripts/cleanup_runs.py --no-html            # применить (--no-html)
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
# Контракт #54: destructive-скрипты берут предикат из db, не свой литерал 130 —
# иначе при перетюне порога скрипты разойдутся и повредят базу.
FALSE_TIMEOUT = db.FALSE_TIMEOUT_SQL

# Предикаты выбора мусора: WHERE-условие на строке runs С АЛИАСОМ ru.
# Оба подставляются в SQL вида «... FROM runs ru WHERE <PREDICATE>».
# JUNK_RUN_RU — форма db.FALSE_TIMEOUT_SQL, привязанная к алиасу ru (числовой
# порог — из db.FALSE_TIMEOUT_MAX_ELAPSED, литерал не дублируется).
JUNK_RUN_RU = (f"ru.code = 2 OR (ru.code = 1 AND ru.elapsed < "
               f"{db.FALSE_TIMEOUT_MAX_ELAPSED})")

# Прогон library_fine без HTML-копии: нет agent_file с .html/.htm В ЭТОМ отчёте.
# Прогон library_fine без HTML-копии: нет agent_file с .html/.htm В ЭТОМ отчёте.
# Корреляция ra.report_id = ru.report_id обязательна — иначе NOT IN смотрит во
# ВСЕ отчёты (run_idx 1/2/3 есть везде) и возвращает 5 вместо 47.
# ru.code = 0 обязателен: у неуспешной копии (timeout/error) HTML естественно нет,
# и чистка «без HTML» стёрла бы сам факт провала модели. Удаляем только
# УСПЕШНЫЕ копии без HTML — лог-мусор при code==0 (согласовано с gate #128:
# неуспешные копии не оцениваются, нечего и чистить).
NO_HTML_RUN = (
    "ru.code = 0 AND "
    "ru.report_id IN (SELECT id FROM reports WHERE project='library_fine') "
    "AND ru.idx NOT IN ("
    "  SELECT ra.run_idx FROM run_artifacts ra "
    "  WHERE ra.report_id = ru.report_id AND ra.kind='agent_file' "
    "  AND (lower(ra.path) LIKE '%.html' OR lower(ra.path) LIKE '%.htm'))"
)


def purge_runs_by_predicate(conn, predicate_sql: str, *, apply: bool) -> dict:
    """Удаляет прогоны по предикату, пересобирает выживших, сносит опустевшие
    отчёты, подметает блобы.

    predicate_sql — WHERE-условие на строке runs С АЛИАСОМ ru (JUNK_RUN_RU для
    ошибок/ложных таймаутов, NO_HTML_RUN для library_fine без HTML). Корреляция
    с run_artifacts через ra.report_id=ru.report_id — ответственность вызывающего
    (в предикате).

    apply=False — dry-run: только preview (кандидаты + опустевшие отчёты), без
    записи. apply=True — атомарная транзакция (DELETE артефактов → DELETE runs →
    DELETE опустевших отчётов → regenerate_one выживших → prune_orphan_blobs).

    Возвращает счётчики {candidates, emptied, artifacts_deleted,
    survivors_rebuilt, orphan_blobs}; в dry-run добавляет empties_preview.
    """
    P = predicate_sql

    # Отчёты, которые опустеют ИМЕННО в этом проходе: у них есть runs, и ВСЕ
    # они — кандидаты. Единая выборка для dry-run preview и реального DELETE
    # (баг B11: раньше реальный DELETE сносил любой отчёт без runs, включая
    # легитимные пустые, а preview показывал только опустевшие тут).
    EMPTIED_THIS_PASS = (
        "(SELECT count(*) FROM runs ru WHERE ru.report_id=r.id) = "
        f"(SELECT count(*) FROM runs ru WHERE ru.report_id=r.id AND ({P}))"
        " AND EXISTS (SELECT 1 FROM runs ru WHERE ru.report_id=r.id)"
    )

    candidates = conn.execute(
        f"SELECT count(*) FROM runs ru WHERE {P}").fetchone()[0]

    if not apply:
        empties = conn.execute(
            f"SELECT r.id, r.project, r.model FROM reports r "
            f"WHERE {EMPTIED_THIS_PASS}"
        ).fetchall()
        return {"candidates": candidates, "emptied": len(empties),
                "empties_preview": empties, "artifacts_deleted": 0,
                "survivors_rebuilt": 0, "orphan_blobs": 0}

    if candidates == 0:
        return {"candidates": 0, "emptied": 0, "artifacts_deleted": 0,
                "survivors_rebuilt": 0, "orphan_blobs": 0}

    with conn:
        # ДО удаления фиксируем id отчётов, которые опустеют в этом проходе —
        # после DELETE FROM runs отличить их от легитимно пустых уже нельзя.
        empty_ids = [
            row[0] for row in conn.execute(
                f"SELECT r.id FROM reports r WHERE {EMPTIED_THIS_PASS}")]
        empty_set = set(empty_ids)
        # Отчёты с кандидатами, которые НЕ опустеют — им (ДО удаления runs)
        # фиксируем id, чтобы потом пересобрать raw_json из выживших.
        affected_surviving = [
            row[0] for row in conn.execute(
                f"SELECT DISTINCT report_id FROM runs ru WHERE {P}")
            if row[0] not in empty_set]
        # 1) Артефакты удаляемых прогонов (точечно по report_id+run_idx).
        cur_arts = conn.execute(
            f"DELETE FROM run_artifacts WHERE (report_id, run_idx) IN ("
            f"  SELECT ru.report_id, ru.idx FROM runs ru WHERE {P})")
        # 2) Сами прогоны — по паре (report_id, idx): idx НЕ глобально уникален
        #    (PK runs = report_id+idx), удалять только по idx нельзя — заденет
        #    чужие отчёты с тем же idx.
        conn.execute(f"DELETE FROM runs WHERE (report_id, idx) IN ("
                     f"  SELECT ru.report_id, ru.idx FROM runs ru WHERE {P})")
        # 3) Опустевшие В ЭТОМ ПРОХОДЕ отчёты — целиком (каскад уберёт артефакты).
        if empty_ids:
            placeholders = ", ".join("?" * len(empty_ids))
            conn.execute(f"DELETE FROM reports WHERE id IN ({placeholders})",
                         empty_ids)
        # 4) Пересобрать raw_json/summary/copies/сводки затронутых выживших тем
        #    же путём, что и regenerate_raw_json — единая таксономия RUN_CODES,
        #    чтобы SQL-срез и raw_json сошлись, а code=3 (без своей summary-колонки)
        #    учёлся в summary внутри raw_json.
        for rid in affected_surviving:
            regenerate_one(conn, rid, dry_run=False)
        # 5) Осиротевшие блобы.
        blobs = db.prune_orphan_blobs(conn)
        return {"candidates": candidates, "emptied": len(empty_ids),
                "artifacts_deleted": cur_arts.rowcount,
                "survivors_rebuilt": len(affected_surviving),
                "orphan_blobs": blobs}


def _print_result(result: dict, label: str, *, apply: bool) -> None:
    print(f"Режим: {label}")
    if apply:
        print(f"К удалению: прогонов={result['candidates']}; затронуто "
              f"опустевших отчётов={result['emptied']}")
        print(f"Удалено: прогонов={result['candidates']}, "
              f"опустевших отчётов={result['emptied']}, "
              f"осиротевших блобов={result['orphan_blobs']}")
    else:
        print(f"К удалению: прогонов={result['candidates']}; "
              f"отчётов опустеет (будут удалены целиком)={result['emptied']}")
        for rid, project, model in result["empties_preview"]:
            print(f"  - id={rid} {project}/{model}")
        print("\n[dry-run] изменений не внесено.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_dry_run(parser)
    parser.add_argument(
        "--no-html", action="store_true",
        help="удалить прогоны library_fine без HTML-копии (#126) вместо "
             "стандартной чистки code=2/ложных таймаутов")
    args = parser.parse_args()

    if args.no_html:
        predicate, label = NO_HTML_RUN, "library_fine без HTML (#126)"
    else:
        predicate, label = JUNK_RUN_RU, "code=2 / ложные таймауты"

    with db.session() as conn:
        result = purge_runs_by_predicate(conn, predicate, apply=not args.dry_run)
        _print_result(result, label, apply=not args.dry_run)

        if not args.dry_run:
            rep = conn.execute("SELECT count(*) FROM reports").fetchone()[0]
            runs = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
            print(f"Осталось: reports={rep}, runs={runs}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

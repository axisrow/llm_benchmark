#!/usr/bin/env python3
"""Функциональная оценка HTML-артефактов library_fine из data/main.db (#119).

Извлекает встроенный JS каждого HTML-артефакта, исполняет его во встраиваемом
движке (mini-racer/quickjs, без браузера), вызывает функцию расчёта на матрице
комбинаций и сравнивает с Python-эталоном (library_fine_grading). Результаты
печатает на stdout; базу НЕ меняет (решение #119: сначала посчитать, хранение —
отдельно).

Режимы:
  (по умолчанию)   строка на артефакт + сводка
  -v               дополнительно перечислить несовпавшие комбинации
  --json           машиночитаемый дамп оценок
  --calibrate      по каждой комбинации с расхождениями: эталон, консенсус
                   реализаций, разброс значений (инструмент проверки I1–I8)
  --dump-expected  напечатать эталонный вектор матрицы (для снапшот-фикстуры)
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from dataclasses import asdict
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from db import DB_PATH  # noqa: E402
from library_fine_grading import (  # noqa: E402
    GRADE_STATUS_GRADED,
    TEST_MATRIX,
    ArtifactGrade,
    HtmlGrade,
    calibrate,
    expected_vector,
    grade_report,
)

PROJECT_NAME = "library_fine"


def _conn(path: Path) -> sqlite3.Connection:
    """Открывает снимок существующей базы строго read-only, без sidecar-файлов."""
    # База проекта — коммитящийся снимок. immutable не создаёт -wal/-shm и тем
    # самым сохраняет на диске ровно тот набор байтов, который CLI оценивает.
    uri = f"{path.resolve().as_uri()}?mode=ro&immutable=1"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _report_ids(conn: sqlite3.Connection,
                requested: list[int] | None) -> list[int]:
    """Отчёты с HTML-артефактами агента: явно заданные либо все library_fine."""
    if requested:
        return requested
    rows = conn.execute(
        """
        SELECT DISTINCT reports.id
        FROM reports
        JOIN run_artifacts ON run_artifacts.report_id = reports.id
        WHERE reports.project = ?
          AND run_artifacts.kind = 'agent_file'
          AND (lower(run_artifacts.path) LIKE '%.html'
               OR lower(run_artifacts.path) LIKE '%.htm')
        ORDER BY reports.id
        """,
        (PROJECT_NAME,),
    )
    return [row["id"] for row in rows]


def _model_by_report(conn: sqlite3.Connection,
                     report_ids: list[int]) -> dict[int, str]:
    if not report_ids:
        return {}
    marks = ",".join("?" * len(report_ids))
    rows = conn.execute(
        f"SELECT id, provider, model FROM reports WHERE id IN ({marks})",
        report_ids,
    )
    return {row["id"]: f"{row['provider']}/{row['model']}" for row in rows}


def _print_grade_line(ag: ArtifactGrade, model: str) -> None:
    g = ag.grade
    autonomous = "yes" if not g.autonomy_violations else "NO"
    extra = ""
    if g.status == GRADE_STATUS_GRADED:
        extra = (f"func={g.function_name} adapter={g.adapter} "
                 f"score={g.passed}/{g.total}")
    elif g.error:
        extra = f"error={g.error}"
    warn = f" warn={g.exec_warning}" if g.exec_warning else ""
    print(f"report {ag.report_id} run {ag.run_idx}  {ag.path}  "
          f"sha={ag.sha256[:8]}  {model}  status={g.status}  {extra}  "
          f"autonomous={autonomous}{warn}")


def _print_mismatches(grade: HtmlGrade) -> None:
    for outcome in grade.outcomes:
        if outcome.match:
            continue
        actual = outcome.error if outcome.actual is None else outcome.actual
        print(f"    ✗ {outcome.case_name}: эталон {outcome.expected}, "
              f"факт {actual}")


def _print_summary(grades: list[ArtifactGrade]) -> None:
    statuses: dict[str, int] = {}
    for ag in grades:
        statuses[ag.grade.status] = statuses.get(ag.grade.status, 0) + 1
    graded = [ag for ag in grades if ag.grade.status == GRADE_STATUS_GRADED]
    print()
    print(f"итого артефактов: {len(grades)}  "
          + "  ".join(f"{k}={v}" for k, v in sorted(statuses.items())))
    if graded:
        avg = sum(ag.grade.passed / ag.grade.total for ag in graded) / len(graded)
        print(f"средний счёт по graded: {avg:.1%} "
              f"(матрица: {len(TEST_MATRIX)} комбинаций)")
    violators = [ag for ag in grades if ag.grade.autonomy_violations]
    if violators:
        print("нарушители автономности:")
        seen: set[str] = set()
        for ag in violators:
            if ag.sha256 in seen:
                continue
            seen.add(ag.sha256)
            for violation in ag.grade.autonomy_violations:
                print(f"  {ag.sha256[:8]} ({ag.path}): {violation}")
    else:
        print("нарушителей автономности нет")


def _print_calibration(grades: list[ArtifactGrade]) -> None:
    """Кейсы, где хотя бы одна реализация разошлась с эталоном: консенсус и
    разброс. Полное совпадение всех реализаций с эталоном не печатается."""
    rows = calibrate(grades)
    disagreements = [row for row in rows
                     if row["n_values"]
                     and (row["consensus"] != row["reference"]
                          or row["consensus_count"] != row["n_values"]
                          or row["errors"])]
    print(f"калибровка: расхождения в {len(disagreements)} из {len(rows)} комбинаций")
    consensus_vs_ref = [row for row in disagreements
                        if row["consensus"] != row["reference"]
                        and row["consensus_count"] > row["n_values"] / 2]
    if consensus_vs_ref:
        print(f"⚠ консенсус (>50% реализаций) ПРОТИВ эталона: "
              f"{len(consensus_vs_ref)} комбинаций — проверить интерпретации I1–I8")
    for row in disagreements:
        spread: dict[float, int] = {}
        for value in row["values"].values():
            spread[value] = spread.get(value, 0) + 1
        spread_text = ", ".join(
            f"{v}×{n}" for v, n in sorted(spread.items(), key=lambda x: -x[1]))
        err_text = f"  ошибок: {len(row['errors'])}" if row["errors"] else ""
        marker = "⚠" if row in consensus_vs_ref else " "
        print(f" {marker} {row['case']}: эталон {row['reference']}, "
              f"значения [{spread_text}]{err_text}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=DB_PATH,
                        help="путь к SQLite-базе (по умолчанию data/main.db)")
    parser.add_argument("--report-id", type=int, action="append",
                        help="оценить только указанные отчёты (повторяемый)")
    parser.add_argument("--engine", choices=("auto", "mini-racer", "quickjs"),
                        default="auto", help="JS-движок (по умолчанию auto)")
    parser.add_argument("--json", action="store_true",
                        help="машиночитаемый дамп оценок на stdout")
    parser.add_argument("--calibrate", action="store_true",
                        help="по-комбинационная сверка эталона с реализациями")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="перечислить несовпавшие комбинации")
    parser.add_argument("--dump-expected", action="store_true",
                        help="напечатать эталонный вектор матрицы и выйти")
    args = parser.parse_args()

    # Даты в комбинациях локальные; фиксируем таймзону, чтобы счёт «X из Y» был
    # воспроизводим между машинами (Date-хелперы моделей бывают local и UTC).
    os.environ["TZ"] = "UTC"
    time.tzset()

    if args.dump_expected:
        print(json.dumps(expected_vector(), ensure_ascii=False, indent=2,
                         sort_keys=True))
        return 0

    conn = _conn(args.db)
    try:
        report_ids = _report_ids(conn, args.report_id)
        if not report_ids:
            print("нет отчётов library_fine с HTML-артефактами", file=sys.stderr)
            return 1
        cache: dict[str, HtmlGrade] = {}
        grades: list[ArtifactGrade] = []
        for report_id in report_ids:
            grades.extend(grade_report(conn, report_id,
                                       prefer_engine=args.engine, cache=cache))
        models = _model_by_report(conn, report_ids)
    finally:
        conn.close()

    if not grades:
        print("в выбранных отчётах нет HTML-артефактов", file=sys.stderr)
        return 1

    if args.json:
        payload = [{**asdict(ag),
                    "grade": {**asdict(ag.grade),
                              "outcomes": [asdict(o) for o in ag.grade.outcomes]}}
                   for ag in grades]
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    for ag in grades:
        _print_grade_line(ag, models.get(ag.report_id, "?"))
        if args.verbose and ag.grade.status == GRADE_STATUS_GRADED:
            _print_mismatches(ag.grade)
    _print_summary(grades)
    if args.calibrate:
        print()
        _print_calibration(grades)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

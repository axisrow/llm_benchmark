"""backfill считает успех так же, как рейтинг (issue #142).

Найдено ревью Codex к PR #144: `cell_ok` определял недобор по `runs.code=0`,
то есть по СТАРОМУ определению успеха. После #142 рейтинг требует ещё и файл
модели (agent_file), поэтому backfill считал ячейку укомплектованной ровно там,
где дашборд показывает недобор, — штатный путь восстановления не чинил бы именно
те провалы, которые вскрывает #142.

Исключение то же, что в index_builder._expects_agent_file: у `--questions-only`
прогона фазы build нет, файла никто не ждёт — такая копия остаётся успехом.
"""

import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for path in (str(_ROOT), str(_ROOT / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

import backfill_runs  # noqa: E402
import db  # noqa: E402
from conftest import fake_artifacts, report_for_db, temp_db  # noqa: E402


def _report(runs, *, questions_only=False, started_at="2026-01-01T00:00:00"):
    report = {
        "project": "p", "provider": "prov", "model": "m",
        "started_at": started_at,
        "summary": {"ok": sum(1 for r in runs if r["code"] == 0),
                    "timeout": 0, "error": 0},
        "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
        "runs": runs,
    }
    if questions_only:
        report["planning"] = {"enabled": True, "agent": "plan",
                              "responder": "task-text", "questions_only": True}
    return report


def _seed(conn, report, rel_path="data/result/r.json"):
    stored = report_for_db(report)
    with conn:
        db.upsert_report(conn, stored, rel_path, json.dumps(stored),
                         artifacts=fake_artifacts(report))


class BackfillSuccessDefinitionTests(unittest.TestCase):
    def test_cell_ok_skips_code0_runs_without_agent_file(self):
        # Три копии code=0, файл сохранила одна → укомплектовано на 1, не на 3.
        report = _report([
            {"index": 1, "code": 0, "elapsed": 1.0},
            {"index": 2, "code": 0, "elapsed": 2.0, "artifacts": ["run.log"]},
            {"index": 3, "code": 0, "elapsed": 3.0, "artifacts": ["run.log"]},
        ])
        with temp_db() as (conn, _root, _db_path):
            _seed(conn, report)

            self.assertEqual(backfill_runs.cell_ok(conn, "prov", "m", "p"), 1)

    def test_cell_ok_counts_questions_only_runs_without_artifact(self):
        # questions-only: фазы build нет, файла не ждём — копия успешна.
        report = _report(
            [{"index": 1, "code": 0, "elapsed": 1.0, "artifacts": ["run.log"]}],
            questions_only=True,
        )
        with temp_db() as (conn, _root, _db_path):
            _seed(conn, report)

            self.assertEqual(backfill_runs.cell_ok(conn, "prov", "m", "p"), 1)

    def test_cell_ok_ignores_failed_runs_even_with_artifact(self):
        # Файл есть, но копия упала — это не успех ни по какому определению.
        report = _report([
            {"index": 1, "code": 2, "elapsed": 1.0},
            {"index": 2, "code": 0, "elapsed": 2.0},
        ])
        with temp_db() as (conn, _root, _db_path):
            _seed(conn, report)

            self.assertEqual(backfill_runs.cell_ok(conn, "prov", "m", "p"), 1)

    def test_build_matrix_reports_need_for_artifactless_cell(self):
        # Ячейка «5 из 5 по code=0», но файл лишь у одной → need=4, а не 0:
        # штатный backfill обязан видеть недобор, который показывает рейтинг.
        runs = [{"index": 1, "code": 0, "elapsed": 1.0}] + [
            {"index": i, "code": 0, "elapsed": float(i), "artifacts": ["run.log"]}
            for i in range(2, 6)
        ]
        with temp_db() as (conn, _root, _db_path):
            _seed(conn, _report(runs))

            matrix = backfill_runs.build_matrix(conn, projects=("p",), target=5)

        cell = next(c for c in matrix if c["project"] == "p")
        self.assertEqual(cell["cell_ok"], 1)
        self.assertEqual(cell["need"], 4)


if __name__ == "__main__":
    unittest.main()

"""regenerate_raw_json сохраняет счётчик no_artifact (issue #142).

Найдено ревью Claude к PR #144 (minor): summary пересобирается по ключам
RUN_CODES, а no_artifact живёт вне таксономии (это не код исхода) — при
регенерации он молча пропадал. Просто скопировать старое значение нельзя:
скрипт умеет выбрасывать копии, поэтому счётчик надо пересчитать по выжившим.

Ключ, как и остальные сводки, попадает в результат ТОЛЬКО если был в исходном
отчёте: отчёты старого формата не должны обрастать новыми полями (байт-в-байт).
"""

import json
import sys
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
for path in (str(_ROOT), str(_ROOT / "scripts")):
    if path not in sys.path:
        sys.path.insert(0, path)

import db  # noqa: E402
import regenerate_raw_json  # noqa: E402
from conftest import fake_artifacts, report_for_db, temp_db  # noqa: E402


def _report(runs, summary):
    return {
        "project": "p", "provider": "prov", "model": "m",
        "started_at": "2026-01-01T00:00:00", "copies": len(runs),
        "summary": summary,
        "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
        "usage_summary": {}, "artifact_summary": {},
        "runs": runs,
    }


class RegenerateNoArtifactTests(unittest.TestCase):
    def _seed(self, conn, report):
        stored = report_for_db(report)
        with conn:
            return db.upsert_report(conn, stored, "data/result/r.json",
                                    json.dumps(stored),
                                    artifacts=fake_artifacts(report))

    def test_no_artifact_recounted_over_surviving_runs(self):
        # 3 копии code=0: у первой файл есть, у двух других — только лог.
        # Выбрасываем копию 3 → в выживших ровно одна копия без файла.
        report = _report(
            [
                {"index": 1, "code": 0, "elapsed": 1.0},
                {"index": 2, "code": 0, "elapsed": 2.0, "artifacts": ["run.log"]},
                {"index": 3, "code": 0, "elapsed": 3.0, "artifacts": ["run.log"]},
            ],
            {"ok": 3, "timeout": 0, "error": 0, "no_artifact": 2},
        )
        with temp_db() as (conn, _root, _db_path):
            report_id = self._seed(conn, report)
            stored = report_for_db(report)

            rebuilt = regenerate_raw_json.rebuild_report_dict(
                conn, report_id, stored, {1, 2})

        self.assertEqual(rebuilt["summary"]["no_artifact"], 1)
        self.assertEqual(rebuilt["summary"]["ok"], 2)

    def test_no_artifact_key_absent_when_report_had_none(self):
        # Отчёт старого формата (до #142) не должен обрасти новым ключом.
        report = _report(
            [{"index": 1, "code": 0, "elapsed": 1.0, "artifacts": ["run.log"]}],
            {"ok": 1, "timeout": 0, "error": 0},
        )
        with temp_db() as (conn, _root, _db_path):
            report_id = self._seed(conn, report)
            stored = report_for_db(report)

            rebuilt = regenerate_raw_json.rebuild_report_dict(
                conn, report_id, stored, {1})

        self.assertNotIn("no_artifact", rebuilt["summary"])

    def test_no_artifact_ignores_failed_runs(self):
        # Упавшая копия без файла — это error, а не no_artifact.
        report = _report(
            [
                {"index": 1, "code": 0, "elapsed": 1.0},
                {"index": 2, "code": 2, "elapsed": 2.0, "artifacts": ["run.log"]},
            ],
            {"ok": 1, "timeout": 0, "error": 1, "no_artifact": 0},
        )
        with temp_db() as (conn, _root, _db_path):
            report_id = self._seed(conn, report)
            stored = report_for_db(report)

            rebuilt = regenerate_raw_json.rebuild_report_dict(
                conn, report_id, stored, {1, 2})

        self.assertEqual(rebuilt["summary"]["no_artifact"], 0)
        self.assertEqual(rebuilt["summary"]["error"], 1)


if __name__ == "__main__":
    unittest.main()

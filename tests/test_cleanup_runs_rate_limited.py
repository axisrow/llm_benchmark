"""Регресс-тест issue #54 (находка #1): cleanup_runs.py должен оставлять отчёт
согласованным, когда среди выживших прогонов есть code=3 (rate_limited).

Баг: пересчёт после чистки правил только SQL-колонки summary_ok/timeout/error
(хардкод кодов 0/1/2) и copies, но НЕ raw_json. При выжившем code=3 (в reports
нет колонки summary_rate_limited — он живёт только в raw_json) raw_json оставался
со старым набором runs/summary/copies → дашборд (index_builder читает ТОЛЬКО
raw_json) показывал удалённые ошибки/таймауты, а строка отчёта рассыпалась.

Фикс: после удаления junk-прогонов cleanup_runs пересобирает raw_json/summary/
copies затронутых отчётов из выживших runs через ту же таксономию RUN_CODES, что
и regenerate_raw_json. После этого raw_json согласован с таблицей runs, а code=3
учтён в summary.

Сеть/opencode не нужны: временная sqlite + мок db.connect + sys.argv (как в
tests/test_fix_B11.py).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import scripts.cleanup_runs as cleanup
from opencode_runtime import RUN_CODES

# code -> ключ summary из единого источника таксономии (как в regenerate_raw_json).
_CODE_TO_KEY = {code: key for code, (key, _label) in RUN_CODES.items()}


def _report_dict(started_at: str, runs: list[tuple[int, int, float]]) -> dict:
    """Собирает report dict с runs[] (idx, code, elapsed) и согласованной сводкой."""
    summary = {key: 0 for key in _CODE_TO_KEY.values()}
    run_entries = []
    for idx, code, elapsed in runs:
        summary[_CODE_TO_KEY[code]] += 1
        run_entries.append(
            {"index": idx, "code": code, "elapsed": elapsed, "usage": None})
    return {
        "project": "p", "provider": "prov", "model": "m",
        "started_at": started_at, "run_elapsed": 10.0, "copies": len(runs),
        "summary": summary, "usage_summary": {}, "runs": run_entries,
    }


def _insert(conn, report: dict) -> int:
    """Вставляет отчёт (reports + runs) дословным raw_json через upsert_report."""
    raw = json.dumps(report, ensure_ascii=False, indent=2)
    return db.upsert_report(conn, report, "data/result/x.json", raw)


class CleanupRunsRateLimitedTests(unittest.TestCase):
    def _run_main(self, db_path: Path, *extra_argv: str) -> int:
        orig_connect = cleanup.db.connect
        with mock.patch.object(cleanup.db, "connect",
                               lambda *a, **k: orig_connect(db_path)):
            with mock.patch.object(sys, "argv", ["cleanup_runs.py", *extra_argv]):
                return cleanup.main()

    def test_surviving_rate_limited_run_keeps_report_consistent(self):
        """Отчёт: ok + error(удалится) + false-timeout(удалится) + real-timeout +
        rate_limited. После чистки raw_json должен сойтись с таблицей runs, а
        code=3 — попасть в summary."""
        with tempfile.TemporaryDirectory() as t_dir:
            db_path = Path(t_dir) / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                with conn:
                    rid = _insert(conn, _report_dict(
                        "2026-01-01T00:00:00",
                        runs=[
                            (0, 0, 12.0),    # ok — остаётся
                            (1, 2, 5.0),     # error — удаляется
                            (2, 1, 124.0),   # false timeout (<130) — удаляется
                            (3, 1, 454.0),   # настоящий таймаут — остаётся
                            (4, 3, 8.0),     # rate_limited — остаётся
                        ],
                    ))
            finally:
                conn.close()

            rc = self._run_main(db_path)
            self.assertEqual(rc, 0)

            conn = db.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT copies, summary_ok, summary_timeout, summary_error, "
                    "raw_json FROM reports WHERE id=?", (rid,)).fetchone()
                runs_codes = sorted(
                    r[0] for r in conn.execute(
                        "SELECT code FROM runs WHERE report_id=?", (rid,)))
            finally:
                conn.close()

            # Таблица runs: остались ok(0), настоящий таймаут(1), rate_limited(3).
            self.assertEqual(runs_codes, [0, 1, 3])

            report = json.loads(row["raw_json"])
            # raw_json пересобран под выжившие прогоны (а не остался со старыми 5).
            self.assertEqual(len(report["runs"]), 3,
                             "raw_json.runs должен совпасть с таблицей runs")
            self.assertEqual(report["copies"], 3)
            # summary в raw_json: code=3 учтён, сумма сходится с copies.
            summary = report["summary"]
            self.assertEqual(summary["ok"], 1)
            self.assertEqual(summary["timeout"], 1)
            self.assertEqual(summary["error"], 0)
            self.assertEqual(summary["rate_limited"], 1)
            self.assertEqual(sum(summary.values()), report["copies"],
                             "сумма summary должна равняться copies (с rate_limited)")
            # SQL-колонки (legacy-срез без rate_limited) — из выживших.
            self.assertEqual(row["copies"], 3)
            self.assertEqual(
                (row["summary_ok"], row["summary_timeout"], row["summary_error"]),
                (1, 1, 0))


if __name__ == "__main__":
    unittest.main()

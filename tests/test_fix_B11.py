"""Регресс-тест бага B11: cleanup_runs.py Step 3 удаляет ВСЕ отчёты без runs,
а не только опустевшие в этом проходе.

Механизм бага: реальный DELETE использовал
    DELETE FROM reports WHERE id NOT IN (SELECT DISTINCT report_id FROM runs)
— то есть сносил КАЖДЫЙ отчёт, у которого сейчас нет строк в runs, включая
легитимные отчёты, вставленные без runs. dry-run preview же показывал только
отчёты, реально опустевшие из-за удаления junk-прогонов (AND EXISTS runs) —
over-deletion был скрыт.

Сеть/opencode тут не нужны: работаем с временной sqlite (см. стиль
tests/test_bench.py — mock db.connect + sys.argv).
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import scripts.cleanup_runs as cleanup
from conftest import capture_stdout


def _insert_report(conn, started_at: str, *, runs: list[tuple[int, int]]) -> int:
    """Вставляет отчёт и его runs. runs — список (idx, code). Возвращает id."""
    report_id = conn.execute(
        """
        INSERT INTO reports
            (project, provider, model, started_at, run_elapsed, copies,
             summary_ok, summary_timeout, summary_error, rel_path, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        ("p", "prov", "m", started_at, 1.0, len(runs), 0, 0, 0,
         "data/result/x.json", "{}"),
    ).fetchone()[0]
    for idx, code in runs:
        conn.execute(
            "INSERT INTO runs (report_id, idx, port, dir, status, code, elapsed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (report_id, idx, 4000 + idx, "/tmp/x", "st", code, 5.0),
        )
    return report_id


class CleanupRunsB11Tests(unittest.TestCase):
    def _run_main(self, db_path: Path, *extra_argv: str) -> int:
        orig_connect = cleanup.db.connect
        with mock.patch.object(cleanup.db, "connect",
                               lambda *a, **k: orig_connect(db_path)):
            with mock.patch.object(sys, "argv", ["cleanup_runs.py", *extra_argv]):
                return cleanup.main()

    def test_real_delete_keeps_legit_report_without_runs(self):
        """Step 3 должен сносить ТОЛЬКО опустевшие в этом проходе отчёты.

        Отчёт A: легитимный, без runs (вставлен без прогонов / опустошён ранее).
        Отчёт B: один junk-прогон (code=2) — опустеет в этом проходе.
        Ожидание: удалён только B, A сохранён. На баговом коде сносятся оба.
        """
        with tempfile.TemporaryDirectory() as t_dir:
            db_path = Path(t_dir) / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                with conn:
                    rid_a = _insert_report(conn, "2026-01-01T00:00:00", runs=[])
                    rid_b = _insert_report(conn, "2026-01-02T00:00:00",
                                           runs=[(0, 2)])
            finally:
                conn.close()

            rc = self._run_main(db_path)
            self.assertEqual(rc, 0)

            conn = db.connect(db_path)
            try:
                remaining = {
                    row[0] for row in conn.execute("SELECT id FROM reports")
                }
            finally:
                conn.close()

            self.assertIn(rid_a, remaining,
                          "легитимный отчёт без runs не должен удаляться")
            self.assertNotIn(rid_b, remaining,
                             "опустевший в этом проходе отчёт должен удалиться")

    def test_dry_run_preview_matches_real_delete_set(self):
        """dry-run preview и реальный DELETE должны удалять один и тот же набор.

        Готовим: A — легитимный без runs (preview его НЕ показывает),
        B — junk-прогон (опустеет, preview его показывает),
        C — junk + нормальный прогон (НЕ опустеет, остаётся).
        Сначала снимаем preview (id в выводе), затем реальный прогон;
        набор реально удалённых отчётов обязан совпасть с preview.
        """
        with tempfile.TemporaryDirectory() as t_dir:
            db_path = Path(t_dir) / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                with conn:
                    rid_a = _insert_report(conn, "2026-01-01T00:00:00", runs=[])
                    rid_b = _insert_report(conn, "2026-01-02T00:00:00",
                                           runs=[(0, 2)])
                    rid_c = _insert_report(conn, "2026-01-03T00:00:00",
                                           runs=[(0, 2), (1, 0)])
            finally:
                conn.close()

            # dry-run: собираем id отчётов, которые preview обещает удалить.
            out = self._capture_stdout(lambda: self._run_main(db_path, "--dry-run"))
            preview_ids = {
                rid for rid in (rid_a, rid_b, rid_c)
                if f"id={rid} " in out
            }

            # dry-run ничего не меняет — все отчёты на месте.
            conn = db.connect(db_path)
            try:
                still_all = {
                    row[0] for row in conn.execute("SELECT id FROM reports")
                }
            finally:
                conn.close()
            self.assertEqual(still_all, {rid_a, rid_b, rid_c})

            # реальный прогон.
            self._run_main(db_path)
            conn = db.connect(db_path)
            try:
                remaining = {
                    row[0] for row in conn.execute("SELECT id FROM reports")
                }
            finally:
                conn.close()
            really_deleted = {rid_a, rid_b, rid_c} - remaining

            self.assertEqual(
                really_deleted, preview_ids,
                "реальный DELETE и dry-run preview должны совпадать")
            self.assertEqual(really_deleted, {rid_b},
                             "удалиться должен только опустевший в этом проходе B")

    @staticmethod
    def _capture_stdout(fn) -> str:
        return capture_stdout(fn)  # тело в conftest (issue #54 #9)


if __name__ == "__main__":
    unittest.main()

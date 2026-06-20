"""Тест issue #54 (находка #4): общие хелперы статуса модели (denylist + unstable).

Раньше db.py содержал две почти идентичные семьи из 5 функций (различие — имя
таблицы). Сведены к приватным хелперам с параметром table + тонкие публичные
обёртки. Проверяем: обёртки обеих семей работают через общий путь и не путают
таблицы; allowlist отвергает неизвестное (в т.ч. инъекционное) имя таблицы.
"""

import tempfile
import unittest
from pathlib import Path

import db


class ModelStatusHelpersTests(unittest.TestCase):
    def _conn(self, td: str):
        conn = db.connect(Path(td) / "main.db")
        db.init_schema(conn)
        return conn

    def test_both_families_share_helper_behaviour(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._conn(td)
            try:
                with conn:
                    db.block_model_exclusion(conn, "p", "m", "spam")
                    db.mark_model_unstable(conn, "p", "m2", "flaky")
                # denylist-семья
                self.assertEqual(
                    db.get_model_exclusion(conn, "p", "m")["reason"], "spam")
                self.assertEqual(
                    db.active_exclusions_map(conn), {("p", "m"): "spam"})
                # таблицы не путаются: denylist-модель не видна как unstable
                self.assertIsNone(db.get_model_unstable(conn, "p", "m"))
                # unstable-семья
                self.assertEqual(
                    db.get_model_unstable(conn, "p", "m2")["reason"], "flaky")
                self.assertEqual(
                    db.active_unstable_map(conn), {("p", "m2"): "flaky"})
                self.assertIsNone(db.get_model_exclusion(conn, "p", "m2"))
                # снятие статуса в обеих семьях
                with conn:
                    db.unblock_model_exclusion(conn, "p", "m")
                    db.unmark_model_unstable(conn, "p", "m2")
                self.assertEqual(db.active_exclusions_map(conn), {})
                self.assertEqual(db.active_unstable_map(conn), {})
            finally:
                conn.close()

    def test_unknown_table_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._conn(td)
            try:
                with self.assertRaises(ValueError):
                    db._get_model_status(conn, "reports", "p", "m")
                with self.assertRaises(ValueError):
                    db._set_model_status(conn, "runs; DROP TABLE reports", "p", "m")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

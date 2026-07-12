"""Тест issue #54 (находка #4): общие хелперы статуса модели (denylist + unstable).

Раньше db.py содержал две почти идентичные семьи из 5 функций (различие — имя
таблицы). Сведены к приватным хелперам с параметром table + тонкие публичные
обёртки. Проверяем: обёртки обеих семей работают через общий путь и не путают
таблицы; allowlist отвергает неизвестное (в т.ч. инъекционное) имя таблицы.
"""

import unittest

import db
from conftest import temp_db


class ModelStatusHelpersTests(unittest.TestCase):
    def test_both_families_share_helper_behaviour(self):
        with temp_db() as (conn, _root, _db_path):
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

    def test_unknown_table_is_rejected(self):
        bad = "runs; DROP TABLE reports"
        with temp_db() as (conn, _root, _db_path):
            # Все 5 хелперов идут через _check_status_table — проверяем каждый.
            with self.assertRaises(ValueError):
                db._get_model_status(conn, "reports", "p", "m")
            with self.assertRaises(ValueError):
                db._list_model_status(conn, bad)
            with self.assertRaises(ValueError):
                db._active_status_map(conn, bad)
            with self.assertRaises(ValueError):
                db._set_model_status(conn, bad, "p", "m")
            with self.assertRaises(ValueError):
                db._clear_model_status(conn, bad, "p", "m")


if __name__ == "__main__":
    unittest.main()

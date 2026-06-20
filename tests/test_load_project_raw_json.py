"""Тест issue #54 (находка #11): load_project парсит raw_json через
db.safe_json_loads (а не ручной except), сохраняя инвариант: «не распарсилось» —
это ошибка БД (PROJECT_LOAD_ERROR), а валидный non-dict — «проекта нет» (ad-hoc).
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path

import benchmark_report
import db


class LoadProjectRawJsonTests(unittest.TestCase):
    @contextlib.contextmanager
    def _project(self, raw_json: str):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                with conn:
                    conn.execute(
                        "INSERT INTO projects_library (name, raw_json) VALUES (?, ?)",
                        ("proj", raw_json))
            finally:
                conn.close()
            orig = benchmark_report.connect
            benchmark_report.connect = lambda *a, **k: db.connect(db_path)
            try:
                yield
            finally:
                benchmark_report.connect = orig

    def test_corrupted_raw_json_returns_load_error(self):
        with self._project("{not json"):
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = benchmark_report.load_project("proj")
        self.assertIs(result, benchmark_report.PROJECT_LOAD_ERROR)
        self.assertIn("повреждён raw_json", stderr.getvalue())

    def test_valid_non_dict_returns_none(self):
        # Валидный JSON, но не объект → «проекта нет» (ad-hoc), а НЕ ошибка БД.
        with self._project("[1, 2, 3]"):
            self.assertIsNone(benchmark_report.load_project("proj"))

    def test_valid_dict_returned(self):
        with self._project('{"prompt": "hi", "x": 1}'):
            self.assertEqual(benchmark_report.load_project("proj"),
                             {"prompt": "hi", "x": 1})


if __name__ == "__main__":
    unittest.main()

"""Регресс-тест cleanup_runs.py --no-html (#126): предикат не должен трогать
неуспешные копии (code≠0) — у timeout/error копии HTML естественно нет, и чистка
«без HTML» стёрла бы сам факт провала модели.

Удаляются только УСПЕШНЫЕ (code==0) копии без HTML — лог-мусор при успехе.
Сетевой/opencode-части нет — работаем с временной sqlite (как tests/test_fix_B11.py).
"""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import scripts.cleanup_runs as cleanup


def _insert_report(conn, started_at: str, project: str, *, runs: list[tuple]) -> int:
    """Вставляет отчёт и его runs. runs — список (idx, code, has_html).
    has_html=True добавляет agent_file .html артефакт (с dummy-блобом)."""
    import hashlib

    report_id = conn.execute(
        """
        INSERT INTO reports
            (project, provider, model, started_at, run_elapsed, copies,
             summary_ok, summary_timeout, summary_error, rel_path, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        RETURNING id
        """,
        (project, "prov", "m", started_at, 1.0, len(runs), 0, 0, 0,
         "data/result/x.json", "{}"),
    ).fetchone()[0]
    for idx, code, has_html in runs:
        conn.execute(
            "INSERT INTO runs (report_id, idx, port, dir, status, code, elapsed) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (report_id, idx, 4000 + idx, "/tmp/x", "st", code, 5.0),
        )
        log_sha = hashlib.sha256(f"log{report_id}-{idx}".encode()).hexdigest()
        conn.execute(
            "INSERT OR IGNORE INTO file_blobs "
            "(sha256, size_bytes, content_encoding, content_blob) VALUES (?,?,?,?)",
            (log_sha, 3, "raw", b"x"),
        )
        conn.execute(
            "INSERT INTO run_artifacts (report_id, run_idx, path, kind, sha256) "
            "VALUES (?, ?, ?, ?, ?)",
            (report_id, idx, "run.log", "log", log_sha),
        )
        if has_html:
            sha = hashlib.sha256(f"html{report_id}-{idx}".encode()).hexdigest()
            conn.execute(
                "INSERT OR IGNORE INTO file_blobs "
                "(sha256, size_bytes, content_encoding, content_blob) VALUES (?,?,?,?)",
                (sha, 3, "raw", b"x"),
            )
            conn.execute(
                "INSERT INTO run_artifacts (report_id, run_idx, path, kind, sha256) "
                "VALUES (?, ?, ?, ?, ?)",
                (report_id, idx, "calc.html", "agent_file", sha),
            )
    return report_id


class CleanupNoHtmlTests(unittest.TestCase):
    def _run_main(self, db_path: Path, *extra_argv: str) -> int:
        orig_connect = cleanup.db.connect
        with mock.patch.object(cleanup.db, "connect",
                               lambda *a, **k: orig_connect(db_path)):
            with mock.patch.object(sys, "argv", ["cleanup_runs.py", *extra_argv]):
                return cleanup.main()

    def test_no_html_keeps_failed_copies_and_drops_successful_without_html(self):
        """--no-html удаляет ТОЛЬКО code==0 без HTML. Неуспешные копии (code
        1/2/3) без HTML выживают — это записи о провалах модели, не мусор.

        Отчёт library_fine с копиями:
          run 1 code=0 БЕЗ html  → удалить (лог-мусор при успехе)
          run 2 code=1 БЕЗ html  → ОСТАВИТЬ (timeout, нет html нормально)
          run 3 code=2 БЕЗ html  → ОСТАВИТЬ (error)
          run 4 code=0 С html    → ОСТАВИТЬ (успешная с реализацией)
        """
        with tempfile.TemporaryDirectory() as t_dir:
            db_path = Path(t_dir) / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                with conn:
                    _insert_report(conn, "2026-01-01T00:00:00", "library_fine",
                                   runs=[(1, 0, False), (2, 1, False),
                                         (3, 2, False), (4, 0, True)])
            finally:
                conn.close()

            self._run_main(db_path, "--no-html")

            conn = db.connect(db_path)
            try:
                survivors = {
                    (row[0], row[1]): row[2]
                    for row in conn.execute(
                        "SELECT report_id, idx, code FROM runs ORDER BY idx")
                }
            finally:
                conn.close()

        # выжили копии 2 (timeout), 3 (error), 4 (ok+html); удалена только 1.
        self.assertEqual(
            {idx for _, idx in survivors}, {2, 3, 4},
            "неуспешные копии (code 1/2) без HTML и успешная с HTML выживают; "
            "удаляется только успешная без HTML")

    def test_no_html_predicate_carries_code_zero(self):
        """Контракт предиката: ru.code = 0 присутствует — неуспешные копии
        в принципе не могут стать кандидатами. Строковая проверка (быстрая,
        без БД) ловит регрессию, если условие случайно уберут."""
        self.assertIn("ru.code = 0", cleanup.NO_HTML_RUN)


if __name__ == "__main__":
    unittest.main()

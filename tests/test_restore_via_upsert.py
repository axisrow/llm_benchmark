"""Тест issue #54 (находка #3): restore_reports_from_git пишет отчёт через единый
путь db.upsert_report, а не приватную копию списка колонок reports (REPORT_COLS).

Раньше скрипт дублировал 11 колонок reports; при добавлении колонки в схему он
молча терял бы её значение. Теперь вставка идёт через upsert_report. Проверяем
сквозной перенос: raw_json дословно, summary_*/copies выведены из него, runs и
run_artifacts/file_blobs перенесены.
"""

import contextlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import scripts.restore_reports_from_git as restore


def _report(project: str, started_at: str) -> dict:
    return {
        "project": project, "provider": "v", "model": "m",
        "started_at": started_at, "run_elapsed": 2.0, "copies": 2,
        "summary": {"ok": 1, "timeout": 0, "error": 0, "rate_limited": 1},
        "usage_summary": {}, "artifact_summary": {},
        "runs": [
            {"index": 0, "code": 0, "elapsed": 5.0, "usage": None},
            {"index": 1, "code": 3, "elapsed": 8.0, "usage": None},
        ],
    }


class RestoreViaUpsertTests(unittest.TestCase):
    def test_restore_routes_through_upsert_and_preserves_data(self):
        with tempfile.TemporaryDirectory() as td:
            source_path = Path(td) / "source.db"
            target_path = Path(td) / "target.db"
            keys_path = Path(td) / "keys.txt"

            report = _report("pA", "2026-03-01T00:00:00")
            raw = json.dumps(report, ensure_ascii=False, indent=2)

            conn = db.connect(source_path)
            try:
                db.init_schema(conn)
                with conn:
                    rid = db.upsert_report(conn, report, "data/result/pA.json", raw)
                    conn.execute(
                        "INSERT INTO file_blobs (sha256, size_bytes, "
                        "content_encoding, content_blob) VALUES (?,?,?,?)",
                        ("sha1", 3, "identity", b"abc"))
                    conn.execute(
                        "INSERT INTO run_artifacts (report_id, run_idx, path, kind, "
                        "sha256) VALUES (?,?,?,?,?)",
                        (rid, 0, "out.txt", "agent_file", "sha1"))
            finally:
                conn.close()

            keys_path.write_text("pA|v|m|2026-03-01T00:00:00\n", encoding="utf-8")

            orig_connect = restore.db.connect
            with mock.patch.object(restore.db, "connect",
                                   lambda: orig_connect(target_path)), \
                    mock.patch.object(sys, "argv",
                                      ["restore_reports_from_git.py",
                                       "--source", str(source_path),
                                       "--keys", str(keys_path)]):
                rc = restore.main()
            self.assertEqual(rc, 0)

            conn = db.connect(target_path)
            try:
                row = conn.execute(
                    "SELECT raw_json, project, provider, model, started_at, copies, "
                    "summary_ok, summary_timeout, summary_error FROM reports"
                ).fetchone()
                runs = sorted(r[0] for r in conn.execute("SELECT code FROM runs"))
                arts = [tuple(r) for r in conn.execute(
                    "SELECT path, kind, sha256 FROM run_artifacts")]
                blob = conn.execute(
                    "SELECT content_blob FROM file_blobs WHERE sha256='sha1'"
                ).fetchone()
            finally:
                conn.close()

            # raw_json перенесён дословно (byte-for-byte инвариант).
            self.assertEqual(row["raw_json"], raw)
            # Идентичность и summary_*/copies выведены из raw_json (upsert_report).
            self.assertEqual(
                (row["project"], row["provider"], row["model"], row["started_at"]),
                ("pA", "v", "m", "2026-03-01T00:00:00"))
            self.assertEqual(row["copies"], 2)
            self.assertEqual(
                (row["summary_ok"], row["summary_timeout"], row["summary_error"]),
                (1, 0, 0))
            # runs из report["runs"], включая code=3.
            self.assertEqual(runs, [0, 3])
            # Артефакты и блоб перенесены отдельным копированием из источника.
            self.assertEqual(arts, [("out.txt", "agent_file", "sha1")])
            self.assertEqual(blob["content_blob"], b"abc")

    def test_mismatched_raw_json_identity_is_rejected(self):
        """Источник найден по SQL-ключу A, но raw_json внутри — идентичность B.

        Без проверки upsert_report записал бы под ключом B и через ON CONFLICT
        затёр бы легитимный отчёт B в target (codex P2). Ожидаем: ключ падает
        (rc=1, «ОШИБКА»), отчёт B в target НЕ тронут.
        """
        with tempfile.TemporaryDirectory() as td:
            source_path = Path(td) / "source.db"
            target_path = Path(td) / "target.db"
            keys_path = Path(td) / "keys.txt"

            # src: SQL-ключ строки = A, но raw_json несёт идентичность B.
            raw_b = json.dumps(_report("pB", "2026-04-02T00:00:00"),
                               ensure_ascii=False, indent=2)
            src = sqlite3.connect(source_path)
            try:
                db.init_schema(src)
                src.execute(
                    "INSERT INTO reports (project, provider, model, started_at, "
                    "run_elapsed, copies, summary_ok, summary_timeout, "
                    "summary_error, rel_path, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    ("pA", "v", "m", "2026-04-01T00:00:00", 1.0, 2, 1, 0, 0, "x",
                     raw_b))
                src.commit()
            finally:
                src.close()

            # target: уже есть легитимный B (отличаем по copies=99).
            report_b_target = _report("pB", "2026-04-02T00:00:00")
            report_b_target["copies"] = 99
            tconn = db.connect(target_path)
            try:
                db.init_schema(tconn)
                with tconn:
                    db.upsert_report(
                        tconn, report_b_target, "data/result/pB.json",
                        json.dumps(report_b_target, ensure_ascii=False, indent=2))
            finally:
                tconn.close()

            keys_path.write_text("pA|v|m|2026-04-01T00:00:00\n", encoding="utf-8")

            orig_connect = restore.db.connect
            err = io.StringIO()
            with mock.patch.object(restore.db, "connect",
                                   lambda: orig_connect(target_path)), \
                    mock.patch.object(sys, "argv",
                                      ["restore_reports_from_git.py",
                                       "--source", str(source_path),
                                       "--keys", str(keys_path)]), \
                    contextlib.redirect_stderr(err):
                rc = restore.main()

            self.assertEqual(rc, 1)
            self.assertIn("ОШИБКА", err.getvalue())
            conn = db.connect(target_path)
            try:
                copies = conn.execute(
                    "SELECT copies FROM reports WHERE project='pB'").fetchone()[0]
            finally:
                conn.close()
            self.assertEqual(copies, 99, "отчёт B в target не должен быть затёрт")


if __name__ == "__main__":
    unittest.main()

"""Регресс-тест B3: перезапись артефакта новым содержимым не должна плодить
осиротевшие file_blobs.

Механизм бага: replace_report_artifacts (и full-, и partial-путь) удаляет старые
маппинги run_artifacts и вставляет новые, но НЕ зовёт prune_orphan_blobs. При
upsert_report тем же ключом (project, provider, model, started_at) с ИЗМЕНЁННЫМ
содержимым артефакта старый blob остаётся в file_blobs без единой ссылки навсегда
(подметает только delete_report). data/main.db коммитится в git -> мёртвые блобы.

Сеть/opencode тут не задействованы, БД — временная sqlite (НЕ data/main.db).
"""

import hashlib
import unittest
from pathlib import Path

import artifacts
import db
from conftest import temp_db


def _make_artifact(content: bytes, *, path: str = "out.txt") -> artifacts.RunArtifact:
    """Собирает RunArtifact для run_idx=0 c sha256 от содержимого."""
    return artifacts.RunArtifact(
        run_idx=0,
        path=path,
        kind="agent_file",
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
        source_path=Path("/x"),
    )


_BASE_REPORT = {
    "project": "p", "model": "m", "provider": "v", "prompt": "t",
    "description": None, "what_it_tests": None, "copies": 1,
    "started_at": "2026-01-01T00:00:00", "run_elapsed": 1.0,
    "summary": {"ok": 1, "timeout": 0, "error": 0}, "pricing": {},
    "usage_summary": {}, "artifact_summary": {},
    "runs": [{"index": 0, "port": 4000, "dir": "/x", "status": "готово",
              "code": 0, "elapsed": 10.0, "usage": None}],
}


class ReplaceArtifactsPrunesOrphanBlobsTests(unittest.TestCase):
    def _count_blobs(self, conn) -> int:
        return conn.execute("SELECT count(*) FROM file_blobs").fetchone()[0]

    def _count_orphan_blobs(self, conn) -> int:
        return conn.execute(
            "SELECT count(*) FROM file_blobs "
            "WHERE sha256 NOT IN (SELECT sha256 FROM run_artifacts)"
        ).fetchone()[0]

    def test_full_upsert_with_changed_content_prunes_old_blob(self):
        # Перезапись отчёта тем же ключом, но новым содержимым артефакта:
        # старый blob должен быть подметён, остаётся ровно один (новый).
        v1 = _make_artifact(b"version-1")
        v2 = _make_artifact(b"version-2")
        self.assertNotEqual(v1.sha256, v2.sha256)

        with temp_db() as (conn, _root, _db_path):
            with conn:
                db.upsert_report(
                    conn, dict(_BASE_REPORT),
                    "data/result/r.json", "{}", artifacts=[v1])
            self.assertEqual(self._count_blobs(conn), 1)

            # тот же ключ (started_at не меняется) -> full replace
            with conn:
                db.upsert_report(
                    conn, dict(_BASE_REPORT),
                    "data/result/r.json", "{}", artifacts=[v2])

            self.assertEqual(self._count_blobs(conn), 1)
            self.assertEqual(self._count_orphan_blobs(conn), 0)
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM file_blobs WHERE sha256=?",
                    (v2.sha256,)).fetchone()[0], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM file_blobs WHERE sha256=?",
                    (v1.sha256,)).fetchone()[0], 0)

    def test_partial_replace_with_changed_content_prunes_old_blob(self):
        # partial-путь (backfill): точечная замена run_idx=0 новым содержимым
        # тоже должна подметать осиротевший старый blob.
        v1 = _make_artifact(b"partial-1")
        v2 = _make_artifact(b"partial-2")

        with temp_db() as (conn, _root, _db_path):
            with conn:
                report_id = db.upsert_report(
                    conn, dict(_BASE_REPORT),
                    "data/result/r.json", "{}", artifacts=[v1])
            self.assertEqual(self._count_blobs(conn), 1)

            with conn:
                db.replace_report_artifacts(
                    conn, report_id, [v2], partial=True)

            self.assertEqual(self._count_blobs(conn), 1)
            self.assertEqual(self._count_orphan_blobs(conn), 0)
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM file_blobs WHERE sha256=?",
                    (v1.sha256,)).fetchone()[0], 0)

    def test_shared_blob_survives_when_still_referenced(self):
        # Инвариант: prune не должен трогать blob, на который ещё есть ссылка
        # из другого отчёта (защита от чрезмерного удаления).
        shared = _make_artifact(b"shared", path="shared.txt")
        v1 = _make_artifact(b"keep-version-1", path="out.txt")
        v2 = _make_artifact(b"keep-version-2", path="out.txt")

        other_report = dict(_BASE_REPORT, started_at="2026-02-02T00:00:00")

        with temp_db() as (conn, _root, _db_path):
            with conn:
                db.upsert_report(
                    conn, dict(_BASE_REPORT),
                    "data/result/r1.json", "{}", artifacts=[shared, v1])
                db.upsert_report(
                    conn, other_report,
                    "data/result/r2.json", "{}", artifacts=[shared])
            self.assertEqual(self._count_blobs(conn), 2)  # shared + v1

            # перезапись первого отчёта новым содержимым out.txt
            with conn:
                db.upsert_report(
                    conn, dict(_BASE_REPORT),
                    "data/result/r1.json", "{}", artifacts=[shared, v2])

            # v1 подметён, shared уцелел (ссылка из второго отчёта), v2 жив
            self.assertEqual(self._count_orphan_blobs(conn), 0)
            self.assertEqual(self._count_blobs(conn), 2)  # shared + v2
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM file_blobs WHERE sha256=?",
                    (shared.sha256,)).fetchone()[0], 1)
            self.assertEqual(
                conn.execute(
                    "SELECT count(*) FROM file_blobs WHERE sha256=?",
                    (v1.sha256,)).fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()

"""Покрытие функций, не затронутых тестами (аудит issue #38).

Закрывает юнит-дыры P0/P1: opencode_runtime-хелперы, write_availability_json,
get_pricing, iter_artifact_contents (в т.ч. битый zlib-blob), dashboard_server
(_db_fingerprint / cleanup_index_snapshot) и CLI smoke (P3). HTTP-обработчик
дашборда (200/404/rebuild) и фронтенд покрыты отдельно в test_e2e_dashboard.py
(Playwright). Тесты-зависимости от сети/opencode не используются.
"""

import contextlib
import json
import subprocess
import sys
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import check_models
import dashboard_server
import db
import opencode_runtime as runtime
import pricing

PROJECT_ROOT = Path(__file__).resolve().parent.parent


class OpencodeRuntimeHelperTests(unittest.TestCase):
    """opencode_runtime: base_url, client_for_port, work_root_for, _extract_session_id."""

    def test_base_url(self):
        self.assertEqual(runtime.base_url(4096), "http://127.0.0.1:4096")

    def test_client_for_port_passes_base_url(self):
        with mock.patch.object(runtime, "Opencode") as fake:
            runtime.client_for_port(4242)
        fake.assert_called_once_with(base_url="http://127.0.0.1:4242")

    def test_work_root_for_sanitizes_segments(self):
        root = runtime.work_root_for("my proj", "zai/coding", "glm 5.1")
        # provider/model склеены через "_", спорные символы санированы.
        self.assertEqual(root.parent.name, "my-proj")
        self.assertEqual(root.name, "zai-coding_glm-5.1")
        self.assertTrue(root.is_relative_to(runtime.WORK_ROOT))

    def test_extract_session_id_from_info_session_id(self):
        payload = {"properties": {"info": {"sessionID": "ses_abc"}}}
        self.assertEqual(runtime._extract_session_id(payload), "ses_abc")

    def test_extract_session_id_from_info_id_with_prefix(self):
        payload = {"properties": {"info": {"id": "ses_xyz"}}}
        self.assertEqual(runtime._extract_session_id(payload), "ses_xyz")

    def test_extract_session_id_ignores_non_ses_info_id(self):
        # info.id без префикса ses_ не считается id сессии.
        payload = {"properties": {"info": {"id": "msg_123"}}}
        self.assertIsNone(runtime._extract_session_id(payload))

    def test_extract_session_id_from_top_level_session_id(self):
        payload = {"sessionID": "ses_top"}
        self.assertEqual(runtime._extract_session_id(payload), "ses_top")

    def test_extract_session_id_missing_returns_none(self):
        self.assertIsNone(runtime._extract_session_id({"type": "noise"}))


class WriteAvailabilityJsonTests(unittest.TestCase):
    """check_models.write_availability_json — сериализация отчёта доступности."""

    def _result(self, provider, model, code, status):
        ref = check_models.ModelRef(provider=provider, model=model)
        return check_models.CheckResult(
            ref=ref, code=code, status=status, reason=None, elapsed=0.5,
            attempt_timeout=10.0, retried=False, log_path=f"{model}.log")

    def test_writes_summary_meta_and_results(self):
        results = [
            self._result("p", "ok-model", 0, "available"),
            self._result("p", "bad-model", 2, "error"),
        ]
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "availability.json"
            check_models.write_availability_json(
                results, path, {"generated_at": "2026-06-16T00:00:00"})
            report = json.loads(path.read_text(encoding="utf-8"))

        self.assertEqual(report["generated_at"], "2026-06-16T00:00:00")
        self.assertEqual(report["summary"]["total"], 2)
        self.assertEqual(report["summary"]["available"], 1)
        self.assertEqual(report["summary"]["error"], 1)
        self.assertEqual({r["model"] for r in report["results"]},
                         {"ok-model", "bad-model"})
        self.assertEqual(report["results"][0]["log"], "ok-model.log")


class GetPricingTests(unittest.TestCase):
    """pricing.get_pricing — приоритеты overrides → каталог → note → empty."""

    def setUp(self):
        # get_pricing опирается на мемоизированные читатели базы — чистим кэш.
        pricing._load_local_prices.cache_clear()
        pricing._read_cached_models.cache_clear()

    def tearDown(self):
        pricing._load_local_prices.cache_clear()
        pricing._read_cached_models.cache_clear()

    @contextlib.contextmanager
    def _db(self, seed):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                with conn:
                    seed(conn)
            finally:
                conn.close()
            original = pricing.connect
            try:
                pricing.connect = lambda: db.connect(db_path)
                pricing._load_local_prices.cache_clear()
                pricing._read_cached_models.cache_clear()
                yield
            finally:
                pricing.connect = original

    def test_override_takes_precedence(self):
        def seed(conn):
            conn.execute(
                "INSERT INTO price_overrides (key, prompt_per_1m, completion_per_1m) "
                "VALUES ('prov/model', 1.5, 3.0)")
        with self._db(seed):
            result = pricing.get_pricing("prov", "model", refresh=False)
        self.assertEqual(result["prompt_per_1m"], 1.5)
        self.assertEqual(result["completion_per_1m"], 3.0)

    def test_catalog_exact_key_match(self):
        def seed(conn):
            conn.execute(
                "INSERT INTO openrouter_cache (model_id, prompt, completion) "
                "VALUES ('prov/model', '0.000001', '0.000002')")
        with self._db(seed):
            result = pricing.get_pricing("prov", "model", refresh=False)
        # '0.000001' за токен → 1.0 за 1M токенов.
        self.assertAlmostEqual(result["prompt_per_1m"], 1.0)
        self.assertAlmostEqual(result["completion_per_1m"], 2.0)

    def test_missing_returns_empty_pricing_with_provider_note(self):
        def seed(conn):
            conn.execute(
                "INSERT INTO provider_notes (provider, note) "
                "VALUES ('prov', 'нет в каталоге')")
        with self._db(seed):
            result = pricing.get_pricing("prov", "model", refresh=False)
        self.assertIsNone(result["prompt_per_1m"])
        self.assertIsNone(result["completion_per_1m"])
        self.assertEqual(result["note"], "нет в каталоге")


class IterArtifactContentsTests(unittest.TestCase):
    """db.iter_artifact_contents — пакетное чтение артефактов, в т.ч. битый blob."""

    def _seed_report(self, conn):
        conn.execute(
            "INSERT INTO reports (project, provider, model, started_at, rel_path, "
            "raw_json) VALUES ('p','v','m','2026-01-01T00:00:00','r.json','{}')")
        return conn.execute("SELECT id FROM reports").fetchone()[0]

    def _add_artifact(self, conn, report_id, run_idx, path, sha, encoding, blob):
        conn.execute(
            "INSERT OR IGNORE INTO file_blobs (sha256, size_bytes, content_encoding, "
            "content_blob) VALUES (?,?,?,?)", (sha, len(blob), encoding, blob))
        conn.execute(
            "INSERT INTO run_artifacts (report_id, run_idx, path, kind, sha256) "
            "VALUES (?,?,?,?,?)", (report_id, run_idx, path, "agent_file", sha))

    def test_yields_decoded_contents_ordered(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                with conn:
                    rid = self._seed_report(conn)
                    self._add_artifact(conn, rid, 1, "b.txt", "s2", "zlib",
                                       zlib.compress(b"second"))
                    self._add_artifact(conn, rid, 0, "a.txt", "s1", "zlib",
                                       zlib.compress(b"first"))
                got = list(db.iter_artifact_contents(conn, rid))
            finally:
                conn.close()
        # Упорядочено по (run_idx, path); содержимое распаковано.
        self.assertEqual(got, [(0, "a.txt", b"first"), (1, "b.txt", b"second")])

    def test_run_idx_filter(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                with conn:
                    rid = self._seed_report(conn)
                    self._add_artifact(conn, rid, 0, "a.txt", "s1", "identity", b"x")
                    self._add_artifact(conn, rid, 1, "b.txt", "s2", "identity", b"y")
                got = list(db.iter_artifact_contents(conn, rid, run_idx=1))
            finally:
                conn.close()
        self.assertEqual(got, [(1, "b.txt", b"y")])

    def test_corrupted_zlib_blob_raises(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                with conn:
                    rid = self._seed_report(conn)
                    # encoding=zlib, но содержимое — не валидный zlib-поток.
                    self._add_artifact(conn, rid, 0, "broken.txt", "sbad", "zlib",
                                       b"not a zlib stream")
                with self.assertRaises(zlib.error):
                    list(db.iter_artifact_contents(conn, rid))
            finally:
                conn.close()


class DashboardServerUnitTests(unittest.TestCase):
    """dashboard_server: _db_fingerprint (mtime) и cleanup_index_snapshot."""

    def test_db_fingerprint_missing_db_returns_zero(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "nope.db"
            with mock.patch.object(dashboard_server, "DB_PATH", missing):
                self.assertEqual(dashboard_server._db_fingerprint(), 0.0)

    def test_db_fingerprint_returns_newest_mtime(self):
        with tempfile.TemporaryDirectory() as td:
            dbf = Path(td) / "main.db"
            dbf.write_bytes(b"x")
            wal = Path(td) / "main.db-wal"
            wal.write_bytes(b"y")
            import os
            os.utime(dbf, (1000, 1000))
            os.utime(wal, (5000, 5000))
            with mock.patch.object(dashboard_server, "DB_PATH", dbf):
                self.assertEqual(dashboard_server._db_fingerprint(), 5000.0)

    def test_cleanup_index_snapshot_removes_file(self):
        with tempfile.TemporaryDirectory() as td:
            idx = Path(td) / "index.json"
            idx.write_text("{}", encoding="utf-8")
            dashboard_server.cleanup_index_snapshot(idx)
            self.assertFalse(idx.exists())

    def test_cleanup_index_snapshot_missing_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            # Не должно бросить на отсутствующем файле.
            dashboard_server.cleanup_index_snapshot(Path(td) / "absent.json")


class CliSmokeTests(unittest.TestCase):
    """P3: CLI поднимаются и печатают help без падений (без сети/opencode/БД)."""

    def _run_help(self, *args):
        proc = subprocess.run(
            [sys.executable, *args, "--help"],
            cwd=str(PROJECT_ROOT), capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("usage", (proc.stdout + proc.stderr).lower())

    def test_bench_help(self):
        self._run_help("bench.py")

    def test_check_models_help(self):
        self._run_help("check_models.py")

    def test_model_exclusions_help(self):
        self._run_help("scripts/model_exclusions.py")

    def test_run_artifacts_help(self):
        self._run_help("scripts/run_artifacts.py")


if __name__ == "__main__":
    unittest.main()

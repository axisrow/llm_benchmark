import json
import tempfile
import unittest
from pathlib import Path

import bench
import build_index
import db
import usage as usage_metrics


class FakeResponse:
    status_code = 200
    text = "{}"

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload


class FakeHttpClient:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, path, json=None, timeout=None):
        if path == "/session":
            return FakeResponse({"id": "ses_test"})
        if path == "/session/ses_test/message":
            return FakeResponse({"info": {}})
        raise AssertionError(path)

    def get(self, path, timeout=None):
        if path == "/session/ses_test/message":
            return FakeResponse([])
        raise AssertionError(path)


class BrokenSSE:
    def __enter__(self):
        raise RuntimeError("simulated SSE disconnect")

    def __exit__(self, *args):
        return False


class BenchCriticalBugTests(unittest.TestCase):
    def test_sse_disconnect_is_error_not_success(self):
        orig_client = bench.httpx.Client
        orig_sse = bench.httpx_sse.connect_sse
        orig_tail = bench._opencode_error_tail
        try:
            bench.httpx.Client = FakeHttpClient
            bench.httpx_sse.connect_sse = lambda *args, **kwargs: BrokenSSE()
            bench._opencode_error_tail = lambda session_id: None

            result = bench.probe_session(
                task="ping",
                model="m",
                provider="p",
                agent="coder",
                timeout=2,
                port=4096,
                write=lambda msg: None,
            )
        finally:
            bench.httpx.Client = orig_client
            bench.httpx_sse.connect_sse = orig_sse
            bench._opencode_error_tail = orig_tail

        self.assertEqual(result.code, 2)
        self.assertIn("SSE reader error", result.reason or "")
        self.assertIsNone(result.usage)

    def test_run_copy_converts_session_crash_to_error_result(self):
        orig_ensure = bench.ensure_server_running
        orig_probe_session = bench.probe_session
        try:
            bench.ensure_server_running = lambda work_dir, port, status: True

            def crash(**kwargs):
                raise RuntimeError("simulated crash")

            bench.probe_session = crash
            with tempfile.TemporaryDirectory() as td:
                result = bench.run_copy(
                    index=1,
                    work_dir=Path(td),
                    port=4096,
                    task="task",
                    model="m",
                    provider="p",
                    agent="coder",
                    timeout=1,
                )
                log_text = (Path(td) / "run.log").read_text(encoding="utf-8")
        finally:
            bench.ensure_server_running = orig_ensure
            bench.probe_session = orig_probe_session

        self.assertEqual(result["code"], 2)
        self.assertIn("simulated crash", log_text)

    def test_existing_unowned_server_is_port_conflict(self):
        orig_try = bench._try_connect
        orig_popen = bench.subprocess.Popen
        orig_owners = dict(bench._server_owners)
        popen_calls = []
        statuses = []
        try:
            bench._server_owners.clear()
            bench._try_connect = lambda port: True

            def fake_popen(*args, **kwargs):
                popen_calls.append((args, kwargs))
                raise AssertionError("Popen should not be called")

            bench.subprocess.Popen = fake_popen
            with tempfile.TemporaryDirectory() as td:
                ok = bench.ensure_server_running(Path(td), 4096, statuses.append)
        finally:
            bench._try_connect = orig_try
            bench.subprocess.Popen = orig_popen
            bench._server_owners.clear()
            bench._server_owners.update(orig_owners)

        self.assertFalse(ok)
        self.assertEqual(popen_calls, [])
        self.assertTrue(statuses)

    def test_extract_usage_from_opencode_wrapper_shape(self):
        usage = usage_metrics.extract_usage_from_message({
            "info": {
                "role": "assistant",
                "cost": 0.0123,
                "tokens": {
                    "input": 1000,
                    "output": 200,
                    "reasoning": 30,
                    "cache": {"read": 400, "write": 50},
                },
            },
            "parts": [],
        })
        usage_dict = usage.to_report_dict()

        self.assertEqual(usage_dict["input_tokens"], 1000)
        self.assertEqual(usage_dict["output_tokens"], 200)
        self.assertEqual(usage_dict["reasoning_tokens"], 30)
        self.assertEqual(usage_dict["cache_read_tokens"], 400)
        self.assertEqual(usage_dict["cache_write_tokens"], 50)
        self.assertEqual(usage_dict["total_tokens"], 1230)
        self.assertEqual(usage_dict["opencode_cost_usd"], 0.0123)

    def test_extract_usage_from_direct_assistant_message_shape(self):
        usage = usage_metrics.extract_usage_from_message({
            "role": "assistant",
            "cost": 0,
            "tokens": {
                "input": 10.0,
                "output": 5.0,
                "reasoning": 0.0,
                "cache": {"read": 0, "write": 0},
            },
        })
        usage_dict = usage.to_report_dict()

        self.assertEqual(usage_dict["input_tokens"], 10)
        self.assertEqual(usage_dict["output_tokens"], 5)
        self.assertEqual(usage_dict["total_tokens"], 15)
        self.assertEqual(usage_dict["opencode_cost_usd"], 0.0)

    def test_estimate_usage_cost_normal_free_and_missing(self):
        usage = usage_metrics.Usage(input_tokens=1_000_000, output_tokens=500_000)

        priced = usage_metrics.estimate_usage_cost(
            usage, {"prompt_per_1m": 1.0, "completion_per_1m": 2.0},
        ).to_report_dict()
        self.assertEqual(priced["estimated_prompt_cost_usd"], 1.0)
        self.assertEqual(priced["estimated_completion_cost_usd"], 1.0)
        self.assertEqual(priced["estimated_cost_usd"], 2.0)

        free = usage_metrics.estimate_usage_cost(
            usage, {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
        ).to_report_dict()
        self.assertEqual(free["estimated_cost_usd"], 0.0)

        missing = usage_metrics.estimate_usage_cost(
            usage, {"prompt_per_1m": None, "completion_per_1m": 2.0},
        ).to_report_dict()
        self.assertIsNone(missing["estimated_cost_usd"])

        self.assertIsNone(usage_metrics.estimate_usage_cost(None, {}))

    def test_upsert_report_keeps_usage_only_in_raw_json(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                report = {
                    "project": "p",
                    "provider": "provider",
                    "model": "model",
                    "started_at": "2026-01-01T00:00:00",
                    "summary": {"ok": 1, "timeout": 0, "error": 0},
                    "runs": [
                        {
                            "index": 1,
                            "port": 4096,
                            "dir": "/tmp/run",
                            "status": "готово",
                            "code": 0,
                            "elapsed": 1.0,
                            "usage": {
                                "input_tokens": 100,
                                "output_tokens": 20,
                                "reasoning_tokens": 5,
                                "cache_read_tokens": 7,
                                "cache_write_tokens": 3,
                                "total_tokens": 125,
                                "estimated_prompt_cost_usd": 0.0001,
                                "estimated_completion_cost_usd": 0.0002,
                                "estimated_cost_usd": 0.0003,
                                "opencode_cost_usd": 0.0004,
                            },
                        },
                        {
                            "index": 2,
                            "port": 4097,
                            "dir": "/tmp/run2",
                            "status": "ошибка",
                            "code": 2,
                            "elapsed": 2.0,
                        },
                    ],
                }
                with conn:
                    report_id = db.upsert_report(
                        conn,
                        report,
                        "data/result/p/report.json",
                        json.dumps(report),
                    )
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
                rows = conn.execute(
                    """
                    SELECT idx, port, status, code, elapsed
                    FROM runs
                    WHERE report_id = ?
                    ORDER BY idx
                    """,
                    (report_id,),
                ).fetchall()
                raw_json = conn.execute(
                    "SELECT raw_json FROM reports WHERE id = ?", (report_id,),
                ).fetchone()["raw_json"]
            finally:
                conn.close()

        self.assertNotIn("input_tokens", columns)
        self.assertEqual(rows[0]["port"], 4096)
        self.assertEqual(rows[0]["status"], "готово")
        self.assertEqual(rows[1]["code"], 2)
        self.assertEqual(json.loads(raw_json)["runs"][0]["usage"]["total_tokens"], 125)

    def test_init_schema_drops_legacy_run_usage_columns(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                conn.executescript(
                    """
                    CREATE TABLE reports (
                        id              INTEGER PRIMARY KEY,
                        project         TEXT NOT NULL,
                        provider        TEXT NOT NULL,
                        model           TEXT NOT NULL,
                        started_at      TEXT NOT NULL,
                        run_elapsed     REAL,
                        copies          INTEGER,
                        summary_ok      INTEGER NOT NULL DEFAULT 0,
                        summary_timeout INTEGER NOT NULL DEFAULT 0,
                        summary_error   INTEGER NOT NULL DEFAULT 0,
                        rel_path        TEXT NOT NULL,
                        raw_json        TEXT NOT NULL,
                        UNIQUE (project, provider, model, started_at)
                    );
                    CREATE TABLE runs (
                        report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
                        idx       INTEGER NOT NULL,
                        port      INTEGER,
                        dir       TEXT,
                        status    TEXT,
                        code      INTEGER,
                        elapsed   REAL,
                        input_tokens INTEGER,
                        estimated_cost_usd REAL,
                        PRIMARY KEY (report_id, idx)
                    );
                    """
                )
                raw_json = json.dumps({
                    "runs": [{"index": 1, "usage": {"total_tokens": 12}}],
                })
                conn.execute(
                    """
                    INSERT INTO reports
                        (id, project, provider, model, started_at, rel_path, raw_json)
                    VALUES (1, 'p', 'provider', 'model', '2026-01-01T00:00:00',
                            'data/result/p/report.json', ?)
                    """,
                    (raw_json,),
                )
                conn.execute(
                    """
                    INSERT INTO runs
                        (report_id, idx, port, dir, status, code, elapsed,
                         input_tokens, estimated_cost_usd)
                    VALUES (1, 1, 4096, '/tmp/run', 'готово', 0, 1.0, 10, 0.01)
                    """
                )
                db.init_schema(conn)
                columns = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
                row = conn.execute("SELECT * FROM runs").fetchone()
                stored_raw_json = conn.execute(
                    "SELECT raw_json FROM reports WHERE id = 1"
                ).fetchone()["raw_json"]
            finally:
                conn.close()

        self.assertNotIn("input_tokens", columns)
        self.assertNotIn("estimated_cost_usd", columns)
        self.assertEqual(row["port"], 4096)
        self.assertEqual(json.loads(stored_raw_json)["runs"][0]["usage"]["total_tokens"], 12)

    def test_cleanup_index_snapshot_deletes_existing_file_and_missing_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            index_path = Path(td) / "docs" / "data" / "index.json"
            index_path.parent.mkdir(parents=True)
            index_path.write_text("{}", encoding="utf-8")

            bench._cleanup_index_snapshot(index_path)
            bench._cleanup_index_snapshot(index_path)

        self.assertFalse(index_path.exists())

    def test_serve_removes_generated_index_on_exit(self):
        import socketserver

        original_project_root = bench.PROJECT_ROOT
        original_build_index = build_index.build_index
        original_tcp_server = socketserver.TCPServer

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            index_path = root / "docs" / "data" / "index.json"
            seen = {"index_exists_during_serve": False}

            def fake_build_index():
                index_path.parent.mkdir(parents=True, exist_ok=True)
                index_path.write_text('{"total": 0}', encoding="utf-8")
                return 0

            class FakeTCPServer:
                def __init__(self, address, handler):
                    self.address = address
                    self.handler = handler

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def serve_forever(self):
                    seen["index_exists_during_serve"] = index_path.exists()

            try:
                bench.PROJECT_ROOT = root
                build_index.build_index = fake_build_index
                socketserver.TCPServer = FakeTCPServer
                bench.serve(9999)
            finally:
                bench.PROJECT_ROOT = original_project_root
                build_index.build_index = original_build_index
                socketserver.TCPServer = original_tcp_server

            self.assertTrue(seen["index_exists_during_serve"])
            self.assertFalse(index_path.exists())

    def test_build_index_accepts_old_report_without_usage(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                report = {
                    "project": "old",
                    "provider": "provider",
                    "model": "model",
                    "started_at": "2026-01-01T00:00:00",
                    "summary": {"ok": 1, "timeout": 0, "error": 0},
                    "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                    "runs": [
                        {
                            "index": 1,
                            "port": 4096,
                            "dir": "/tmp/run",
                            "status": "готово",
                            "code": 0,
                            "elapsed": 1.0,
                        },
                    ],
                }
                with conn:
                    db.upsert_report(
                        conn,
                        report,
                        "data/result/old/report.json",
                        json.dumps(report),
                    )
            finally:
                conn.close()

            original_connect = build_index.connect
            original_project_root = build_index.PROJECT_ROOT
            try:
                build_index.connect = lambda: db.connect(db_path)
                build_index.PROJECT_ROOT = root
                count = build_index.build_index()
            finally:
                build_index.connect = original_connect
                build_index.PROJECT_ROOT = original_project_root

            data = json.loads((root / "docs" / "data" / "index.json").read_text())

        run = data["projects"][0]["reports"][0]["runs"][0]
        self.assertEqual(count, 1)
        self.assertNotIn("usage", run)


if __name__ == "__main__":
    unittest.main()

import tempfile
import unittest
from pathlib import Path

import bench


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

            code, reason = bench.probe_session(
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

        self.assertEqual(code, 2)
        self.assertIn("SSE reader error", reason or "")

    def test_run_copy_converts_session_crash_to_error_result(self):
        orig_ensure = bench.ensure_server_running
        orig_run_task = bench.run_task
        try:
            bench.ensure_server_running = lambda work_dir, port, status: True

            def crash(**kwargs):
                raise RuntimeError("simulated crash")

            bench.run_task = crash
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
            bench.run_task = orig_run_task

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


if __name__ == "__main__":
    unittest.main()

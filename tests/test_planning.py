import json
import sqlite3
import unittest
from unittest import mock

from db import SCHEMA, upsert_report
from planning_questions import (
    QuestionProtocolError,
    capture_question_request,
)
import opencode_runtime as runtime
import bench


class QuestionResponderTests(unittest.TestCase):
    def test_recommended_is_case_insensitive_in_label_only(self):
        captured, answers = capture_question_request({
            "id": "q1", "sessionID": "s1", "questions": [{
                "header": "DB", "question": "Which?", "multiple": False,
                "custom": True, "options": [
                    {"label": "SQLite", "description": "Recommended here"},
                    {"label": "Postgres (RECOMMENDED)", "description": "other"},
                ],
            }],
        }, "recommended", attempt_idx=2, elapsed=1.5)
        self.assertEqual(answers, [["Postgres (RECOMMENDED)"]])
        self.assertFalse(captured[0]["fallback_used"])
        self.assertEqual(captured[0]["attempt_idx"], 2)

    def test_recommended_multiple_and_fallback(self):
        captured, answers = capture_question_request({
            "id": "q1", "sessionID": "s1", "questions": [
                {"question": "Many", "multiple": True, "options": [
                    {"label": "A recommended", "description": ""},
                    {"label": "B RECOMMENDED", "description": ""},
                ]},
                {"question": "Fallback", "multiple": False, "options": [
                    {"label": "First", "description": ""},
                    {"label": "Second", "description": ""},
                ]},
            ],
        }, "recommended", attempt_idx=1, elapsed=0)
        self.assertEqual(answers, [["A recommended", "B RECOMMENDED"], ["First"]])
        self.assertTrue(captured[1]["fallback_used"])

    def test_first_always_selects_first(self):
        captured, answers = capture_question_request({
            "id": "q", "questions": [{"question": "Q", "multiple": True,
                                         "options": [{"label": "A"},
                                                     {"label": "B recommended"}]}]},
            "first", attempt_idx=1, elapsed=0)
        self.assertEqual(answers, [["A"]])
        self.assertFalse(captured[0]["fallback_used"])

    def test_empty_options_is_protocol_error(self):
        with self.assertRaises(QuestionProtocolError):
            capture_question_request(
                {"id": "q", "questions": [{"question": "Q", "options": []}]},
                "recommended", attempt_idx=1, elapsed=0)

    def test_invalid_question_is_captured_as_error_record(self):
        """Дефект 1: невалидный вопрос (пустые options) всё равно формирует
        нормализованную запись с reply_status='error'+санитизированный reply_error,
        и QuestionProtocolError несёт её в .questions — иначе error-вопрос не
        доходит до runs[].questions/agent_questions."""
        with self.assertRaises(QuestionProtocolError) as error:
            capture_question_request(
                {"id": "q", "sessionID": "s1",
                 "questions": [{"header": "H", "question": "Q", "options": []}]},
                "recommended", attempt_idx=1, elapsed=0.5)
        questions = error.exception.questions
        self.assertEqual(len(questions), 1)
        record = questions[0]
        self.assertEqual(record["reply_status"], "error")
        self.assertIsNotNone(record["reply_error"])
        # Запись нормализована как валидная: те же поля, что у обычного вопроса.
        self.assertEqual(record["request_id"], "q")
        self.assertEqual(record["session_id"], "s1")
        self.assertEqual(record["question_idx"], 1)
        self.assertEqual(record["question"], "Q")
        # answer/options не способны что-либо выбрать — но структура сохранена.
        self.assertEqual(record["answer"], [])
        self.assertEqual(record["options"], [])
        self.assertFalse(record["fallback_used"])

    def test_error_on_second_question_keeps_first(self):
        """Дефект 1: если второй вопрос невалиден, первый (уже нормализованный)
        не теряется — QuestionProtocolError несёт обе записи."""
        with self.assertRaises(QuestionProtocolError) as error:
            capture_question_request({
                "id": "q", "sessionID": "s1", "questions": [
                    {"header": "H1", "question": "Q1", "multiple": False,
                     "options": [{"label": "A"}, {"label": "B recommended"}]},
                    {"header": "H2", "question": "Q2",
                     "options": [{"label": ""}]},  # нет label — невалиден
                ],
            }, "recommended", attempt_idx=1, elapsed=0)
        questions = error.exception.questions
        self.assertEqual(len(questions), 2)
        # первый — валидный, отвечен
        self.assertEqual(questions[0]["reply_status"], "pending")
        self.assertEqual(questions[0]["question_idx"], 1)
        self.assertEqual(questions[0]["answer"], ["B recommended"])
        # второй — error-запись
        self.assertEqual(questions[1]["reply_status"], "error")
        self.assertEqual(questions[1]["question_idx"], 2)
        self.assertIsNotNone(questions[1]["reply_error"])

    def test_request_without_id_is_protocol_error_with_captured(self):
        """Дефект 1: нет id/questions в запросе — собственно ответить нельзя,
        поэтому error фиксируется без записи (captured пуст), но исключение
        по-прежнему несёт captured (возможно, из предыдущих вопросов)."""
        with self.assertRaises(QuestionProtocolError) as error:
            capture_question_request(
                {"sessionID": "s1", "questions": [{"question": "Q",
                                                    "options": [{"label": "A"}]}]},
                "first", attempt_idx=1, elapsed=0)
        self.assertEqual(error.exception.questions, [])

    def test_custom_defaults_true_and_unknown_responder_rejected(self):
        captured, _answers = capture_question_request(
            {"id": "q", "questions": [{"question": "Q", "options": [{"label": "A"}]}]},
            "first", attempt_idx=1, elapsed=0)
        self.assertTrue(captured[0]["custom"])
        with self.assertRaises(ValueError):
            capture_question_request(
                {"id": "q", "questions": [{"question": "Q",
                                              "options": [{"label": "A"}]}]},
                "bogus", attempt_idx=1, elapsed=0)


class QuestionPersistenceTests(unittest.TestCase):
    def test_upsert_replaces_agent_questions_without_duplicates(self):
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.executescript(SCHEMA)
        report = {
            "project": "p", "provider": "v", "model": "m",
            "started_at": "2026-01-01", "copies": 1, "summary": {},
            "runs": [{"index": 1, "questions": [{
                "attempt_idx": 1, "session_id": "s", "request_id": "q",
                "round_idx": 1, "question_idx": 1, "header": "H",
                "question": "Q", "options": [{"label": "A"}],
                "multiple": False, "custom": True, "answer": ["A"],
                "responder": "first", "fallback_used": False,
                "reply_status": "replied", "reply_error": None,
                "elapsed": 0.5,
            }]}],
        }
        raw = json.dumps(report)
        rid = upsert_report(conn, report, "r", raw)
        upsert_report(conn, report, "r", raw)
        rows = conn.execute(
            "SELECT * FROM agent_questions WHERE report_id=?", (rid,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(json.loads(rows[0]["answer_json"]), ["A"])
        conn.execute("DELETE FROM reports WHERE id=?", (rid,))
        self.assertEqual(conn.execute("SELECT count(*) FROM agent_questions").fetchone()[0], 0)


class RuntimeReplyTests(unittest.TestCase):
    def test_reply_posts_exact_answers_body(self):
        response = mock.Mock()
        response.raise_for_status.return_value = None
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.post.return_value = response
        payload = {"properties": {"id": "q1", "sessionID": "s1", "questions": [{
            "question": "Choose", "options": [{"label": "A"},
                                                {"label": "B recommended"}],
        }]}}
        with mock.patch.object(runtime.httpx, "Client", return_value=client):
            captured = runtime._reply_to_question(
                "http://localhost", payload, "recommended", 1, 0)
        client.post.assert_called_once_with(
            "/question/q1/reply", json={"answers": [["B recommended"]]})
        self.assertEqual(captured[0]["reply_status"], "replied")

    def test_sse_reader_deduplicates_question_request(self):
        payload = {"type": "question.asked", "properties": {
            "id": "q1", "sessionID": "s1", "questions": []}}
        idle = {"type": "session.idle", "properties": {"sessionID": "s1"}}
        events = [mock.Mock(data=json.dumps(payload)), mock.Mock(data=json.dumps(payload)),
                  mock.Mock(data=json.dumps(idle))]
        source = mock.MagicMock()
        source.__enter__.return_value = source
        source.iter_sse.return_value = events
        handler = mock.Mock(return_value=[{"request_id": "q1"}])
        done = __import__("threading").Event()
        result = {}
        with mock.patch.object(runtime.httpx_sse, "connect_sse", return_value=source), \
             mock.patch.object(runtime.httpx, "Client"):
            runtime._sse_reader("http://localhost", "s1", done,
                                __import__("threading").Event(), result,
                                lambda _msg: None, question_handler=handler)
        handler.assert_called_once()
        self.assertEqual(result["questions"], [{"request_id": "q1", "round_idx": 1}])

    def test_reply_failure_is_protocol_error(self):
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.post.side_effect = OSError("secret detail")
        client.get.side_effect = OSError("still down")
        payload = {"properties": {"id": "q1", "questions": [{
            "question": "Choose", "options": [{"label": "A"}],
        }]}}
        with mock.patch.object(runtime.httpx, "Client", return_value=client):
            with self.assertRaises(QuestionProtocolError) as error:
                runtime._reply_to_question(
                    "http://localhost", payload, "first", 1, 0)
        self.assertEqual(error.exception.questions[0]["reply_status"], "error")

    def _http_error(self, status: int):
        import httpx
        request = httpx.Request("POST", "http://localhost/question/q1/reply")
        response = httpx.Response(status, request=request,
                                  text=f"provider error {status}")
        return httpx.HTTPStatusError(
            f"HTTP {status}", request=request, response=response)

    def test_http_status_error_is_error_without_reconciliation(self):
        """Дефект 2: POST вернул 4xx/5xx (raise_for_status) — это известный
        отказ сервера, GET /question reconciliation не нужен: сразу error+code=2.
        До правок raise_for_status летел в общий except и ошибочно шёл в GET,
        где запроса нет в pending -> ложный reply_status='replied'."""
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.post.return_value.raise_for_status.side_effect = self._http_error(400)
        payload = {"properties": {"id": "q1", "questions": [{
            "question": "Choose", "options": [{"label": "A"}],
        }]}}
        with mock.patch.object(runtime.httpx, "Client", return_value=client):
            with self.assertRaises(QuestionProtocolError) as error:
                runtime._reply_to_question(
                    "http://localhost", payload, "first", 1, 0)
        client.get.assert_not_called()  # reconciliation не вызывался
        record = error.exception.questions[0]
        self.assertEqual(record["reply_status"], "error")
        self.assertIn("400", record["reply_error"])

    def test_http_500_is_error_without_reconciliation(self):
        """Дефект 2: 5xx — тоже известный отказ сервера, error без GET."""
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.post.return_value.raise_for_status.side_effect = self._http_error(500)
        payload = {"properties": {"id": "q1", "questions": [{
            "question": "Choose", "options": [{"label": "A"}],
        }]}}
        with mock.patch.object(runtime.httpx, "Client", return_value=client):
            with self.assertRaises(QuestionProtocolError):
                runtime._reply_to_question(
                    "http://localhost", payload, "first", 1, 0)
        client.get.assert_not_called()

    def test_transport_error_not_in_pending_is_replied_without_retry(self):
        """Дефект 2: transport/timeout на POST — неизвестно, принял ли сервер.
        GET /question говорит, что запроса уже нет в pending (сервер принял и
        обработал) -> replied. Ретрая POST не делаем."""
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.post.side_effect = OSError("connection reset")
        pending_resp = mock.Mock()
        pending_resp.raise_for_status.return_value = None
        pending_resp.json.return_value = []  # запрос не в pending
        client.get.return_value = pending_resp
        payload = {"properties": {"id": "q1", "questions": [{
            "question": "Choose", "options": [{"label": "A"}],
        }]}}
        with mock.patch.object(runtime.httpx, "Client", return_value=client):
            captured = runtime._reply_to_question(
                "http://localhost", payload, "first", 1, 0)
        self.assertEqual(captured[0]["reply_status"], "replied")
        # POST ровно один (первый упал по transport), второй retry не делали.
        self.assertEqual(client.post.call_count, 1)

    def test_transport_error_still_in_pending_retries_once(self):
        """Дефект 2: transport на POST, но запрос ещё в pending (сервер не
        принял) -> ОДИН retry POST. Retry удался -> replied."""
        client = mock.MagicMock()
        client.__enter__.return_value = client
        ok_response = mock.Mock()
        ok_response.raise_for_status.return_value = None
        client.post.side_effect = [OSError("connection reset"), ok_response]
        pending_resp = mock.Mock()
        pending_resp.raise_for_status.return_value = None
        pending_resp.json.return_value = [{"id": "q1"}]  # ещё в pending
        client.get.return_value = pending_resp
        payload = {"properties": {"id": "q1", "questions": [{
            "question": "Choose", "options": [{"label": "A"}],
        }]}}
        with mock.patch.object(runtime.httpx, "Client", return_value=client):
            captured = runtime._reply_to_question(
                "http://localhost", payload, "first", 1, 0)
        self.assertEqual(captured[0]["reply_status"], "replied")
        self.assertEqual(client.post.call_count, 2)  # исходный + один retry

    def test_transport_error_retry_fails_is_error(self):
        """Дефект 2: transport на POST, запрос в pending, retry тоже упал -> error."""
        client = mock.MagicMock()
        client.__enter__.return_value = client
        client.post.side_effect = OSError("down again")
        pending_resp = mock.Mock()
        pending_resp.raise_for_status.return_value = None
        pending_resp.json.return_value = [{"id": "q1"}]  # в pending
        client.get.return_value = pending_resp
        payload = {"properties": {"id": "q1", "questions": [{
            "question": "Choose", "options": [{"label": "A"}],
        }]}}
        with mock.patch.object(runtime.httpx, "Client", return_value=client):
            with self.assertRaises(QuestionProtocolError) as error:
                runtime._reply_to_question(
                    "http://localhost", payload, "first", 1, 0)
        self.assertEqual(error.exception.questions[0]["reply_status"], "error")
        self.assertEqual(client.post.call_count, 2)  # ровно один retry

    def test_sse_reader_off_mode_ignores_question(self):
        question = {"type": "question.asked", "properties": {
            "id": "q1", "sessionID": "s1", "questions": []}}
        idle = {"type": "session.idle", "properties": {"sessionID": "s1"}}
        source = mock.MagicMock()
        source.__enter__.return_value = source
        source.iter_sse.return_value = [mock.Mock(data=json.dumps(question)),
                                        mock.Mock(data=json.dumps(idle))]
        result = {}
        with mock.patch.object(runtime.httpx_sse, "connect_sse", return_value=source), \
             mock.patch.object(runtime.httpx, "Client"):
            runtime._sse_reader("http://localhost", "s1", __import__("threading").Event(),
                                __import__("threading").Event(), result,
                                lambda _msg: None, question_handler=None)
        self.assertNotIn("questions", result)


class PlanningCliTests(unittest.TestCase):
    def test_cli_planning_defaults_to_planner_and_recommended(self):
        seen = {}
        with mock.patch("sys.argv", ["bench.py", "--project", "p",
                                     "--planning", "on", "task"]), \
             mock.patch.object(bench, "install_shutdown_handlers"), \
             mock.patch.object(bench, "run_benchmark",
                               side_effect=lambda args: seen.update(vars(args)) or 0):
            with self.assertRaises(SystemExit) as exit_info:
                bench.main()
        self.assertEqual(exit_info.exception.code, 0)
        self.assertEqual(seen["agent"], "bench_planner")
        self.assertEqual(seen["question_responder"], "recommended")

    def test_cli_off_preserves_coder_and_allows_first(self):
        seen = {}
        with mock.patch("sys.argv", ["bench.py", "--project", "p",
                                     "--question-responder", "first", "task"]), \
             mock.patch.object(bench, "install_shutdown_handlers"), \
             mock.patch.object(bench, "run_benchmark",
                               side_effect=lambda args: seen.update(vars(args)) or 0):
            with self.assertRaises(SystemExit):
                bench.main()
        self.assertEqual(seen["planning"], "off")
        self.assertEqual(seen["agent"], runtime.DEFAULT_AGENT)
        self.assertEqual(seen["question_responder"], "first")


class PlanningReportTests(unittest.TestCase):
    """Sub-issue #81: проброс planning в probe_session и questions в отчёт."""

    def _question(self, **overrides):
        base = {
            "attempt_idx": 1, "session_id": "ses_test",
            "request_id": "q1", "round_idx": 0, "question_idx": 1,
            "header": "DB", "question": "Which DB?",
            "options": [{"label": "SQLite (recommended)", "description": ""}],
            "multiple": False, "custom": True,
            "answer": ["SQLite (recommended)"], "responder": "recommended",
            "fallback_used": False, "reply_status": "replied",
            "reply_error": None, "elapsed": 0.3,
        }
        base.update(overrides)
        return base

    def test_run_copy_propagates_questions_when_planning_on(self):
        """run_copy пробрасывает planning/question_responder в probe_session и
        сохраняет session_result.questions в результат. До правок #81 — красный:
        run_copy не принимал planning и отбрасывал questions."""
        import tempfile
        from pathlib import Path
        from opencode_runtime import SessionProbeResult
        import benchmark_report

        seen_kwargs = {}
        orig_ensure = benchmark_report.ensure_server_running
        orig_probe = benchmark_report.probe_session
        try:
            benchmark_report.ensure_server_running = (
                lambda work_dir, port, status: True)

            def fake_probe(**kwargs):
                seen_kwargs.update(kwargs)
                return SessionProbeResult(
                    code=0, reason=None, usage=None,
                    rate_limited=False,
                    questions=(self._question(question="Which DB?"),))

            benchmark_report.probe_session = fake_probe
            with tempfile.TemporaryDirectory() as td:
                result = benchmark_report.run_copy(
                    index=1, work_dir=Path(td), port=4096,
                    task="task", model="m", provider="p",
                    agent="bench_planner", timeout=1,
                    planning=True, question_responder="first",
                )
        finally:
            benchmark_report.ensure_server_running = orig_ensure
            benchmark_report.probe_session = orig_probe

        self.assertTrue(seen_kwargs.get("planning"))
        self.assertEqual(seen_kwargs.get("question_responder"), "first")
        self.assertEqual(result["code"], 0)
        self.assertEqual(len(result["questions"]), 1)
        self.assertEqual(result["questions"][0]["question"], "Which DB?")
        self.assertEqual(result["questions"][0]["reply_status"], "replied")

    def test_run_copy_empty_questions_when_planning_off(self):
        """run_copy возвращает questions=[] в no-op режиме (planning=False)."""
        import tempfile
        from pathlib import Path
        from opencode_runtime import SessionProbeResult
        import benchmark_report

        orig_ensure = benchmark_report.ensure_server_running
        orig_probe = benchmark_report.probe_session
        try:
            benchmark_report.ensure_server_running = (
                lambda work_dir, port, status: True)
            benchmark_report.probe_session = lambda **kw: SessionProbeResult(0)
            with tempfile.TemporaryDirectory() as td:
                result = benchmark_report.run_copy(
                    index=1, work_dir=Path(td), port=4096,
                    task="task", model="m", provider="p",
                    agent="bench_coder", timeout=1,
                )
        finally:
            benchmark_report.ensure_server_running = orig_ensure
            benchmark_report.probe_session = orig_probe

        self.assertEqual(result["code"], 0)
        self.assertEqual(result["questions"], [])

    def test_summarize_planning_questions_fields(self):
        """Сводка по 5 полям считается по question-записям корректно."""
        import benchmark_report

        results = [
            {"questions": [
                self._question(responder="recommended", fallback_used=False),
                self._question(responder="recommended", fallback_used=True),
                self._question(responder="first", fallback_used=False,
                               reply_status="error"),
            ]},
            {"questions": []},
            {},  # копия без ключа questions вовсе
        ]
        summary = benchmark_report.summarize_planning_questions(results)
        self.assertEqual(summary, {
            "questions": 3,
            "runs_with_questions": 1,
            "recommended_matches": 1,
            "fallbacks_to_first": 1,
            "reply_errors": 1,
        })

    def _run_benchmark_into_db(self, planning, run_copy, *,
                               copies=1, agent="bench_coder",
                               responder="recommended"):
        """Гоняет run_benchmark во временную БД с замоканными внешними слоями.

        ``run_copy`` — либо dict (одинаковый для всех копий), либо callable,
        принимающий индекс копии (1-based) и возвращающий её результат.
        """
        import io
        import contextlib
        import tempfile
        from pathlib import Path
        from types import SimpleNamespace
        import db as dbmod
        import benchmark_report

        if callable(run_copy):
            run_copy_fn = run_copy
        else:
            def run_copy_fn(index):  # noqa: E704 — compact test stub
                return dict(run_copy)

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            work_dir = Path(td) / "work"
            work_dir.mkdir()
            conn = dbmod.connect(db_path)
            try:
                dbmod.init_schema(conn)
            finally:
                conn.close()

            original_connect = dbmod.connect
            original_session = benchmark_report.session
            original_prepare = benchmark_report.prepare_work_dirs
            original_run_copy = benchmark_report.run_copy
            original_get_pricing = benchmark_report.get_pricing
            original_collect = benchmark_report.collect_report_artifacts
            original_cleanup = benchmark_report.cleanup_collected_artifacts
            try:
                dbmod.connect = lambda *a, **k: original_connect(db_path)
                benchmark_report.connect = dbmod.connect
                # save_report открывает БД через session(), а не connect() —
                # патчим именно его, иначе тест пишет в боевую data/main.db.
                benchmark_report.session = lambda *a, **k: original_session(db_path)
                benchmark_report.prepare_work_dirs = lambda *a: [work_dir] * copies
                benchmark_report.run_copy = (
                    lambda *a, **kw: run_copy_fn(kw.get("index", a[0] if a else 1)))
                benchmark_report.get_pricing = lambda p, m: {
                    "prompt_per_1m": 0.0, "completion_per_1m": 0.0}
                benchmark_report.collect_report_artifacts = lambda r: SimpleNamespace(
                    artifacts=[], summary=lambda: {})
                benchmark_report.cleanup_collected_artifacts = lambda c: None

                with contextlib.redirect_stderr(io.StringIO()):
                    benchmark_report.run_benchmark(SimpleNamespace(
                        project="ad_hoc", file=None, task="task",
                        provider="provider", model="model", copies=copies,
                        base_port=4096, agent=agent, timeout=1,
                        planning=planning, question_responder=responder,
                        force_excluded=False,
                    ))
                conn = dbmod.connect(db_path)
                try:
                    row = conn.execute(
                        "SELECT raw_json FROM reports WHERE project = 'ad_hoc'",
                    ).fetchone()
                    raw_json = row["raw_json"]
                finally:
                    conn.close()
            finally:
                dbmod.connect = original_connect
                benchmark_report.connect = original_connect
                benchmark_report.session = original_session
                benchmark_report.prepare_work_dirs = original_prepare
                benchmark_report.run_copy = original_run_copy
                benchmark_report.get_pricing = original_get_pricing
                benchmark_report.collect_report_artifacts = original_collect
                benchmark_report.cleanup_collected_artifacts = original_cleanup

        return json.loads(raw_json)

    def test_run_benchmark_planning_off_omits_planning_keys(self):
        """No-change-when-off: при planning='off' в отчёте нет planning,
        planning_summary и runs[].questions (байт-в-байт для coding-отчётов)."""
        report = self._run_benchmark_into_db(
            "off",
            {"index": 1, "port": 4096, "dir": "d", "code": 0,
             "elapsed": 0.1, "usage": None, "questions": []},
        )
        self.assertNotIn("planning", report)
        self.assertNotIn("planning_summary", report)
        self.assertNotIn("questions", report["runs"][0])

    def test_run_benchmark_planning_on_includes_questions_and_summary(self):
        """При planning='on' отчёт содержит planning-объект (enabled/agent/
        responder), planning_summary и runs[].questions с реальными вопросами."""
        question = self._question(question="Which DB?", responder="first")
        report = self._run_benchmark_into_db(
            "on",
            {"index": 1, "port": 4096, "dir": "d", "code": 0,
             "elapsed": 0.1, "usage": None, "questions": [question]},
            agent="bench_planner", responder="first",
        )
        self.assertEqual(report["planning"], {
            "enabled": True,
            "agent": "bench_planner",
            "responder": "first",
        })
        self.assertEqual(report["planning_summary"]["questions"], 1)
        self.assertEqual(report["planning_summary"]["runs_with_questions"], 1)
        # question с responder='first' → не recommended-match
        self.assertEqual(report["planning_summary"]["recommended_matches"], 0)
        self.assertIn("questions", report["runs"][0])
        self.assertEqual(report["runs"][0]["questions"][0]["question"], "Which DB?")

    def test_run_benchmark_planning_on_empty_questions_array(self):
        """Дефект 3: в planning-отчёте runs[].questions присутствует ВСЕГДА —
        пустой массив, если вопросов не было (а не отсутствующий ключ). Сводка
        для пустого прогона: questions==0, runs_with_questions==0."""
        report = self._run_benchmark_into_db(
            "on",
            {"index": 1, "port": 4096, "dir": "d", "code": 0,
             "elapsed": 0.1, "usage": None, "questions": []},
            agent="bench_planner", responder="first",
        )
        self.assertIn("questions", report["runs"][0])
        self.assertEqual(report["runs"][0]["questions"], [])
        self.assertEqual(report["planning_summary"]["questions"], 0)
        self.assertEqual(report["planning_summary"]["runs_with_questions"], 0)

    def test_run_benchmark_planning_questions_isolated_per_run(self):
        """Issue #81 п.6: вопросы нескольких копий не смешиваются — каждый
        вопрос привязан к своему run_idx и не утекает в чужую копию.

        Дефект 3: у копии без вопросов теперь questions==[] (ключ есть)."""
        def run_copy_for(index):
            # только копия 1 задаёт вопрос; копия 2 — без вопросов
            questions = ([self._question(question=f"Q from copy {index}")]
                         if index == 1 else [])
            return {"index": index, "port": 4096 + index - 1, "dir": f"d{index}",
                    "code": 0, "elapsed": 0.1, "usage": None, "questions": questions}

        report = self._run_benchmark_into_db("on", run_copy_for, copies=2)
        runs_by_index = {r["index"]: r for r in report["runs"]}
        self.assertEqual(report["planning_summary"]["questions"], 1)
        self.assertEqual(report["planning_summary"]["runs_with_questions"], 1)
        self.assertIn("questions", runs_by_index[1])
        self.assertEqual(len(runs_by_index[1]["questions"]), 1)
        self.assertEqual(runs_by_index[1]["questions"][0]["question"], "Q from copy 1")
        # Дефект 3: копия 2 без вопросов имеет questions==[] (ключ присутствует)
        self.assertEqual(runs_by_index[2]["questions"], [])


if __name__ == "__main__":
    unittest.main()

import json
import os
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import artifacts
import bench
import benchmark_report
import opencode_process
import opencode_session
from planning_questions import capture_question_request
from usage import Usage


TASK_TEXT_REPLY = (
    "Ответ на этот вопрос уже содержится в тексте задания. "
    "Перечитай задание и следуй ему буквально."
)

EXACT_TASK = "  Первая строка с Unicode: ёж…\n\nВторая строка\n"


def test_server_environment_toggles_native_plan_mode() -> None:
    with mock.patch.dict(
        os.environ,
        {
            "OPENCODE_EXPERIMENTAL_PLAN_MODE": "inherited",
            "KEEP_ME": "yes",
        },
    ):
        planning_env = opencode_process._server_environment(planning=True)
        coding_env = opencode_process._server_environment(planning=False)

    assert planning_env["OPENCODE_EXPERIMENTAL_PLAN_MODE"] == "1"
    assert coding_env["OPENCODE_EXPERIMENTAL_PLAN_MODE"] == "0"
    assert planning_env["OPENCODE_DB"] == ":memory:"
    assert coding_env["OPENCODE_DB"] == ":memory:"
    assert planning_env["KEEP_ME"] == "yes"
    assert coding_env["KEEP_ME"] == "yes"


def test_run_copy_forwards_planning_mode_to_server_startup() -> None:
    seen: list[bool] = []

    def server_startup(_work_dir, _port, _status, *, planning=False,
                       output_token_max=None, **_kwargs):
        seen.append(planning)
        return False

    with mock.patch.object(
        benchmark_report, "ensure_server_running", server_startup
    ):
        for planning in (False, True):
            with tempfile.TemporaryDirectory() as td:
                result = benchmark_report.run_copy(
                    index=1,
                    work_dir=Path(td),
                    port=4096,
                    task="task",
                    model="model",
                    provider="provider",
                    agent="plan" if planning else "build",
                    timeout=1,
                    planning=planning,
                )
                assert result["reason"] == "opencode serve не поднялся"

    assert seen == [False, True]


class _SSESource:
    def __init__(self, events: list[dict]):
        self.events = events

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_sse(self):
        for event in self.events:
            yield SimpleNamespace(data=json.dumps(event))


class _Response:
    def __init__(self, payload, status_code=200):
        self.payload = payload
        self.status_code = status_code
        self.text = json.dumps(payload)

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"HTTP {self.status_code}")


class _SessionClient:
    calls: list[tuple[str, str, dict | None]] = []

    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def post(self, path, json=None, timeout=None):
        self.calls.append(("POST", path, json))
        if path == "/session":
            return _Response({"id": "s1"})
        if path == "/session/s1/message":
            return _Response({"info": {}})
        if path == "/question/q-control/reply":
            return _Response({})
        if path == "/session/s1/abort":
            return _Response({})
        raise AssertionError(path)

    def get(self, path, timeout=None):
        if path == "/session/s1/message":
            return _Response([
                {"info": {"role": "assistant", "agent": "plan",
                           "tokens": {"input": 10, "output": 2}}},
                {"info": {"role": "assistant", "agent": "build",
                           "tokens": {"input": 20, "output": 4}}},
            ])
        raise AssertionError(path)


def test_cli_defaults_to_build_and_planning_uses_native_plan() -> None:
    seen: list[dict] = []

    def run(args):
        seen.append(vars(args))
        return 0

    with mock.patch("sys.argv", ["bench.py", "--project", "p", "task"]), \
            mock.patch.object(bench, "install_shutdown_handlers"), \
            mock.patch.object(bench, "run_benchmark", side_effect=run):
        try:
            bench.main()
        except SystemExit:
            pass
    with mock.patch("sys.argv", ["bench.py", "--project", "p",
                                 "--planning", "on", "task"]), \
            mock.patch.object(bench, "install_shutdown_handlers"), \
            mock.patch.object(bench, "run_benchmark", side_effect=run):
        try:
            bench.main()
        except SystemExit:
            pass

    assert seen[0]["planning"] == "off"
    assert seen[0]["agent"] == "build"
    assert seen[0]["questions_only"] is False
    assert seen[1]["agent"] == "plan"
    assert seen[1]["question_responder"] == "task-text"
    assert seen[1]["questions_only"] is False


def test_task_text_responder_uses_fixed_answer() -> None:
    captured, answers = capture_question_request(
        {
            "id": "q1",
            "sessionID": "s1",
            "questions": [{
                "question": "Which format?",
                "options": [{"label": "A"}, {"label": "B"}],
                "custom": False,
            }],
        },
        "task-text",
        attempt_idx=1,
        elapsed=0.2,
    )

    assert answers == [[TASK_TEXT_REPLY]]
    assert captured[0]["answer"] == [TASK_TEXT_REPLY]
    assert captured[0]["responder"] == "task-text"
    assert captured[0]["fallback_used"] is False


def test_plan_exit_is_autoapproved_and_excluded_from_questions() -> None:
    tool = {
        "type": "message.part.updated",
        "properties": {"part": {
            "type": "tool", "sessionID": "s1", "callID": "call-1",
            "tool": "plan_exit", "state": {"status": "running"},
        }},
    }
    control = {
        "type": "question.asked",
        "properties": {
            "id": "q-control", "sessionID": "s1",
            "tool": {"messageID": "m1", "callID": "call-1"},
            "questions": [{
                "header": "Build Agent",
                "question": (
                    "Plan at .opencode/plans/123-test.md is complete. "
                    "Would you like to switch to the build agent?"
                ),
                "options": [{"label": "Yes"}, {"label": "No"}],
                "custom": False,
            }],
        },
    }
    idle = {"type": "session.idle", "properties": {"sessionID": "s1"}}
    result: dict = {}
    done = threading.Event()
    plan_exit_handler = mock.Mock()
    ordinary_handler = mock.Mock()

    with mock.patch.object(opencode_session.httpx_sse, "connect_sse",
                           return_value=_SSESource([tool, control, idle])):
        opencode_session._sse_reader(
            "http://localhost", "s1", done, threading.Event(), result,
            lambda _msg: None,
            question_handler=ordinary_handler,
            plan_exit_handler=plan_exit_handler,
        )

    plan_exit_handler.assert_called_once()
    ordinary_handler.assert_not_called()
    assert result.get("questions", []) == []
    assert result["plan_path"] == ".opencode/plans/123-test.md"
    assert result["plan_completed"] is True
    assert done.is_set()


def test_native_plan_switches_to_build_inside_one_session() -> None:
    tool = {
        "type": "message.part.updated",
        "properties": {"part": {
            "type": "tool", "sessionID": "s1", "callID": "call-1",
            "tool": "plan_exit", "state": {"status": "running"},
        }},
    }
    control = {
        "type": "question.asked",
        "properties": {
            "id": "q-control", "sessionID": "s1",
            "tool": {"messageID": "m1", "callID": "call-1"},
            "questions": [{
                "header": "Build Agent",
                "question": (
                    "Plan at .opencode/plans/123-test.md is complete. "
                    "Would you like to switch to the build agent?"
                ),
                "options": [{"label": "Yes"}, {"label": "No"}],
                "custom": False,
            }],
        },
    }
    idle = {"type": "session.idle", "properties": {"sessionID": "s1"}}
    _SessionClient.calls = []

    with mock.patch.object(opencode_session.httpx, "Client", _SessionClient), \
            mock.patch.object(opencode_session.httpx_sse, "connect_sse",
                              return_value=_SSESource([tool, control, idle])), \
            mock.patch.object(opencode_session.time, "sleep"):
        result = opencode_session._probe_session_once(
            EXACT_TASK, "model", "provider", "plan", 5, 4096,
            lambda _msg: None, planning=True, question_responder="task-text",
        )

    task_posts = [call for call in _SessionClient.calls
                  if call[1] == "/session/s1/message"]
    assert len(task_posts) == 1
    assert task_posts[0][2]["agent"] == "plan"
    assert task_posts[0][2]["parts"] == [{
        "type": "text",
        "text": EXACT_TASK,
    }]
    assert ("POST", "/question/q-control/reply",
            {"answers": [["Yes"]]}) in _SessionClient.calls
    assert result.code == 0
    assert result.plan_completed is True
    assert result.plan_path == ".opencode/plans/123-test.md"
    assert result.plan_usage.input_tokens == 10
    assert result.build_usage.input_tokens == 20
    assert result.questions == ()


def test_build_task_payload_is_byte_exact() -> None:
    idle = {"type": "session.idle", "properties": {"sessionID": "s1"}}
    _SessionClient.calls = []

    with mock.patch.object(opencode_session.httpx, "Client", _SessionClient), \
            mock.patch.object(opencode_session.httpx_sse, "connect_sse",
                              return_value=_SSESource([idle])), \
            mock.patch.object(opencode_session.time, "sleep"):
        result = opencode_session._probe_session_once(
            EXACT_TASK, "model", "provider", "build", 5, 4096,
            lambda _msg: None,
        )

    task_posts = [call for call in _SessionClient.calls
                  if call[1] == "/session/s1/message"]
    assert len(task_posts) == 1
    assert task_posts[0][2]["parts"] == [{
        "type": "text",
        "text": EXACT_TASK,
    }]
    assert result.code == 0


def test_questions_only_task_payload_is_byte_exact_and_opt_in() -> None:
    question = {
        "type": "question.asked",
        "properties": {
            "id": "q1",
            "sessionID": "s1",
            "questions": [{
                "question": "Нужно уточнение?",
                "options": [{"label": "Да"}, {"label": "Нет"}],
                "custom": False,
            }],
        },
    }
    _SessionClient.calls = []

    with mock.patch.object(opencode_session.httpx, "Client", _SessionClient), \
            mock.patch.object(opencode_session.httpx_sse, "connect_sse",
                              return_value=_SSESource([question])), \
            mock.patch.object(opencode_session.time, "sleep"):
        result = opencode_session._probe_session_once(
            EXACT_TASK, "model", "provider", "plan", 5, 4096,
            lambda _msg: None, planning=True, questions_only=True,
        )

    task_posts = [call for call in _SessionClient.calls
                  if call[1] == "/session/s1/message"]
    assert len(task_posts) == 1
    assert task_posts[0][2]["parts"] == [{
        "type": "text",
        "text": EXACT_TASK,
    }]
    assert result.code == 0
    assert len(result.questions) == 1
    assert result.questions[0]["reply_status"] == "captured"


def test_planning_idle_without_plan_exit_is_an_error() -> None:
    idle = {"type": "session.idle", "properties": {"sessionID": "s1"}}
    _SessionClient.calls = []

    with mock.patch.object(opencode_session.httpx, "Client", _SessionClient), \
            mock.patch.object(opencode_session.httpx_sse, "connect_sse",
                              return_value=_SSESource([idle])), \
            mock.patch.object(opencode_session.time, "sleep"):
        result = opencode_session._probe_session_once(
            "task", "model", "provider", "plan", 5, 4096,
            lambda _msg: None, planning=True,
        )

    assert result.code == 2
    assert "без plan_exit" in result.reason
    assert result.plan_completed is False


def test_report_contains_separate_phase_metrics_and_plan_snapshot() -> None:
    args = SimpleNamespace(
        project="p", provider="v", model="m", copies=1, planning="on",
        agent="plan", question_responder="task-text", questions_only=False,
    )
    results = [{
        "index": 1, "port": 4096, "dir": "/tmp/work", "code": 0,
        "elapsed": 8.0, "usage": Usage(input_tokens=30), "reason": None,
        "questions": [], "plan_path": "data/result/.opencode/plans/p.md",
        "plan_content": "# Plan\n", "plan_elapsed": 3.0,
        "build_elapsed": 5.0, "plan_usage": Usage(input_tokens=10),
        "build_usage": Usage(input_tokens=20), "plan_completed": True,
    }]

    report = benchmark_report._build_report(
        args, "task", None, None, __import__("datetime").datetime.now(),
        8.0, {"ok": 1}, {}, {}, mock.Mock(summary=lambda: {}), results,
    )

    run = report["runs"][0]
    assert run["plan"] == {
        "path": "data/result/.opencode/plans/p.md",
        "content": "# Plan\n",
    }
    assert run["phases"]["plan"]["status"] == "completed"
    assert run["phases"]["plan"]["elapsed"] == 3.0
    assert run["phases"]["plan"]["usage"]["input_tokens"] == 10
    assert run["phases"]["build"]["status"] == "готово"
    assert run["phases"]["build"]["elapsed"] == 5.0


def test_plan_file_is_not_collected_or_deleted() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        plan = root / ".opencode" / "plans" / "plan.md"
        plan.parent.mkdir(parents=True)
        plan.write_text("# Plan\n", encoding="utf-8")
        (root / "run.log").write_text("log", encoding="utf-8")

        collection = artifacts.collect_run_artifacts(1, root)
        assert ".opencode/plans/plan.md" not in {
            item.path for item in collection.artifacts
        }
        artifacts.cleanup_collected_artifacts(collection)

        assert plan.read_text(encoding="utf-8") == "# Plan\n"


def test_global_native_plan_snapshot_is_saved_safely() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        worktree = root / "worktree"
        work_dir = worktree / "copy"
        work_dir.mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: /dev/null\n", encoding="utf-8")
        global_plans = root / "home" / ".local/share/opencode/plans"
        global_plans.mkdir(parents=True)
        plan = global_plans / "123-plan.md"
        plan.write_text("# Native plan\n", encoding="utf-8")
        plan_ref = os.path.relpath(plan, worktree)

        with mock.patch.object(benchmark_report, "GLOBAL_PLAN_ROOT", global_plans):
            path, content = benchmark_report._read_plan_snapshot(
                work_dir, plan_ref)

        assert path == "~/.local/share/opencode/plans/123-plan.md"
        assert content == "# Native plan\n"


def test_root_relative_native_plan_snapshot_is_saved_safely() -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        worktree = root / "worktree"
        work_dir = worktree / "copy"
        work_dir.mkdir(parents=True)
        (worktree / ".git").write_text("gitdir: /dev/null\n", encoding="utf-8")
        global_plans = root / "home" / ".local/share/opencode/plans"
        global_plans.mkdir(parents=True)
        plan = global_plans / "456-plan.md"
        plan.write_text("# Root-relative plan\n", encoding="utf-8")
        plan_ref = plan.as_posix().lstrip("/")

        with mock.patch.object(benchmark_report, "GLOBAL_PLAN_ROOT", global_plans):
            path, content = benchmark_report._read_plan_snapshot(
                work_dir, plan_ref)

        assert path == "~/.local/share/opencode/plans/456-plan.md"
        assert content == "# Root-relative plan\n"

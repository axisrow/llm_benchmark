"""Regression tests for OpenCode output-limit diagnostics (issue #161)."""

import argparse
import threading
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import bench
import benchmark_report
import opencode_process
import opencode_session
from opencode_base import SessionProbeResult


class _Response:
    status_code = 200

    def __init__(self, payload: object) -> None:
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _Client:
    def __init__(self, messages: list[dict]) -> None:
        self._messages = messages

    def get(self, path: str, timeout: float | None = None) -> _Response:
        assert path == "/session/ses_test/message"
        return _Response(self._messages)


def test_idle_finish_length_is_precise_error() -> None:
    messages = [{
        "info": {
            "role": "assistant",
            "finish": "length",
            "tokens": {
                "input": 9_877,
                "output": 33,
                "reasoning": 31_967,
            },
            "time": {"completed": 1},
        },
        "parts": [],
    }]

    result = opencode_session._classify_outcome(
        "idle",
        None,
        {},
        None,
        "нет ответа за 1000с",
        _Client(messages),  # type: ignore[arg-type]
        "ses_test",
        "build",
        lambda _message: None,
        post_hung=True,
    )

    assert result.code == 2
    assert result.finish_reason == "length"
    assert result.post_hung is False
    assert result.reason is not None
    assert result.reason.startswith("лимит ответа OpenCode исчерпан")
    assert "32 000" in result.reason
    assert result.usage is not None
    assert result.usage.output_tokens + result.usage.reasoning_tokens == 32_000


def test_session_error_with_output_length_is_classified() -> None:
    # issue #161: MessageOutputLengthError может прийти не только терминальным
    # assistant-message (finish=length), но и через SSE session.error (или POST
    # info.error) — _error_text строит из {error:{name:...}} строку. Раньше такая
    # копия уходила в generic provider error без finish_reason; теперь
    # error-first-ветка распознаёт сигнал и возвращает OUTPUT_LENGTH_REASON.
    #
    # Тест гоняет РЕАЛИСТИЧНЫЕ формы SDK-ошибки через _error_text (а не прямую
    # строку): с HTTP-статусом, с message-приоритетом над name, с двоеточием.
    # Cycle-2 review показал, что точный матчер молчал на этих формах.
    messages = [{
        "info": {
            "role": "assistant",
            "error": {"name": "MessageOutputLengthError"},
            "tokens": {
                "input": 9_814,
                "output": 29,
                "reasoning": 31_971,
            },
            "time": {"completed": 1},
        },
        "parts": [],
    }]
    realistic_error_shapes = [
        # голый name (исходный, cycle-1)
        {"name": "MessageOutputLengthError"},
        # SDK часто прикладывает statusCode → "_error_text" дописывает " (HTTP N)"
        {"name": "MessageOutputLengthError", "data": {"statusCode": 500}},
        # message приоритетнее name в _error_text, но имя — внутри
        {"name": "MessageOutputLengthError",
         "message": "MessageOutputLengthError: output budget exceeded"},
    ]

    for err_shape in realistic_error_shapes:
        result = {"error": opencode_session._error_text({"error": err_shape})}
        classified = opencode_session._classify_outcome(
            "error",
            None,
            result,
            None,
            "нет ответа за 1000с",
            _Client(messages),  # type: ignore[arg-type]
            "ses_test",
            "build",
            lambda _message: None,
        )

        assert classified.code == 2, f"shape={err_shape}: code={classified.code}"
        assert classified.finish_reason == "length", \
            f"shape={err_shape}: finish_reason={classified.finish_reason}"
        assert classified.reason is not None
        assert classified.reason.startswith("лимит ответа OpenCode исчерпан"), \
            f"shape={err_shape}: reason={classified.reason!r}"
        assert classified.usage is not None
        assert (classified.usage.output_tokens
                + classified.usage.reasoning_tokens == 32_000)


def test_session_error_generic_stays_generic() -> None:
    # Регресс: обычная (не output-length) ошибка через session.error НЕ должна
    # превращаться в OUTPUT_LENGTH_REASON — её ждёт прежний generic-провайдерский
    # путь с tail'ом из лога opencode.
    result = {"error": "some provider error: rate limited"}

    classified = opencode_session._classify_outcome(
        "error",
        None,
        result,
        None,
        "нет ответа за 1000с",
        _Client([]),  # type: ignore[arg-type]
        "ses_test",
        "build",
        lambda _message: None,
    )

    assert classified.code == 2
    assert classified.reason is not None
    assert not classified.reason.startswith("лимит ответа OpenCode исчерпан")


def test_step_finish_part_supplies_finish_reason() -> None:
    message = {
        "info": {"role": "assistant", "time": {"completed": 1}},
        "parts": [{"type": "step-finish", "reason": "length"}],
    }

    finish, output_length_error = opencode_session._terminal_finish(message)

    assert finish == "length"
    assert output_length_error is True


def test_message_output_length_error_is_detected_without_finish() -> None:
    message = {
        "info": {
            "role": "assistant",
            "error": {"name": "MessageOutputLengthError"},
            "time": {"completed": 1},
        },
        "parts": [],
    }

    finish, output_length_error = opencode_session._terminal_finish(message)

    assert finish is None
    assert output_length_error is True


def test_first_action_timeout_exits_wait_loop() -> None:
    outcome, limit_tail = opencode_session._wait_for_session(
        threading.Event(),
        {"started": time.monotonic()},
        time.monotonic() + 5,
        lambda: None,
        first_action_timeout=0.01,
    )

    assert outcome == "first_action_timeout"
    assert limit_tail is None


def test_action_event_records_first_action_elapsed_once() -> None:
    result = {"started": time.monotonic() - 2}
    tool_event = {
        "type": "message.part.updated",
        "properties": {"part": {"type": "tool", "callID": "call_1"}},
    }

    opencode_session._record_first_action(tool_event, result)
    first = result["first_action_elapsed"]
    opencode_session._record_first_action(tool_event, result)

    assert 1.0 < first < 3.0
    assert result["first_action_elapsed"] == first


def test_user_and_title_text_are_not_agent_actions() -> None:
    result = {"started": time.monotonic() - 1, "agent": "build"}
    user_message = {
        "type": "message.updated",
        "properties": {"info": {
            "id": "msg_user",
            "role": "user",
            "agent": "build",
        }},
    }
    user_part = {
        "type": "message.part.updated",
        "properties": {"part": {
            "messageID": "msg_user",
            "type": "text",
            "text": "benchmark prompt",
        }},
    }
    title_message = {
        "type": "message.updated",
        "properties": {"info": {
            "id": "msg_title",
            "role": "assistant",
            "agent": "title",
        }},
    }
    title_part = {
        "type": "message.part.updated",
        "properties": {"part": {
            "messageID": "msg_title",
            "type": "text",
            "text": "Generated title",
        }},
    }

    for event in (user_message, user_part, title_message, title_part):
        opencode_session._record_message_context(event, result)
        opencode_session._record_first_action(event, result)

    assert "first_action_elapsed" not in result


def test_reasoning_text_delta_is_not_agent_action() -> None:
    result = {"started": time.monotonic() - 1, "agent": "build"}
    events = (
        {
            "type": "message.updated",
            "properties": {"info": {
                "id": "msg_build",
                "role": "assistant",
                "agent": "build",
            }},
        },
        {
            "type": "message.part.updated",
            "properties": {"part": {
                "id": "prt_reasoning",
                "messageID": "msg_build",
                "type": "reasoning",
                "text": "",
            }},
        },
        {
            "type": "message.part.delta",
            "properties": {
                "messageID": "msg_build",
                "partID": "prt_reasoning",
                "field": "text",
                "delta": "thinking",
            },
        },
    )

    for event in events:
        opencode_session._record_message_context(event, result)
        opencode_session._record_first_action(event, result)

    assert "first_action_elapsed" not in result


def test_server_environment_overrides_output_token_max() -> None:
    env = opencode_process._server_environment(
        planning=False,
        output_token_max=65_536,
    )

    assert env["OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX"] == "65536"


def test_validate_runtime_limits() -> None:
    parser = argparse.ArgumentParser()
    valid = argparse.Namespace(
        copies=1,
        timeout=1.0,
        base_port=4096,
        output_token_max=65_536,
        first_action_timeout=360.0,
    )
    bench.validate_benchmark_args(parser, valid)

    for field, value in (("output_token_max", 0), ("first_action_timeout", -1)):
        invalid = argparse.Namespace(**vars(valid))
        setattr(invalid, field, value)
        with mock.patch.object(parser, "error", side_effect=ValueError) as error:
            try:
                bench.validate_benchmark_args(parser, invalid)
            except ValueError:
                pass
        error.assert_called_once()


def test_run_copy_forwards_runtime_limits() -> None:
    session_result = SessionProbeResult(
        2,
        "лимит ответа OpenCode исчерпан",
        finish_reason="length",
        first_action_elapsed=None,
    )
    with TemporaryDirectory() as tmp:
        work_dir = Path(tmp)
        with (
            mock.patch.object(benchmark_report, "ensure_server_running", return_value=True)
            as ensure,
            mock.patch.object(benchmark_report, "probe_session", return_value=session_result)
            as probe,
            mock.patch.object(benchmark_report, "stop_server"),
        ):
            result = benchmark_report.run_copy(
                1,
                work_dir,
                4096,
                "task",
                "glm-5.1",
                "zai-coding-plan",
                "build",
                1000,
                output_token_max=65_536,
                first_action_timeout=360,
            )

    assert ensure.call_args.kwargs["output_token_max"] == 65_536
    assert probe.call_args.kwargs["first_action_timeout"] == 360
    assert result["finish_reason"] == "length"
    assert result["first_action_elapsed"] is None


class _PostResponse:
    status_code = 200
    text = ""

    def __init__(self, payload: dict) -> None:
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _PostClient:
    """POST /session/.../message возвращает HTTP 200 с info.error."""

    def post(self, path: str, json: dict | None = None,
             timeout: float | None = None) -> _PostResponse:
        assert path == "/session/ses_test/message"
        return _PostResponse({
            "info": {
                "role": "assistant",
                "error": {"name": "MessageOutputLengthError",
                          "data": {"statusCode": 500}},
                "tokens": {"input": 9_814, "output": 29, "reasoning": 31_971},
            },
        })


def test_post_info_error_output_length_is_classified() -> None:
    # issue #161 / cycle-2: POST /message может вернуть HTTP 200 с info.error =
    # MessageOutputLengthError. _post_task НЕ должен отдавать generic provider
    # error — он должен распознать сигнал и вернуть OUTPUT_LENGTH_REASON с
    # finish_reason="length". (Раньше POST-путь ранний-возвращался до
    # _classify_outcome.)
    _usage, result, post_hung = opencode_session._post_task(
        _PostClient(),  # type: ignore[arg-type]
        "ses_test",
        "build",
        {"contents": "task"},
        None,
        lambda _msg: None,
    )

    assert post_hung is False
    assert result is not None
    assert result.code == 2
    assert result.finish_reason == "length"
    assert result.reason is not None
    assert result.reason.startswith("лимит ответа OpenCode исчерпан")
    assert result.usage is not None
    assert result.usage.output_tokens + result.usage.reasoning_tokens == 32_000

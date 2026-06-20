"""Регресс-тест бага B1.

Ретраябельный лимит провайдера (HTTP 429), записанный opencode в лог БЕЗ
токена ``agent=``, проскакивает мимо in-loop детекта лимита
(``provider_limit_tail`` зовёт ``_opencode_error_tail`` только с ``agent=``),
прогон доходит до дедлайна, и таймаут-ветка ``_probe_session_once`` строит
``SessionProbeResult(1, ...)`` БЕЗ проверки ``_is_retryable_limit_error`` на
дописанном provider-tail и БЕЗ ``rate_limited`` — единственная среди всех
error-веток, которые выставляют ``rate_limited``.

Итог на текущем (багованном) коде: исход — code=1 (обычный таймаут),
``rate_limited=False``, ``probe_session`` НЕ ретраит, backoff-пауз нет.

Ожидаемое (правильное) поведение: реальный 429 в provider-tail распознаётся
как ретраябельный лимит, ``probe_session`` ретраит с backoff [5, 10, 20, 40]
и в итоге отдаёт отдельный статус «лимит» (code=3).
"""

import contextlib
import unittest
from unittest import mock

import opencode_runtime as runtime
import opencode_session
from test_bench import FakeHttpClient as _BaseHttpClient, FakeResponse, QuietSSE


class FakeHttpClient(_BaseHttpClient):
    """POST /message виснет по ReadTimeout — для теста таймаут-ветки B1.

    FakeResponse/QuietSSE и базовый клиент берём из test_bench (issue #54 #9,
    раньше форк), переопределяем только POST, чтобы /message висел до дедлайна."""

    def post(self, path, json=None, timeout=None):
        if path == "/session":
            return FakeResponse({"id": "ses_test"})
        if path == "/session/ses_test/message":
            raise runtime.httpx.ReadTimeout("stream did not finish")
        raise AssertionError(path)


def _backoff_sleeps(sleeps):
    """Только паузы retry-backoff: отбрасываем паузы инициализации SSE-reader."""
    return [s for s in sleeps if s != runtime.SSE_READER_STARTUP_DELAY]


class FixB1Tests(unittest.TestCase):
    def _probe(self, *, tail, sleeps, write=None, looks_idle=None):
        def connect(*a, **k):
            return QuietSSE()

        idle = looks_idle if looks_idle is not None else (lambda *a, **k: False)
        with contextlib.ExitStack() as stack:
            stack.enter_context(
                mock.patch.object(runtime.httpx, "Client", FakeHttpClient))
            stack.enter_context(
                mock.patch.object(opencode_session.httpx_sse, "connect_sse", connect))
            stack.enter_context(
                mock.patch.object(opencode_session, "_session_looks_idle", idle))
            stack.enter_context(
                mock.patch.object(opencode_session, "_opencode_error_tail", tail))
            stack.enter_context(
                mock.patch.object(runtime.time, "sleep", sleeps.append))
            return runtime.probe_session(
                task="ping", model="minimax-m2.1", provider="ollama-cloud",
                agent="bench_coder", timeout=0.4, port=4096,
                write=write if write is not None else (lambda msg: None),
            )

    def test_deadline_429_without_agent_token_is_retryable_limit(self):
        # 429 записан opencode в лог БЕЗ токена agent=:
        #   - detect с agent='bench_coder' (in-loop) -> None (строка не матчится),
        #   - fallback с agent=None -> отдаёт 429-tail.
        # На текущем коде это всплывает как обычный таймаут (code=1) без ретрая.
        def tail(session_id, lines=8, *, agent=None):
            if agent is None:
                return ("HTTP 429 | AI_APICallError | "
                        "you have reached your weekly usage limit")
            return None

        sleeps = []
        messages = []
        result = self._probe(tail=tail, sleeps=sleeps, write=messages.append)

        # Правильное поведение: распознан ретраябельный лимит -> code=3 «лимит».
        self.assertEqual(result.code, 3)
        self.assertIn("weekly usage limit", result.reason or "")
        # 5 попыток -> 4 backoff-паузы 5, 10, 20, 40 (без пауз инициализации reader).
        self.assertEqual(_backoff_sleeps(sleeps), [5.0, 10.0, 20.0, 40.0])


if __name__ == "__main__":
    unittest.main()

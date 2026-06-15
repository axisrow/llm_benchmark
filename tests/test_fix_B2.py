"""Регресс-тест бага B2.

Стартовая пауза SSE-reader (SSE_READER_STARTUP_DELAY) не должна вычитаться из
бюджета задачи: при коротком timeout (< паузы) POST /session/<id>/message с
задачей всё равно обязан уйти агенту. На баговом коде дедлайн истекал ещё во
время стартовой паузы, _message_post_timeout возвращал 0, гейт `post_timeout > 0`
был ложен — задача вообще не отправлялась, а прогон репортил ложный таймаут
(code=1, «нет ответа за …») по неотправленной задаче.
"""

import contextlib
import unittest
from unittest import mock

# FakeResponse/QuietSSE — вспомогательные классы (не TestCase), их импорт не
# приводит к повторному сбору тестов pytest-ом. Тест-класс из test_bench НЕ
# наследуем, чтобы не дублировать его тесты в этом модуле.
from test_bench import FakeResponse, QuietSSE

import opencode_runtime as runtime


class FixB2StartupDelayBudgetTests(unittest.TestCase):
    def _probe(self, *, client, tail, looks_idle, timeout):
        """probe_session с подменой runtime-атрибутов (sleeps НЕ мокаем —
        стартовая пауза должна быть реальной)."""
        connect = lambda *a, **k: QuietSSE()  # noqa: E731
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(runtime.httpx, "Client", client))
            stack.enter_context(mock.patch.object(
                runtime.httpx_sse, "connect_sse", connect))
            stack.enter_context(mock.patch.object(
                runtime, "_session_looks_idle", looks_idle))
            stack.enter_context(mock.patch.object(
                runtime, "_opencode_error_tail", tail))
            return runtime.probe_session(
                task="ping", model="m", provider="p", agent="bench_coder",
                timeout=timeout, port=4096, write=lambda msg: None)

    def test_short_timeout_still_posts_task(self):
        # timeout (0.2с) меньше стартовой паузы (0.3с). Стартовая пауза реальная
        # (sleeps не мокаем), SSE пустой, сессия не выглядит idle, лога ошибок
        # провайдера нет — то есть единственная честная развязка возможна только
        # если задача доставлена POST /session/<id>/message.
        self.assertLess(runtime.SSE_READER_STARTUP_DELAY, 0.3 + 1e-9)
        self.assertGreater(runtime.SSE_READER_STARTUP_DELAY, 0.2)

        posted_paths: list[str] = []

        class RecordingHttpClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, path, json=None, timeout=None):
                posted_paths.append(path)
                if path == "/session":
                    return FakeResponse({"id": "ses_test"})
                if path == "/session/ses_test/message":
                    return FakeResponse({"info": {}})
                raise AssertionError(path)

            def get(self, path, timeout=None):
                raise AssertionError(path)

        self._probe(
            client=RecordingHttpClient,
            tail=lambda session_id, **kwargs: None,
            looks_idle=lambda *a, **k: False,
            timeout=0.2,
        )

        # Главное ожидание правильного поведения: задача реально отправлена.
        self.assertIn(
            "/session/ses_test/message",
            posted_paths,
            "POST с задачей не был отправлен — стартовая пауза «съела» бюджет",
        )


if __name__ == "__main__":
    unittest.main()

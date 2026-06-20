"""Тест follow-up #54 #7: выделенные из _probe_session_once хелперы poll-loop.

_exit_state и _wait_for_session извлечены из _probe_session_once. Проверяем их
контракт напрямую: приоритет error над idle, обнаружение лимита провайдера,
дедлайн и повторную проверку error/idle после I/O чтения лога.
"""

import threading
import unittest

import opencode_runtime as runtime


class ExitStateTests(unittest.TestCase):
    def test_error_has_priority_over_idle(self):
        done = threading.Event()
        done.set()
        self.assertEqual(runtime._exit_state({"error": "boom"}, done), "error")

    def test_idle_when_done(self):
        done = threading.Event()
        done.set()
        self.assertEqual(runtime._exit_state({}, done), "idle")

    def test_none_when_running(self):
        self.assertIsNone(runtime._exit_state({}, threading.Event()))


class WaitForSessionTests(unittest.TestCase):
    def _no_tail(self):
        return None

    def test_error_outcome(self):
        outcome, tail = runtime._wait_for_session(
            threading.Event(), {"error": "boom"}, None, self._no_tail)
        self.assertEqual((outcome, tail), ("error", None))

    def test_idle_outcome(self):
        done = threading.Event()
        done.set()
        outcome, tail = runtime._wait_for_session(done, {}, None, self._no_tail)
        self.assertEqual((outcome, tail), ("idle", None))

    def test_limit_outcome(self):
        outcome, tail = runtime._wait_for_session(
            threading.Event(), {}, None, lambda: "AI_APICallError | 429 limit")
        self.assertEqual(outcome, "limit")
        self.assertIn("429", tail)

    def test_deadline_outcome(self):
        # Дедлайн в прошлом → 'deadline' без ожидания.
        past = runtime.time.monotonic() - 1.0
        outcome, tail = runtime._wait_for_session(
            threading.Event(), {}, past, self._no_tail)
        self.assertEqual((outcome, tail), ("deadline", None))

    def test_error_set_during_log_read_wins_over_limit(self):
        # Ошибка reader'а появилась во время чтения лога (внутри provider_limit_tail):
        # повторная _exit_state-проверка должна вернуть 'error', а не 'limit'.
        result: dict = {}

        def tail_that_sets_error():
            result["error"] = "boom during read"
            return "AI_APICallError | 429 limit"

        outcome, tail = runtime._wait_for_session(
            threading.Event(), result, None, tail_that_sets_error)
        self.assertEqual((outcome, tail), ("error", None))


if __name__ == "__main__":
    unittest.main()

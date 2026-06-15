"""Регресс-тест B10: ретрай фазы 2 затирал лог фазы 1.

Баг: `check_one` всегда писал лог в `<key>.log` через open("w") (truncate).
Фаза 2 (ретрай таймаутнувших) переиспользовала тот же ref/log_dir и
труновала лог фазы 1. Когда вердикт фазы 1 оставался лучшим (например
phase1 timeout < phase2 error), `availability.json` ссылался на лог,
содержавший контент фазы 2 — противоречие status/attempt_timeout/reason.

Правильное поведение: лог выбранного вердикта должен соответствовать ИМЕННО
той фазе, чей CheckResult оставлен.
"""

import tempfile
import unittest
from pathlib import Path

import check_models
from opencode_runtime import SessionProbeResult, sanitize_name


class FixB10RetryLogTests(unittest.TestCase):
    def test_retry_keeps_log_consistent_with_chosen_verdict(self) -> None:
        base_timeout = 20.0
        retry_timeout = 120.0
        ref = check_models.ModelRef(provider="prov", model="mdl")

        def fake_probe_session(*, task, model, provider, agent, timeout, port, write):
            # Фаза 1 (базовый таймаут): таймаут (code=1) — кандидат на ретрай.
            if timeout == base_timeout:
                write("PHASE1\n")
                return SessionProbeResult(code=1, reason="phase1 timeout")
            # Фаза 2 (retry-таймаут): ошибка (code=2) — ХУЖЕ фазы 1.
            write("PHASE2\n")
            return SessionProbeResult(code=2, reason="phase2 error")

        original = check_models.probe_session
        check_models.probe_session = fake_probe_session
        try:
            with tempfile.TemporaryDirectory() as td:
                run_dir = Path(td)
                log_dir = run_dir / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)

                results = check_models.check_models(
                    refs=[ref], prompt="ping", agent="bench_coder",
                    base_timeout=base_timeout, retry_timeout=retry_timeout,
                    do_retry=True, port=4096, log_dir=log_dir, run_dir=run_dir,
                )

                self.assertEqual(len(results), 1)
                res = results[0]
                # Вердикт фазы 1 (timeout) лучше фазы 2 (error) — он и остаётся.
                self.assertEqual(res.status, "timeout")
                self.assertEqual(res.attempt_timeout, base_timeout)

                # Лог, на который указывает выбранный вердикт, должен содержать
                # контент ИМЕННО фазы 1, а не затёртый фазой 2.
                chosen_log = run_dir / res.log_path
                content = chosen_log.read_text(encoding="utf-8")
                self.assertIn("PHASE1", content)
                self.assertIn("timeout=20s", content)
                self.assertNotIn("PHASE2", content)
                self.assertNotIn("timeout=120s", content)
        finally:
            check_models.probe_session = original

    def test_retry_better_verdict_points_to_phase2_log(self) -> None:
        # Симметрия: когда фаза 2 ЛУЧШЕ (available), вердикт и лог — фазы 2.
        base_timeout = 20.0
        retry_timeout = 120.0
        ref = check_models.ModelRef(provider="prov", model="mdl")

        def fake_probe_session(*, task, model, provider, agent, timeout, port, write):
            if timeout == base_timeout:
                write("PHASE1\n")
                return SessionProbeResult(code=1, reason="phase1 timeout")
            write("PHASE2\n")
            return SessionProbeResult(code=0, reason=None)

        original = check_models.probe_session
        check_models.probe_session = fake_probe_session
        try:
            with tempfile.TemporaryDirectory() as td:
                run_dir = Path(td)
                log_dir = run_dir / "logs"
                log_dir.mkdir(parents=True, exist_ok=True)

                results = check_models.check_models(
                    refs=[ref], prompt="ping", agent="bench_coder",
                    base_timeout=base_timeout, retry_timeout=retry_timeout,
                    do_retry=True, port=4096, log_dir=log_dir, run_dir=run_dir,
                )

                res = results[0]
                self.assertEqual(res.status, "available")
                self.assertEqual(res.attempt_timeout, retry_timeout)

                chosen_log = run_dir / res.log_path
                content = chosen_log.read_text(encoding="utf-8")
                self.assertIn("PHASE2", content)
                self.assertIn("timeout=120s", content)

        finally:
            check_models.probe_session = original

    def test_sanitize_key_used_for_log_name(self) -> None:
        # Базовая инвариант-проверка: лог фазы 1 именуется по sanitize_name(key).
        ref = check_models.ModelRef(provider="prov", model="a/b")
        self.assertEqual(sanitize_name(ref.key), sanitize_name("prov/a/b"))


if __name__ == "__main__":
    unittest.main()

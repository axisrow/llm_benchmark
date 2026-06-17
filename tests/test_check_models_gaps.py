"""Покрытие непокрытых функций check_models.py (issue #38 P0).

Полностью офлайн: ни сети, ни реального opencode serve, ни боевой data/main.db.
Все вызовы к runtime (probe_session / ensure_server_running / install_shutdown_handlers)
и оркестрация (check_one / check_models / resolve_model_list) замоканы через
mock.patch на уровне модуля check_models — так как check_one зовёт
check_models.probe_session, а check_models зовёт check_models.check_one.
"""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import check_models
from opencode_runtime import SessionProbeResult


def make_ref(provider="prov", model="mdl", free_status="free", name=None):
    return check_models.ModelRef(
        provider=provider, model=model, free_status=free_status, name=name)


def make_result(ref, code=0, status="available", reason=None, retried=False):
    return check_models.CheckResult(
        ref=ref, code=code, status=status, reason=reason, elapsed=0.01,
        attempt_timeout=1.0, retried=retried, log_path="x.log")


# --- tally_statuses --------------------------------------------------------

class TallyStatusesTests(unittest.TestCase):
    def test_counts_mix_of_known_statuses(self):
        ref = make_ref()
        results = [
            make_result(ref, code=0, status="available"),
            make_result(ref, code=0, status="available"),
            make_result(ref, code=1, status="timeout"),
            make_result(ref, code=2, status="error"),
            make_result(ref, code=3, status="rate_limited"),
        ]

        counts = check_models.tally_statuses(results)

        self.assertEqual(counts["available"], 2)
        self.assertEqual(counts["timeout"], 1)
        self.assertEqual(counts["error"], 1)
        self.assertEqual(counts["rate_limited"], 1)

    def test_empty_results_gives_zeroed_taxonomy(self):
        counts = check_models.tally_statuses([])
        # Все известные статусы присутствуют и обнулены.
        self.assertEqual(set(counts), {"available", "timeout", "error", "rate_limited"})
        self.assertTrue(all(v == 0 for v in counts.values()))

    def test_out_of_taxonomy_status_does_not_crash(self):
        # check_one подставляет "code-N" для неизвестного code — tally не должен
        # падать KeyError-ом (counts.get(..., 0) + 1).
        ref = make_ref()
        results = [
            make_result(ref, code=0, status="available"),
            make_result(ref, code=7, status="code-7"),
            make_result(ref, code=7, status="code-7"),
        ]

        counts = check_models.tally_statuses(results)

        self.assertEqual(counts["available"], 1)
        self.assertEqual(counts["code-7"], 2)
        # Базовая таксономия по-прежнему на месте.
        self.assertEqual(counts["timeout"], 0)


# --- check_one -------------------------------------------------------------

class CheckOneTests(unittest.TestCase):
    def _run_check_one(self, probe_impl, timeout=5.0, suffix=""):
        ref = make_ref(provider="zai", model="glm")
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            log_dir = run_dir / "logs"
            log_dir.mkdir()
            with mock.patch.object(check_models, "probe_session", probe_impl):
                res = check_models.check_one(
                    ref, prompt="ping", agent="bench_coder", timeout=timeout,
                    port=4096, log_dir=log_dir, run_dir=run_dir, log_suffix=suffix)
            # Лог реально создан и относительный путь корректен.
            log_file = run_dir / res.log_path
            self.assertTrue(log_file.exists())
            log_text = log_file.read_text(encoding="utf-8")
        return res, log_text

    def test_successful_probe_is_available_code_0(self):
        def probe(*, task, model, provider, agent, timeout, port, write):
            write("ok\n")
            return SessionProbeResult(0, None, None)

        res, log_text = self._run_check_one(probe)

        self.assertEqual(res.code, 0)
        self.assertEqual(res.status, "available")
        self.assertIsNone(res.reason)
        self.assertFalse(res.retried)
        self.assertEqual(res.attempt_timeout, 5.0)
        self.assertIn("zai/glm", log_text)

    def test_timeout_probe_is_timeout_code_1(self):
        def probe(*, task, model, provider, agent, timeout, port, write):
            return SessionProbeResult(1, "no answer in time", None)

        res, _ = self._run_check_one(probe)

        self.assertEqual(res.code, 1)
        self.assertEqual(res.status, "timeout")
        self.assertEqual(res.reason, "no answer in time")

    def test_provider_error_is_error_code_2(self):
        def probe(*, task, model, provider, agent, timeout, port, write):
            return SessionProbeResult(2, "401 Forbidden", None)

        res, _ = self._run_check_one(probe)

        self.assertEqual(res.code, 2)
        self.assertEqual(res.status, "error")
        self.assertEqual(res.reason, "401 Forbidden")

    def test_rate_limited_probe_is_rate_limited_code_3(self):
        def probe(*, task, model, provider, agent, timeout, port, write):
            return SessionProbeResult(3, "429 rate limit", None)

        res, _ = self._run_check_one(probe)

        self.assertEqual(res.code, 3)
        self.assertEqual(res.status, "rate_limited")

    def test_probe_crash_is_caught_and_marked_error(self):
        # Краш probe_session не должен ронять прогон: check_one ловит и помечает
        # модель как error (code 2), пишет трейс причины в лог.
        def probe(*, task, model, provider, agent, timeout, port, write):
            raise RuntimeError("boom")

        res, log_text = self._run_check_one(probe)

        self.assertEqual(res.code, 2)
        self.assertEqual(res.status, "error")
        self.assertIn("RuntimeError", res.reason)
        self.assertIn("boom", res.reason)
        self.assertIn("сбой проверки", log_text)

    def test_log_suffix_namespaces_log_file(self):
        def probe(*, task, model, provider, agent, timeout, port, write):
            return SessionProbeResult(0, None, None)

        res, _ = self._run_check_one(probe, suffix=".retry")

        self.assertTrue(res.log_path.endswith(".retry.log"))


# --- check_models (оркестрация фаз) ----------------------------------------

class CheckModelsTests(unittest.TestCase):
    def _orchestrate(self, refs, scripted, do_retry=True):
        """Патчит check_one: scripted — dict (key, suffix-flag) -> CheckResult-фабрика.

        scripted[key] = list результатов по последовательным вызовам для key
        (фаза1, затем фаза2). check_one дёргается последовательно (_run_phase).
        """
        calls = {r.key: 0 for r in refs}

        def fake_check_one(ref, prompt, agent, timeout, port, log_dir, run_dir,
                           log_suffix=""):
            idx = calls[ref.key]
            calls[ref.key] += 1
            return scripted[ref.key][idx]

        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td)
            with mock.patch.object(check_models, "check_one", fake_check_one):
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    out = check_models.check_models(
                        refs=refs, prompt="ping", agent="a",
                        base_timeout=10.0, retry_timeout=60.0, do_retry=do_retry,
                        port=4096, log_dir=run_dir, run_dir=run_dir)
        return out, calls

    def test_aggregates_results_in_ref_order(self):
        a = make_ref(model="a")
        b = make_ref(model="b")
        scripted = {
            "prov/a": [make_result(a, code=0, status="available")],
            "prov/b": [make_result(b, code=2, status="error")],
        }
        out, calls = self._orchestrate([a, b], scripted, do_retry=False)

        self.assertEqual([r.ref.key for r in out], ["prov/a", "prov/b"])
        self.assertEqual([r.status for r in out], ["available", "error"])
        # Без ретрая каждая модель проверена ровно один раз.
        self.assertEqual(calls["prov/a"], 1)
        self.assertEqual(calls["prov/b"], 1)

    def test_retry_phase_only_for_timeouts_and_keeps_better_code(self):
        a = make_ref(model="a")   # available сразу -> не ретраится
        b = make_ref(model="b")   # timeout -> ретрай -> available
        c = make_ref(model="c")   # timeout -> ретрай -> снова timeout
        scripted = {
            "prov/a": [make_result(a, code=0, status="available")],
            "prov/b": [
                make_result(b, code=1, status="timeout"),
                make_result(b, code=0, status="available"),
            ],
            "prov/c": [
                make_result(c, code=1, status="timeout"),
                make_result(c, code=1, status="timeout"),
            ],
        }
        out, calls = self._orchestrate([a, b, c], scripted, do_retry=True)
        by_key = {r.ref.key: r for r in out}

        # a — ровно одна проверка (не таймаутила).
        self.assertEqual(calls["prov/a"], 1)
        # b и c таймаутили -> по две проверки.
        self.assertEqual(calls["prov/b"], 2)
        self.assertEqual(calls["prov/c"], 2)

        # b: лучший из двух (0 < 1) -> available, помечен retried.
        self.assertEqual(by_key["prov/b"].status, "available")
        self.assertTrue(by_key["prov/b"].retried)
        # c: остался timeout, но помечен retried.
        self.assertEqual(by_key["prov/c"].status, "timeout")
        self.assertTrue(by_key["prov/c"].retried)

    def test_no_retry_skips_phase_two(self):
        b = make_ref(model="b")
        scripted = {
            "prov/b": [make_result(b, code=1, status="timeout")],
        }
        out, calls = self._orchestrate([b], scripted, do_retry=False)

        self.assertEqual(calls["prov/b"], 1)
        self.assertEqual(out[0].status, "timeout")
        self.assertFalse(out[0].retried)


# --- main() (CLI entry) ----------------------------------------------------

class MainCliTests(unittest.TestCase):
    def _run_main(self, argv, refs, source="opencode-models", full_refs=None,
                  exclusions=None, server_ok=True, check_results=None):
        full_refs = refs if full_refs is None else full_refs
        captured = {}

        def fake_resolve(args):
            captured["args"] = args
            return list(refs), source, list(full_refs)

        def fake_filter(in_refs):
            skipped = exclusions or []
            allowed = [r for r in in_refs
                       if r.key not in {x[0].key for x in skipped}]
            return allowed, list(skipped)

        def fake_check_models(**kwargs):
            captured["check_kwargs"] = kwargs
            return check_results if check_results is not None else [
                make_result(r) for r in kwargs["refs"]]

        write_calls = {}

        def fake_write(results, path, meta):
            write_calls["path"] = path
            write_calls["meta"] = meta
            write_calls["n"] = len(results)

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(check_models, "AVAILABILITY_ROOT", Path(td)), \
                 mock.patch.object(check_models.sys, "argv", argv), \
                 mock.patch.object(check_models, "resolve_model_list", fake_resolve), \
                 mock.patch.object(check_models, "filter_excluded_models", fake_filter), \
                 mock.patch.object(check_models, "install_shutdown_handlers",
                                   lambda: None), \
                 mock.patch.object(check_models, "ensure_server_running",
                                   lambda run_dir, port, status: server_ok), \
                 mock.patch.object(check_models, "check_models", fake_check_models), \
                 mock.patch.object(check_models, "write_availability_json",
                                   fake_write):
                buf = io.StringIO()
                err = io.StringIO()
                code = None
                try:
                    with contextlib.redirect_stdout(buf), \
                         contextlib.redirect_stderr(err):
                        check_models.main()
                except SystemExit as exc:
                    code = exc.code
        return SimpleNamespace(
            stdout=buf.getvalue(), stderr=err.getvalue(), exit_code=code,
            captured=captured, write_calls=write_calls)

    def test_list_models_returns_without_starting_server(self):
        # --list-models не должен трогать сервер/check_models — ensure_server_running
        # не замокан на «ронять», но и не должен вызываться (иначе AssertionError).
        refs = [make_ref()]

        def boom_server(run_dir, port, status):
            raise AssertionError("server must not start in --list-models")

        with mock.patch.object(check_models, "ensure_server_running", boom_server):
            out = self._run_main(
                ["check_models.py", "--list-models"], refs)

        self.assertIsNone(out.exit_code)
        self.assertIn("Моделей:", out.stdout)

    def test_happy_path_wires_args_and_writes_report(self):
        refs = [make_ref(model="a"), make_ref(model="b")]
        out = self._run_main(
            ["check_models.py", "--timeout", "7", "--retry-timeout", "33",
             "--base-port", "5050", "--no-retry"],
            refs)

        self.assertIsNone(out.exit_code)
        # args провязаны в check_models.
        ck = out.captured["check_kwargs"]
        self.assertEqual(ck["base_timeout"], 7.0)
        self.assertEqual(ck["retry_timeout"], 33.0)
        self.assertEqual(ck["port"], 5050)
        self.assertFalse(ck["do_retry"])  # --no-retry
        self.assertEqual([r.key for r in ck["refs"]], ["prov/a", "prov/b"])
        # Отчёт записан, мета содержит источник и порт.
        self.assertEqual(out.write_calls["n"], 2)
        self.assertEqual(out.write_calls["meta"]["base_port"], 5050)
        self.assertIn("сводка", out.stdout)

    def test_empty_refs_exits_1(self):
        out = self._run_main(["check_models.py"], refs=[])

        self.assertEqual(out.exit_code, 1)
        self.assertIn("Нет моделей", out.stderr)

    def test_server_start_failure_exits_2(self):
        refs = [make_ref()]
        out = self._run_main(["check_models.py"], refs, server_ok=False)

        self.assertEqual(out.exit_code, 2)
        self.assertIn("Не удалось поднять", out.stderr)

    def test_catalog_error_exits_2(self):
        def raising_resolve(args):
            raise check_models.ModelCatalogError("opencode models failed")

        with mock.patch.object(check_models.sys, "argv", ["check_models.py"]), \
             mock.patch.object(check_models, "resolve_model_list", raising_resolve):
            err = io.StringIO()
            code = None
            try:
                with contextlib.redirect_stdout(io.StringIO()), \
                     contextlib.redirect_stderr(err):
                    check_models.main()
            except SystemExit as exc:
                code = exc.code

        self.assertEqual(code, 2)
        self.assertIn("Не удалось получить список моделей", err.getvalue())


if __name__ == "__main__":
    unittest.main()

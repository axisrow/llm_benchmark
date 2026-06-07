import argparse
import builtins
import contextlib
import hashlib
import io
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock
from pathlib import Path

import artifacts
import bench
import benchmark_report
import check_models
import dashboard_server
import db
import index_builder
import model_catalog
import opencode_runtime as runtime
import pricing
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


class QuietSSE:
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_sse(self):
        return iter(())


class IdleSSE:
    """SSE-стрим, сразу отдающий session.idle для ses_test."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_sse(self):
        yield SimpleNamespace(
            data=json.dumps({"type": "session.idle", "sessionID": "ses_test"}))


class ScriptedSSE:
    """connect_sse-мок: отдаёт по стриму из очереди на каждое подключение.

    Reader теперь зовёт connect_sse многократно (реконнект), поэтому нужно
    моделировать разные стримы по очереди; дальше — пустой QuietSSE.
    """

    def __init__(self, streams):
        self._streams = list(streams)

    def __call__(self, *args, **kwargs):
        if self._streams:
            return self._streams.pop(0)()
        return QuietSSE()


class FakeProcess:
    def __init__(self, running: bool = True):
        self.returncode = None if running else 0
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self):
        return self.returncode

    def terminate(self):
        self.terminated = True
        self.returncode = 0

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        self.wait_calls += 1
        self.returncode = 0 if self.returncode is None else self.returncode
        return self.returncode


class FakeNamedTemp:
    def __init__(self, path: Path):
        self.name = str(path)
        self.closed = False
        path.write_text("", encoding="utf-8")

    def close(self):
        self.closed = True


def backoff_sleeps(sleeps):
    """Только паузы retry-backoff: отбрасываем паузы инициализации SSE-reader."""
    return [s for s in sleeps if s != runtime.SSE_READER_STARTUP_DELAY]


class BenchCriticalBugTests(unittest.TestCase):
    def _probe_session(self, *, client, sse=None, sse_factory=None, tail=None,
                       sleeps=None, write=None, looks_idle=None, timeout=0.2,
                       model="some-model", provider="some-provider"):
        """probe_session с подменой runtime-атрибутов (авто-восстановление).

        Подменяет httpx.Client/SSE/лог-tail/time.sleep на время вызова —
        без ручного orig_*/try/finally в каждом тесте.

        `sse` — фабрика одного стрима (повторяется на каждый реконнект);
        `sse_factory` — готовый callable (например ScriptedSSE([...])) для
        последовательных разных стримов; `looks_idle` подменяет
        _session_looks_idle.
        """
        connect = sse_factory or (lambda *a, **k: (sse() if sse else QuietSSE()))
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(runtime.httpx, "Client", client))
            stack.enter_context(mock.patch.object(
                runtime.httpx_sse, "connect_sse", connect))
            if looks_idle is not None:
                stack.enter_context(mock.patch.object(
                    runtime, "_session_looks_idle", looks_idle))
            if tail is not None:
                stack.enter_context(mock.patch.object(
                    runtime, "_opencode_error_tail", tail))
            if sleeps is not None:
                stack.enter_context(mock.patch.object(
                    runtime.time, "sleep", sleeps.append))
            return runtime.probe_session(
                task="ping", model=model, provider=provider, agent="bench_coder",
                timeout=timeout, port=4096,
                write=write if write is not None else (lambda msg: None),
            )

    def test_regenerate_raw_json_filters_runs_to_table(self):
        # После ручного удаления плохого прогона из таблицы runs регенерация
        # должна привести raw_json в соответствие: убрать его из runs[],
        # пересчитать summary/usage_summary/copies. И быть идемпотентной.
        import scripts.regenerate_raw_json as regen

        def make_run(i, code, status, with_usage=True):
            return {
                "index": i, "port": 4000 + i, "dir": f"/x/{i}",
                "status": status, "code": code, "elapsed": 10.0 + i,
                "usage": ({
                    "input_tokens": 100, "output_tokens": 10,
                    "reasoning_tokens": 0, "cache_read_tokens": 0,
                    "cache_write_tokens": 0, "total_tokens": 110,
                    "estimated_prompt_cost_usd": 0.001,
                    "estimated_completion_cost_usd": 0.0002,
                    "estimated_cost_usd": 0.0012, "opencode_cost_usd": None,
                } if with_usage else None),
            }

        report = {
            "project": "p", "model": "m", "provider": "prov", "prompt": "t",
            "description": None, "what_it_tests": None, "copies": 5,
            "started_at": "2026-01-01T00:00:00", "run_elapsed": 99.0,
            "summary": {"ok": 4, "timeout": 0, "error": 1},
            "pricing": {}, "artifact_summary": {"files": 0},
            "usage_summary": {"input_tokens": 400, "output_tokens": 40,
                              "reasoning_tokens": 0, "total_tokens": 440,
                              "estimated_cost_usd": 0.0048,
                              "runs_with_usage": 4, "runs_with_estimated_cost": 4},
            "runs": [make_run(1, 0, "готово"), make_run(2, 0, "готово"),
                     make_run(3, 2, "ошибка", with_usage=False),
                     make_run(4, 0, "готово"), make_run(5, 0, "готово")],
        }

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                with conn:
                    rid = db.upsert_report(conn, report,
                                           "data/result/r.json", json.dumps(report))
                    # Симулируем ручную чистку: убираем плохой прогон index=3
                    # ТОЛЬКО из таблицы runs (raw_json пока со старыми данными).
                    conn.execute("DELETE FROM runs WHERE report_id=? AND idx=3", (rid,))

                with conn:
                    changed = regen.run(conn, [rid])
                self.assertEqual(changed, 1)

                raw = json.loads(conn.execute(
                    "SELECT raw_json FROM reports WHERE id=?", (rid,)).fetchone()[0])
                # 1) В raw_json осталось 4 прогона, index=3 убран.
                self.assertEqual(len(raw["runs"]), 4)
                self.assertNotIn(3, [r["index"] for r in raw["runs"]])
                # 2) summary пересчитан: ошибки нет.
                self.assertEqual(raw["summary"], {"ok": 4, "timeout": 0, "error": 0})
                # 3) usage_summary переагрегирован по 4 оставшимся.
                self.assertEqual(raw["usage_summary"]["runs_with_usage"], 4)
                self.assertEqual(raw["usage_summary"]["input_tokens"], 400)
                # 4) copies синхронизирован, run_elapsed не тронут.
                self.assertEqual(raw["copies"], 4)
                self.assertEqual(raw["run_elapsed"], 99.0)
                # 5) summary_* колонки и таблица runs согласованы.
                cols = conn.execute(
                    "SELECT summary_ok, summary_timeout, summary_error, copies "
                    "FROM reports WHERE id=?", (rid,)).fetchone()
                self.assertEqual(tuple(cols), (4, 0, 0, 4))

                # 6) Идемпотентность: повторный прогон ничего не меняет.
                before = conn.execute(
                    "SELECT raw_json FROM reports WHERE id=?", (rid,)).fetchone()[0]
                with conn:
                    changed2 = regen.run(conn, [rid])
                after = conn.execute(
                    "SELECT raw_json FROM reports WHERE id=?", (rid,)).fetchone()[0]
                self.assertEqual(changed2, 0)
                self.assertEqual(before, after)
            finally:
                conn.close()

    def test_delete_report_cascades_runs_and_prunes_orphan_blobs(self):
        # delete_report сносит отчёт + каскадно runs/run_artifacts и подметает
        # блобы, на которые больше нет ссылок; общий блоб двух отчётов уцелевает.
        report = {
            "project": "p", "model": "m", "provider": "v", "prompt": "t",
            "description": None, "what_it_tests": None, "copies": 1,
            "started_at": "2026-01-01T00:00:00", "run_elapsed": 1.0,
            "summary": {"ok": 1, "timeout": 0, "error": 0}, "pricing": {},
            "usage_summary": {}, "artifact_summary": {},
            "runs": [{"index": 0, "port": 4000, "dir": "/x", "status": "готово",
                      "code": 0, "elapsed": 10.0, "usage": None}],
        }

        def art(path, content):
            blob = content.encode()
            return artifacts.RunArtifact(
                run_idx=0, path=path, kind="agent_file",
                size_bytes=len(blob), sha256=hashlib.sha256(blob).hexdigest(),
                content=blob, source_path=Path("/x"))

        shared = art("shared.txt", "shared")   # один и тот же sha в обоих отчётах
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                with conn:
                    rid_keep = db.upsert_report(
                        conn, dict(report, started_at="2026-01-01T00:00:00"),
                        "data/result/keep.json", json.dumps({"x": 1}),
                        artifacts=[shared])
                    rid_del = db.upsert_report(
                        conn, dict(report, started_at="2026-01-02T00:00:00"),
                        "data/result/del.json", json.dumps({"x": 2}),
                        artifacts=[shared, art("extra.txt", "only-in-del")])

                blobs_before = conn.execute(
                    "SELECT count(*) FROM file_blobs").fetchone()[0]
                self.assertEqual(blobs_before, 2)  # shared + only-in-del

                with conn:
                    deleted = db.delete_report(conn, rid_del)
                self.assertEqual(deleted, 1)

                # отчёт и его runs ушли каскадом
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM reports WHERE id=?", (rid_del,)).fetchone())
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM runs WHERE report_id=?",
                    (rid_del,)).fetchone()[0], 0)
                # осиротевший блоб подметён, общий (ещё нужен rid_keep) уцелел
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM file_blobs").fetchone()[0], 1)
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM file_blobs WHERE sha256=?",
                    (shared.sha256,)).fetchone()[0], 1)

                # удаление несуществующего отчёта — 0, без побочных эффектов
                with conn:
                    self.assertEqual(db.delete_report(conn, 99999), 0)
                self.assertIsNotNone(conn.execute(
                    "SELECT 1 FROM reports WHERE id=?", (rid_keep,)).fetchone())
            finally:
                conn.close()

    def test_cleanup_false_timeouts_removes_orphans_with_sqlite_delete_syntax(self):
        import scripts.cleanup_false_timeouts as cleanup

        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                blob = b"orphan artifact"
                sha = hashlib.sha256(blob).hexdigest()
                conn.execute("PRAGMA foreign_keys = OFF")
                conn.execute(
                    "INSERT INTO runs "
                    "(report_id, idx, port, dir, status, code, elapsed) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (999, 1, 4096, "/tmp/orphan", "таймаут", 1, 124.0),
                )
                conn.execute(
                    "INSERT INTO file_blobs "
                    "(sha256, size_bytes, content_encoding, content_blob) "
                    "VALUES (?, ?, ?, ?)",
                    (sha, len(blob), "zlib", blob),
                )
                conn.execute(
                    "INSERT INTO run_artifacts "
                    "(report_id, run_idx, path, kind, sha256) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (999, 1, "run.log", "log", sha),
                )
                conn.commit()
                conn.execute("PRAGMA foreign_keys = ON")
            finally:
                conn.close()

            orig_connect = cleanup.db.connect
            with mock.patch.object(cleanup.db, "connect",
                                   lambda: orig_connect(db_path)):
                with mock.patch.object(sys, "argv", ["cleanup_false_timeouts.py"]):
                    rc = cleanup.main()

            conn = db.connect(db_path)
            try:
                self.assertEqual(rc, 0)
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM runs").fetchone()[0], 0)
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM run_artifacts").fetchone()[0], 0)
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM file_blobs").fetchone()[0], 0)
            finally:
                conn.close()

    def test_restore_reports_from_git_initializes_fresh_target_schema(self):
        import scripts.restore_reports_from_git as restore

        report = {
            "project": "p", "model": "m", "provider": "v", "prompt": "t",
            "description": None, "what_it_tests": None, "copies": 1,
            "started_at": "2026-01-01T00:00:00", "run_elapsed": 1.0,
            "summary": {"ok": 1, "timeout": 0, "error": 0}, "pricing": {},
            "usage_summary": {}, "artifact_summary": {},
            "runs": [{"index": 0, "port": 4000, "dir": "/x", "status": "готово",
                      "code": 0, "elapsed": 10.0, "usage": None}],
        }

        with tempfile.TemporaryDirectory() as td:
            source_path = Path(td) / "source.db"
            target_path = Path(td) / "target.db"
            keys_path = Path(td) / "keys.txt"

            conn = db.connect(source_path)
            try:
                db.init_schema(conn)
                with conn:
                    db.upsert_report(
                        conn, report, "data/result/p/report.json",
                        json.dumps(report))
            finally:
                conn.close()

            keys_path.write_text(
                "p|v|m|2026-01-01T00:00:00\n", encoding="utf-8")

            orig_connect = restore.db.connect
            with mock.patch.object(restore.db, "connect",
                                   lambda: orig_connect(target_path)):
                with mock.patch.object(
                    sys, "argv",
                    [
                        "restore_reports_from_git.py",
                        "--source", str(source_path),
                        "--keys", str(keys_path),
                    ],
                ):
                    rc = restore.main()

            conn = db.connect(target_path)
            try:
                self.assertEqual(rc, 0)
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM reports").fetchone()[0], 1)
                self.assertEqual(
                    conn.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
            finally:
                conn.close()

    def _backfill_make_report(self, provider, model, project, ok, fail,
                              started_at, fail_code=1):
        """report-dict для мок-раннера: `ok` успешных + `fail` фейловых прогонов."""
        runs = []
        idx = 0
        for _ in range(ok):
            idx += 1
            runs.append({"index": idx, "port": 4000 + idx, "dir": f"/x/{idx}",
                         "status": "готово", "code": 0, "elapsed": 10.0,
                         "usage": None})
        for _ in range(fail):
            idx += 1
            runs.append({"index": idx, "port": 4000 + idx, "dir": f"/x/{idx}",
                         "status": "таймаут", "code": fail_code, "elapsed": 124.0,
                         "usage": None})
        return {
            "project": project, "model": model, "provider": provider,
            "prompt": "t", "description": None, "what_it_tests": None,
            "copies": ok + fail, "started_at": started_at, "run_elapsed": 1.0,
            "summary": {"ok": ok, "timeout": fail, "error": 0, "rate_limited": 0},
            "pricing": {}, "usage_summary": {}, "artifact_summary": {},
            "runs": runs,
        }

    def test_backfill_runner_fills_underfilled_cell(self):
        # Ячейка с 3 успешными прогонами добивается до 5: мок-раннер пишет новый
        # отчёт на 5 успехов, оркестратор видит 5 из базы и завершает успехом.
        import scripts.backfill_runs as backfill

        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                with conn:
                    db.upsert_report(
                        conn,
                        self._backfill_make_report("p", "m", "fast_sort", 3, 0,
                                                   "2026-01-01T00:00:00"),
                        "data/result/r0.json",
                        json.dumps({"x": 1}))

                seq = iter(["2026-01-02T00:00:00"])

                def runner(cell, *, n, **kwargs):
                    with conn:
                        db.upsert_report(
                            conn,
                            self._backfill_make_report(
                                cell["provider"], cell["model"], cell["project"],
                                5, 0, next(seq)),
                            "data/result/r1.json", json.dumps({"x": 2}))
                    return 0

                rc = backfill.run(conn, projects=("fast_sort",), target=5,
                                  runner=runner)
                self.assertEqual(rc, 0)
                self.assertEqual(backfill.latest_ok(conn, "p", "m", "fast_sort"), 5)
            finally:
                conn.close()

    def test_backfill_cleans_failures_and_retries(self):
        # Первая попытка даёт 3 успеха + 2 таймаута, вторая — 5 успехов. Итог: 5
        # успешных, недобитый отчёт первой попытки удалён (latest = чистый отчёт).
        import scripts.backfill_runs as backfill

        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                stamps = iter(["2026-01-02T00:00:00", "2026-01-03T00:00:00"])
                results = iter([(3, 2), (5, 0)])

                def runner(cell, *, n, **kwargs):
                    ok, fail = next(results)
                    with conn:
                        db.upsert_report(
                            conn,
                            self._backfill_make_report(
                                cell["provider"], cell["model"], cell["project"],
                                ok, fail, next(stamps)),
                            "data/result/r.json", json.dumps({"x": 1}))
                    return 0 if fail == 0 else 1

                cell = {"provider": "p", "model": "m", "project": "fast_sort",
                        "latest_ok": 0, "need": 5, "denylisted": False}
                outcome = backfill.backfill_cell(
                    conn, cell, target=5, max_attempts=3, timeout=1.0,
                    base_port=4096, agent=None, force_excluded=True, runner=runner)

                self.assertTrue(outcome["success"])
                self.assertEqual(outcome["final_ok"], 5)
                # latest-отчёт ровно один и без фейлов
                self.assertEqual(backfill.latest_ok(conn, "p", "m", "fast_sort"), 5)
                rid = backfill.latest_report_id(conn, "p", "m", "fast_sort")
                fails = conn.execute(
                    "SELECT COUNT(*) FROM runs WHERE report_id=? AND code<>0",
                    (rid,)).fetchone()[0]
                self.assertEqual(fails, 0)
            finally:
                conn.close()

    def test_backfill_gives_up_after_max_attempts(self):
        # Модель всегда фейлит (3 успеха из 5). После max_attempts оркестратор
        # сдаётся: outcome.success=False, не падает, возвращает код 1.
        import scripts.backfill_runs as backfill

        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                counter = {"n": 0}

                def runner(cell, *, n, **kwargs):
                    counter["n"] += 1
                    with conn:
                        db.upsert_report(
                            conn,
                            self._backfill_make_report(
                                cell["provider"], cell["model"], cell["project"],
                                3, 2, f"2026-02-{counter['n']:02d}T00:00:00"),
                            "data/result/r.json", json.dumps({"x": 1}))
                    return 1

                cell = {"provider": "p", "model": "m", "project": "stock_downloader",
                        "latest_ok": 0, "need": 5, "denylisted": True}
                outcome = backfill.backfill_cell(
                    conn, cell, target=5, max_attempts=3, timeout=1.0,
                    base_port=4096, agent=None, force_excluded=True, runner=runner)

                self.assertFalse(outcome["success"])
                self.assertEqual(outcome["attempts"], 3)
                self.assertEqual(counter["n"], 3)
                self.assertEqual(outcome["final_ok"], 3)
                self.assertTrue(outcome["denylisted"])
            finally:
                conn.close()

    def test_backfill_dry_run_writes_nothing(self):
        # --dry-run печатает матрицу, но раннер НЕ зовётся и база не меняется.
        import scripts.backfill_runs as backfill

        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                with conn:
                    db.upsert_report(
                        conn,
                        self._backfill_make_report("p", "m", "fast_sort", 2, 0,
                                                   "2026-01-01T00:00:00"),
                        "data/result/r.json", json.dumps({"x": 1}))
                before = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]

                called = {"n": 0}

                def runner(*a, **k):
                    called["n"] += 1
                    return 0

                rc = backfill.run(conn, projects=("fast_sort",), target=5,
                                  dry_run=True, runner=runner)
                self.assertEqual(rc, 0)
                self.assertEqual(called["n"], 0)
                after = conn.execute("SELECT COUNT(*) FROM reports").fetchone()[0]
                self.assertEqual(before, after)
            finally:
                conn.close()

    def _build_index_data(self, reports, exclusions=(), unstable=()):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                with conn:
                    for idx, report in enumerate(reports):
                        db.upsert_report(
                            conn,
                            report,
                            f"data/result/report_{idx}.json",
                            json.dumps(report),
                        )
                    for provider, model, reason in exclusions:
                        db.block_model_exclusion(conn, provider, model, reason)
                    for provider, model, reason in unstable:
                        db.mark_model_unstable(conn, provider, model, reason)
            finally:
                conn.close()

            original_connect = index_builder.connect
            original_project_root = index_builder.PROJECT_ROOT
            try:
                index_builder.connect = lambda: db.connect(db_path)
                index_builder.PROJECT_ROOT = root
                count = index_builder.build_index()
            finally:
                index_builder.connect = original_connect
                index_builder.PROJECT_ROOT = original_project_root

            data = json.loads((root / "docs" / "data" / "index.json").read_text())
        return count, data

    def test_sse_disconnect_is_error_not_success(self):
        # Перманентно битый SSE: reader реконнектит, но соединение каждый раз
        # рвётся; при истечении бюджета итог — ошибка (code=2), а НЕ зависание.
        sleeps = []
        result = self._probe_session(
            client=FakeHttpClient,
            sse=BrokenSSE,
            tail=lambda session_id, **kwargs: None,
            sleeps=sleeps,
            model="m", provider="p",
        )

        self.assertEqual(result.code, 2)
        self.assertIn("SSE reader error", result.reason or "")
        self.assertIsNone(result.usage)

    def test_sse_stream_closes_without_idle_then_reconnect_gets_idle(self):
        # Прямой регресс-тест на баг: 1-й стрим закрывается штатно без события
        # (graceful-close сервера), 2-й после реконнекта отдаёт session.idle.
        # _session_looks_idle → False, чтобы проверить именно реконнект.
        sleeps = []
        result = self._probe_session(
            client=FakeHttpClient,
            sse_factory=ScriptedSSE([QuietSSE, IdleSSE]),
            tail=lambda session_id, **kwargs: None,
            looks_idle=lambda *a, **k: False,
            sleeps=sleeps,
            timeout=5,
            model="m", provider="p",
        )

        self.assertEqual(result.code, 0)

    def test_sse_stream_closes_without_idle_session_already_idle(self):
        # Стрим закрылся без события, но сессия фактически уже завершилась
        # (idle случился в окне реконнекта) — verify ловит это → успех.
        messages = []
        result = self._probe_session(
            client=FakeHttpClient,
            sse=QuietSSE,
            tail=lambda session_id, **kwargs: None,
            looks_idle=lambda *a, **k: True,
            write=messages.append,
            timeout=5,
            model="m", provider="p",
        )

        self.assertEqual(result.code, 0)
        self.assertIn("сервер закрыл /event", "".join(messages))

    def test_sse_graceful_close_is_not_false_timeout(self):
        # Сервер всё время закрывает /event без события, сессия не завершается.
        # Исход — ЧЕСТНЫЙ таймаут по дедлайну (code=1), а не молчаливый: в логе
        # видна диагностика, которой раньше не было (баг был невидим).
        messages = []
        sleeps = []
        result = self._probe_session(
            client=FakeHttpClient,
            sse=QuietSSE,
            tail=lambda session_id, **kwargs: None,
            looks_idle=lambda *a, **k: False,
            write=messages.append,
            sleeps=sleeps,
            model="m", provider="p",
        )

        self.assertEqual(result.code, 1)
        self.assertIn("нет ответа за", result.reason or "")
        self.assertIn("сервер закрыл /event", "".join(messages))

    def test_probe_session_real_timeout_code_1(self):
        # Закрывает дыру в покрытии: реальный таймаут по дедлайну (code=1).
        # POST /message виснет (ReadTimeout), событий нет, лимита нет.
        class ReadTimeoutHttpClient(FakeHttpClient):
            def post(self, path, json=None, timeout=None):
                if path == "/session":
                    return FakeResponse({"id": "ses_test"})
                if path == "/session/ses_test/message":
                    raise runtime.httpx.ReadTimeout("stream did not finish")
                raise AssertionError(path)

        sleeps = []
        result = self._probe_session(
            client=ReadTimeoutHttpClient,
            sse=QuietSSE,
            tail=lambda session_id, **kwargs: None,
            looks_idle=lambda *a, **k: False,
            sleeps=sleeps,
            model="m", provider="p",
        )

        self.assertEqual(result.code, 1)
        self.assertIn("нет ответа за", result.reason or "")

    def test_sse_reconnect_stops_on_stop_flag(self):
        # Анти-busy-loop: после stop.set() (в finally _probe_session_once)
        # reader не зацикливается — probe_session возвращается, число
        # подключений к connect_sse конечно.
        factory = ScriptedSSE([QuietSSE])
        connects = []
        orig_call = factory.__call__

        def counting_call(*a, **k):
            connects.append(1)
            return orig_call(*a, **k)

        sleeps = []
        result = self._probe_session(
            client=FakeHttpClient,
            sse_factory=counting_call,
            tail=lambda session_id, **kwargs: None,
            looks_idle=lambda *a, **k: False,
            sleeps=sleeps,
            model="m", provider="p",
        )

        # Таймаут по дедлайну (сессия не завершилась), но без зависания.
        self.assertEqual(result.code, 1)
        # Реконнектил конечное число раз и корректно остановился.
        self.assertGreaterEqual(len(connects), 1)
        self.assertLess(len(connects), runtime.SSE_MAX_RECONNECTS)

    def test_session_looks_idle_reads_completed_assistant_message(self):
        # Фиксирует формат ответа GET /session/{id}/message и работу field():
        # завершённое assistant-сообщение (time.completed) -> True; иначе False.
        def make_client(messages):
            class C(FakeHttpClient):
                def get(self, path, timeout=None):
                    if path == "/session/ses_test/message":
                        return FakeResponse(messages)
                    raise AssertionError(path)
            return C

        cases = [
            # (сообщения, ожидаемый результат)
            ([{"info": {"role": "assistant", "time": {"completed": 123}}}], True),
            ([{"info": {"role": "assistant", "time": {}}}], False),
            ([{"info": {"role": "user", "time": {"completed": 1}}}], False),
            ([], False),
        ]
        for messages, expected in cases:
            with mock.patch.object(runtime.httpx, "Client", make_client(messages)):
                got = runtime._session_looks_idle(
                    "http://x", "ses_test", lambda msg: None)
            self.assertEqual(got, expected, messages)

    def test_run_copy_converts_session_crash_to_error_result(self):
        orig_ensure = benchmark_report.ensure_server_running
        orig_probe_session = benchmark_report.probe_session
        try:
            benchmark_report.ensure_server_running = lambda work_dir, port, status: True

            def crash(**kwargs):
                raise RuntimeError("simulated crash")

            benchmark_report.probe_session = crash
            with tempfile.TemporaryDirectory() as td:
                result = benchmark_report.run_copy(
                    index=1,
                    work_dir=Path(td),
                    port=4096,
                    task="task",
                    model="m",
                    provider="p",
                    agent="bench_coder",
                    timeout=1,
                )
                log_text = (Path(td) / "run.log").read_text(encoding="utf-8")
        finally:
            benchmark_report.ensure_server_running = orig_ensure
            benchmark_report.probe_session = orig_probe_session

        self.assertEqual(result["code"], 2)
        self.assertIn("simulated crash", log_text)

    def test_run_copy_converts_startup_probe_crash_to_error_result(self):
        orig_ensure = benchmark_report.ensure_server_running
        try:
            def crash(work_dir, port, status):
                raise RuntimeError("startup probe crashed")

            benchmark_report.ensure_server_running = crash
            with tempfile.TemporaryDirectory() as td:
                result = benchmark_report.run_copy(
                    index=1,
                    work_dir=Path(td),
                    port=4096,
                    task="task",
                    model="m",
                    provider="p",
                    agent="bench_coder",
                    timeout=1,
                )
                log_text = (Path(td) / "run.log").read_text(encoding="utf-8")
        finally:
            benchmark_report.ensure_server_running = orig_ensure

        self.assertEqual(result["code"], 2)
        self.assertIn("startup probe crashed", log_text)

    def test_run_copy_logs_startup_status_when_server_not_ready(self):
        orig_ensure = benchmark_report.ensure_server_running
        try:
            def fail(work_dir, port, status):
                status("specific startup failure")
                return False

            benchmark_report.ensure_server_running = fail
            with tempfile.TemporaryDirectory() as td:
                result = benchmark_report.run_copy(
                    index=1,
                    work_dir=Path(td),
                    port=4096,
                    task="task",
                    model="m",
                    provider="p",
                    agent="bench_coder",
                    timeout=1,
                )
                log_text = (Path(td) / "run.log").read_text(encoding="utf-8")
        finally:
            benchmark_report.ensure_server_running = orig_ensure

        self.assertEqual(result["code"], 2)
        self.assertIn("specific startup failure", log_text)
        self.assertIn("[не удалось поднять opencode serve]", log_text)

    def test_try_connect_treats_timeout_as_not_ready(self):
        class APITimeoutError(Exception):
            pass

        class FakeSession:
            def list(self):
                raise APITimeoutError("request timed out")

        class FakeClient:
            session = FakeSession()

        orig_client = runtime.client_for_port
        try:
            runtime.client_for_port = lambda port: FakeClient()
            connected = runtime._try_connect(4096)
        finally:
            runtime.client_for_port = orig_client

        self.assertFalse(connected)

    def test_status_printer_ignores_broken_pipe(self):
        orig_print = builtins.print
        try:
            def broken_print(*args, **kwargs):
                raise BrokenPipeError("pipe closed")

            builtins.print = broken_print
            runtime.status_printer("copy 1")("готово")
        finally:
            builtins.print = orig_print

    def test_provider_limit_error_detection_matches_ollama_cloud_messages(self):
        self.assertTrue(runtime._is_provider_limit_error(
            "HTTP 429 | AI_APICallError | you have reached your weekly usage limit"
        ))
        self.assertTrue(runtime._is_retryable_limit_error(
            "HTTP 429 | AI_APICallError | you have reached your weekly usage limit"
        ))
        self.assertTrue(runtime._is_provider_limit_error(
            "HTTP 403 | AI_APICallError | this model requires a subscription"
        ))
        self.assertFalse(runtime._is_retryable_limit_error(
            "HTTP 403 | AI_APICallError | this model requires a subscription"
        ))
        self.assertTrue(runtime._is_provider_limit_error("Too Many Requests"))
        self.assertFalse(runtime._is_provider_limit_error(
            "SSE reader error: simulated disconnect"
        ))

    def test_opencode_error_tail_extracts_provider_response_body(self):
        raw_response = json.dumps({
            "error": (
                "you (ksamatadirect) have reached your weekly usage limit, "
                "upgrade for higher limits"
            )
        })
        line = (
            'ERROR service=llm providerID=ollama-cloud modelID=minimax-m2.1 '
            'session.id=ses_test error={"error":{"name":"AI_APICallError",'
            '"statusCode":429,"responseBody":'
            f'{json.dumps(raw_response)}}}'
        )

        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td)
            (log_dir / "opencode.log").write_text(line + "\n", encoding="utf-8")
            orig_log_dir = runtime.OPENCODE_LOG_DIR
            try:
                runtime.OPENCODE_LOG_DIR = log_dir
                tail = runtime._opencode_error_tail("ses_test")
            finally:
                runtime.OPENCODE_LOG_DIR = orig_log_dir

        self.assertIn("HTTP 429", tail or "")
        self.assertIn("AI_APICallError", tail or "")
        self.assertIn("weekly usage limit", tail or "")

    def test_opencode_error_tail_can_filter_by_agent(self):
        title_response = json.dumps({
            "error": "this model requires a subscription"
        })
        main_response = json.dumps({
            "error": "you have reached your weekly usage limit"
        })
        prefix_response = json.dumps({
            "error": "wrong agent prefix match"
        })
        title_line = (
            'ERROR service=llm session.id=ses_test agent=title '
            'error={"error":{"name":"AI_APICallError","statusCode":403,'
            f'"responseBody":{json.dumps(title_response)}}}'
        )
        prefix_line = (
            'ERROR service=llm session.id=ses_test agent=bench_coder_v2 '
            'error={"error":{"name":"AI_APICallError","statusCode":429,'
            f'"responseBody":{json.dumps(prefix_response)}}}'
        )
        main_line = (
            'ERROR service=llm session.id=ses_test agent=bench_coder '
            'error={"error":{"name":"AI_APICallError","statusCode":429,'
            f'"responseBody":{json.dumps(main_response)}}}'
        )

        with tempfile.TemporaryDirectory() as td:
            log_dir = Path(td)
            (log_dir / "opencode.log").write_text(
                title_line + "\n" + prefix_line + "\n" + main_line + "\n",
                encoding="utf-8",
            )
            orig_log_dir = runtime.OPENCODE_LOG_DIR
            try:
                runtime.OPENCODE_LOG_DIR = log_dir
                tail = runtime._opencode_error_tail(
                    "ses_test",
                    agent="bench_coder",
                )
            finally:
                runtime.OPENCODE_LOG_DIR = orig_log_dir

        self.assertIn("HTTP 429", tail or "")
        self.assertIn("weekly usage limit", tail or "")
        self.assertNotIn("requires a subscription", tail or "")
        self.assertNotIn("wrong agent prefix match", tail or "")

    def test_message_post_timeout_is_capped_for_long_deadline(self):
        now = 100.0
        self.assertEqual(
            runtime._message_post_timeout(deadline=None, now=now),
            runtime.POST_MESSAGE_READ_TIMEOUT,
        )
        self.assertEqual(
            runtime._message_post_timeout(deadline=now + 1800.0, now=now),
            runtime.POST_MESSAGE_READ_TIMEOUT,
        )

    def test_probe_session_retries_then_rate_limited(self):
        # Лимит провайдера держится на всех попытках -> probe_session ретраит
        # с backoff и в итоге отдаёт отдельный статус «лимит» (code=3).
        class ReadTimeoutHttpClient(FakeHttpClient):
            def post(self, path, json=None, timeout=None):
                if path == "/session":
                    return FakeResponse({"id": "ses_test"})
                if path == "/session/ses_test/message":
                    raise runtime.httpx.ReadTimeout("stream did not finish")
                raise AssertionError(path)

        messages = []
        sleeps = []
        result = self._probe_session(
            client=ReadTimeoutHttpClient,
            tail=lambda session_id, **kwargs: (
                "HTTP 429 | AI_APICallError | weekly usage limit"),
            sleeps=sleeps,
            write=messages.append,
            model="minimax-m2.1", provider="ollama-cloud",
        )

        self.assertEqual(result.code, 3)
        self.assertIn("provider limit", result.reason or "")
        self.assertIn("weekly usage limit", result.reason or "")
        self.assertIn("лимит провайдера", "".join(messages))
        # 5 попыток -> 4 паузы backoff: 5, 10, 20, 40 (без пауз инициализации reader).
        self.assertEqual(backoff_sleeps(sleeps), [5.0, 10.0, 20.0, 40.0])

    def test_probe_session_prefers_completion_racing_provider_limit_log(self):
        # Гонка: idle (done) выставлен ДО проверки лимита -> успех (code=0)
        # побеждает, ретрая быть не должно (лимит проигрывает завершению).
        class ReadTimeoutHttpClient(FakeHttpClient):
            def post(self, path, json=None, timeout=None):
                if path == "/session":
                    return FakeResponse({"id": "ses_test"})
                if path == "/session/ses_test/message":
                    raise runtime.httpx.ReadTimeout("stream did not finish")
                raise AssertionError(path)

        orig_event = runtime.threading.Event
        events = []
        sleeps = []

        def tracking_event():
            event = orig_event()
            events.append(event)
            return event

        def set_done_then_return_limit(session_id, **kwargs):
            events[0].set()
            return "HTTP 429 | AI_APICallError | weekly usage limit"

        with mock.patch.object(runtime.threading, "Event", tracking_event):
            result = self._probe_session(
                client=ReadTimeoutHttpClient,
                tail=set_done_then_return_limit,
                sleeps=sleeps,
                model="minimax-m2.1", provider="ollama-cloud",
            )

        self.assertEqual(result.code, 0)
        # успех на первой попытке — без backoff-ретраев
        self.assertEqual(backoff_sleeps(sleeps), [])

    def test_rate_limit_backoff_sequence(self):
        seq = [runtime._rate_limit_backoff(n) for n in range(1, 6)]
        self.assertEqual(seq, [5.0, 10.0, 20.0, 40.0, 60.0])  # 5-я упирается в потолок

    def test_verdict_rate_limited(self):
        self.assertEqual(runtime.verdict(3), "лимит")

    def test_probe_session_post_429_is_rate_limited(self):
        # 429 приходит прямо в HTTP-ответе POST /message (не в логе opencode).
        class Resp429:
            status_code = 429
            text = "Rate limit exceeded: free-models-per-min"

            def json(self):
                return {}

        class Http429Client(FakeHttpClient):
            def post(self, path, json=None, timeout=None):
                if path == "/session":
                    return FakeResponse({"id": "ses_test"})
                if path == "/session/ses_test/message":
                    return Resp429()
                raise AssertionError(path)

        sleeps = []
        result = self._probe_session(
            client=Http429Client,
            tail=lambda session_id, **kwargs: None,
            sleeps=sleeps,
            model="z-ai/glm-4.5-air:free", provider="openrouter",
        )

        self.assertEqual(result.code, 3)
        self.assertEqual(backoff_sleeps(sleeps), [5.0, 10.0, 20.0, 40.0])

    def test_probe_session_payload_limit_error_is_rate_limited(self):
        # Лимит может прийти в успешном HTTP-ответе как payload.info.error.
        class PayloadLimitClient(FakeHttpClient):
            def post(self, path, json=None, timeout=None):
                if path == "/session":
                    return FakeResponse({"id": "ses_test"})
                if path == "/session/ses_test/message":
                    return FakeResponse({
                        "info": {
                            "error": {
                                "data": {
                                    "message": (
                                        "Rate limit exceeded: free-models-per-min"),
                                    "statusCode": 429,
                                },
                            },
                        },
                    })
                raise AssertionError(path)

        sleeps = []
        result = self._probe_session(
            client=PayloadLimitClient,
            tail=lambda session_id, **kwargs: None,
            sleeps=sleeps,
            model="z-ai/glm-4.5-air:free", provider="openrouter",
        )

        self.assertEqual(result.code, 3)
        self.assertIn("Rate limit exceeded", result.reason or "")
        self.assertEqual(backoff_sleeps(sleeps), [5.0, 10.0, 20.0, 40.0])

    def test_probe_session_sse_limit_error_is_rate_limited(self):
        # Лимит во время исполнения приходит через session.error из SSE.
        class LimitSSE:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def iter_sse(self):
                yield SimpleNamespace(
                    data=json.dumps({
                        "type": "session.error",
                        "properties": {
                            "sessionID": "ses_test",
                            "error": {
                                "data": {
                                    "message": (
                                        "HTTP 429 | AI_APICallError | "
                                        "weekly usage limit"),
                                },
                            },
                        },
                    })
                )

        sleeps = []
        result = self._probe_session(
            client=FakeHttpClient,
            sse=LimitSSE,
            tail=lambda session_id, **kwargs: None,
            sleeps=sleeps,
            model="minimax-m2.1",
            provider="ollama-cloud",
        )

        self.assertEqual(result.code, 3)
        self.assertIn("HTTP 429", result.reason or "")
        self.assertEqual(backoff_sleeps(sleeps), [5.0, 10.0, 20.0, 40.0])

    def test_probe_session_permanent_account_error_is_not_retried(self):
        class SubscriptionClient(FakeHttpClient):
            def post(self, path, json=None, timeout=None):
                if path == "/session":
                    return FakeResponse({"id": "ses_test"})
                if path == "/session/ses_test/message":
                    return FakeResponse({
                        "info": {
                            "error": (
                                "this model requires a subscription"),
                        },
                    })
                raise AssertionError(path)

        sleeps = []
        result = self._probe_session(
            client=SubscriptionClient,
            tail=lambda session_id, **kwargs: None,
            sleeps=sleeps,
            model="paid-model",
            provider="ollama-cloud",
        )

        self.assertEqual(result.code, 2)
        self.assertIn("requires a subscription", result.reason or "")
        self.assertEqual(backoff_sleeps(sleeps), [])

    def test_probe_session_non_limit_error_not_retried(self):
        # Обычная ошибка сессии (не лимит) -> code=2, без ретраев.
        class ErrorSSE:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def iter_sse(self):
                yield SimpleNamespace(
                    data=json.dumps({
                        "type": "session.error",
                        "properties": {
                            "sessionID": "ses_test",
                            "error": {"data": {"message": "boom, not a limit"}},
                        },
                    })
                )

        class ReadTimeoutHttpClient(FakeHttpClient):
            def post(self, path, json=None, timeout=None):
                if path == "/session":
                    return FakeResponse({"id": "ses_test"})
                if path == "/session/ses_test/message":
                    raise runtime.httpx.ReadTimeout("stream did not finish")
                raise AssertionError(path)

        sleeps = []
        result = self._probe_session(
            client=ReadTimeoutHttpClient,
            sse=ErrorSSE,
            tail=lambda session_id, **kwargs: None,
            sleeps=sleeps,
        )

        self.assertEqual(result.code, 2)
        self.assertEqual(backoff_sleeps(sleeps), [])

    def test_existing_unowned_server_is_port_conflict(self):
        orig_try = runtime._try_connect
        orig_popen = runtime.subprocess.Popen
        orig_owners = dict(runtime._server_owners)
        popen_calls = []
        statuses = []
        try:
            runtime._server_owners.clear()
            runtime._try_connect = lambda port: True

            def fake_popen(*args, **kwargs):
                popen_calls.append((args, kwargs))
                raise AssertionError("Popen should not be called")

            runtime.subprocess.Popen = fake_popen
            with tempfile.TemporaryDirectory() as td:
                ok = runtime.ensure_server_running(Path(td), 4096, statuses.append)
        finally:
            runtime._try_connect = orig_try
            runtime.subprocess.Popen = orig_popen
            runtime._server_owners.clear()
            runtime._server_owners.update(orig_owners)

        self.assertFalse(ok)
        self.assertEqual(popen_calls, [])
        self.assertTrue(statuses)

    def test_ensure_server_running_closes_parent_stderr_handle(self):
        with tempfile.TemporaryDirectory() as td:
            stderr_path = Path(td) / "opencode.log"
            fake_file = FakeNamedTemp(stderr_path)
            fake_proc = FakeProcess()

            orig_try = runtime._try_connect
            orig_popen = runtime.subprocess.Popen
            orig_tempfile = runtime.tempfile.NamedTemporaryFile
            orig_sleep = runtime.time.sleep
            orig_processes = list(runtime._server_processes)
            orig_owners = dict(runtime._server_owners)
            attempts = {"count": 0}
            try:
                runtime._server_processes.clear()
                runtime._server_owners.clear()

                def fake_try_connect(port):
                    attempts["count"] += 1
                    return attempts["count"] > 1

                runtime._try_connect = fake_try_connect
                runtime.subprocess.Popen = lambda *args, **kwargs: fake_proc
                runtime.tempfile.NamedTemporaryFile = lambda *args, **kwargs: fake_file
                runtime.time.sleep = lambda seconds: None

                ok = runtime.ensure_server_running(Path(td), 4096, lambda msg: None)
            finally:
                runtime._try_connect = orig_try
                runtime.subprocess.Popen = orig_popen
                runtime.tempfile.NamedTemporaryFile = orig_tempfile
                runtime.time.sleep = orig_sleep
                runtime._server_processes.clear()
                runtime._server_processes.extend(orig_processes)
                runtime._server_owners.clear()
                runtime._server_owners.update(orig_owners)

        self.assertTrue(ok)
        self.assertTrue(fake_file.closed)

    def test_stop_servers_deletes_logs_and_clears_runtime_collections(self):
        with tempfile.TemporaryDirectory() as td:
            log_path = Path(td) / "serve.log"
            log_path.write_text("stderr", encoding="utf-8")
            fake_proc = FakeProcess()
            orig_processes = list(runtime._server_processes)
            orig_owners = dict(runtime._server_owners)
            try:
                runtime._server_processes.clear()
                runtime._server_processes.append((fake_proc, log_path))
                runtime._server_owners.clear()
                runtime._server_owners[4096] = (fake_proc, Path(td))

                runtime.stop_servers()

                self.assertEqual(runtime._server_processes, [])
                self.assertEqual(runtime._server_owners, {})
            finally:
                runtime._server_processes.clear()
                runtime._server_processes.extend(orig_processes)
                runtime._server_owners.clear()
                runtime._server_owners.update(orig_owners)

        self.assertTrue(fake_proc.terminated)
        self.assertFalse(log_path.exists())

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

    def test_extract_session_usage_ignores_non_assistant_token_messages(self):
        usage = usage_metrics.extract_session_usage([
            {
                "info": {
                    "role": "user",
                    "tokens": {"input": 1000, "output": 1000},
                },
            },
            {
                "info": {
                    "role": "assistant",
                    "tokens": {"input": 10, "output": 5},
                },
            },
        ])

        self.assertEqual(usage.to_report_dict()["total_tokens"], 15)

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

    def test_model_exclusion_helpers_block_unblock_and_reactivate(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                with conn:
                    first = db.block_model_exclusion(
                        conn, " provider ", " model ", "broken",
                    )
                    second = db.block_model_exclusion(
                        conn, "provider", "model", "still broken",
                    )

                active = db.list_model_exclusions(conn)
                all_rows = db.list_model_exclusions(conn, active_only=False)

                with conn:
                    unblocked = db.unblock_model_exclusion(conn, "provider", "model")

                active_after_unblock = db.list_model_exclusions(conn)
                inactive = db.get_model_exclusion(
                    conn, "provider", "model", active_only=False,
                )
            finally:
                conn.close()

        self.assertEqual(first["provider"], "provider")
        self.assertEqual(second["reason"], "still broken")
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertEqual(len(active), 1)
        self.assertEqual(len(all_rows), 1)
        self.assertEqual(unblocked["active"], 0)
        self.assertEqual(active_after_unblock, [])
        self.assertEqual(inactive["reason"], "still broken")

    def test_run_benchmark_rejects_excluded_model_before_work_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                with conn:
                    db.block_model_exclusion(conn, "provider", "model", "bad")
            finally:
                conn.close()

            original_connect = benchmark_report.connect
            original_prepare = benchmark_report.prepare_work_dirs
            called = {"prepare": False}

            def fake_prepare(*args, **kwargs):
                called["prepare"] = True
                raise AssertionError("prepare_work_dirs should not be called")

            try:
                benchmark_report.connect = lambda: db.connect(db_path)
                benchmark_report.prepare_work_dirs = fake_prepare
                with self.assertRaisesRegex(ValueError, "исключена из бенчмарка"):
                    benchmark_report.run_benchmark(SimpleNamespace(
                        project="p",
                        file=None,
                        task="task",
                        provider="provider",
                        model="model",
                        copies=1,
                        base_port=4096,
                        agent="bench_coder",
                        timeout=1,
                        force_excluded=False,
                    ))
            finally:
                benchmark_report.connect = original_connect
                benchmark_report.prepare_work_dirs = original_prepare

        self.assertFalse(called["prepare"])

    def test_run_benchmark_force_excluded_bypasses_guard(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                with conn:
                    db.block_model_exclusion(conn, "provider", "model", "bad")
            finally:
                conn.close()

            original_connect = benchmark_report.connect
            original_prepare = benchmark_report.prepare_work_dirs
            called = {"prepare": False}

            def fake_prepare(*args, **kwargs):
                called["prepare"] = True
                raise RuntimeError("stop after exclusion guard")

            try:
                benchmark_report.connect = lambda: db.connect(db_path)
                benchmark_report.prepare_work_dirs = fake_prepare
                with self.assertRaisesRegex(RuntimeError, "stop after exclusion guard"):
                    benchmark_report.run_benchmark(SimpleNamespace(
                        project="p",
                        file=None,
                        task="task",
                        provider="provider",
                        model="model",
                        copies=1,
                        base_port=4096,
                        agent="bench_coder",
                        timeout=1,
                        force_excluded=True,
                    ))
            finally:
                benchmark_report.connect = original_connect
                benchmark_report.prepare_work_dirs = original_prepare

        self.assertTrue(called["prepare"])

    def test_validate_benchmark_args_accepts_zero_timeout_and_rejects_bad_ports(self):
        parser = argparse.ArgumentParser()

        bench.validate_benchmark_args(parser, SimpleNamespace(
            copies=1,
            timeout=0,
            base_port=4096,
        ))

        with self.assertRaises(SystemExit):
            bench.validate_benchmark_args(parser, SimpleNamespace(
                copies=1,
                timeout=-1,
                base_port=4096,
            ))
        with self.assertRaises(SystemExit):
            bench.validate_benchmark_args(parser, SimpleNamespace(
                copies=2,
                timeout=1,
                base_port=65535,
            ))
        with self.assertRaises(SystemExit):
            bench.validate_benchmark_args(parser, SimpleNamespace(
                copies=1,
                timeout=1,
                base_port=0,
            ))

    def test_run_benchmark_rejects_whitespace_only_task(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
            finally:
                conn.close()

            original_connect = benchmark_report.connect
            try:
                benchmark_report.connect = lambda: db.connect(db_path)
                with self.assertRaisesRegex(ValueError, "Нет задания"):
                    benchmark_report.run_benchmark(SimpleNamespace(
                        project="missing",
                        file=None,
                        task="   ",
                        provider="provider",
                        model="model",
                        copies=1,
                        base_port=4096,
                        agent="bench_coder",
                        timeout=1,
                        force_excluded=False,
                    ))
            finally:
                benchmark_report.connect = original_connect

    def test_unknown_project_with_explicit_task_warns_and_runs_ad_hoc(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            work_dir = Path(td) / "work"
            work_dir.mkdir()
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
            finally:
                conn.close()

            original_connect = benchmark_report.connect
            original_prepare = benchmark_report.prepare_work_dirs
            original_run_copy = benchmark_report.run_copy
            original_get_pricing = benchmark_report.get_pricing
            original_collect = benchmark_report.collect_report_artifacts
            original_cleanup = benchmark_report.cleanup_collected_artifacts
            try:
                benchmark_report.connect = lambda: db.connect(db_path)
                benchmark_report.prepare_work_dirs = lambda *args: [work_dir]
                benchmark_report.run_copy = lambda *args, **kwargs: {
                    "index": 1,
                    "port": 4096,
                    "dir": str(work_dir),
                    "code": 0,
                    "elapsed": 0.1,
                    "usage": None,
                }
                benchmark_report.get_pricing = lambda provider, model: {
                    "prompt_per_1m": 0.0,
                    "completion_per_1m": 0.0,
                }
                benchmark_report.collect_report_artifacts = lambda results: SimpleNamespace(
                    artifacts=[],
                    summary=lambda: {},
                )
                benchmark_report.cleanup_collected_artifacts = lambda collection: None

                stderr = io.StringIO()
                with contextlib.redirect_stderr(stderr):
                    rc = benchmark_report.run_benchmark(SimpleNamespace(
                        project="ad_hoc",
                        file=None,
                        task="task",
                        provider="provider",
                        model="model",
                        copies=1,
                        base_port=4096,
                        agent="bench_coder",
                        timeout=1,
                        force_excluded=False,
                    ))
                conn = db.connect(db_path)
                try:
                    raw_json = conn.execute(
                        "SELECT raw_json FROM reports WHERE project = 'ad_hoc'",
                    ).fetchone()["raw_json"]
                finally:
                    conn.close()
            finally:
                benchmark_report.connect = original_connect
                benchmark_report.prepare_work_dirs = original_prepare
                benchmark_report.run_copy = original_run_copy
                benchmark_report.get_pricing = original_get_pricing
                benchmark_report.collect_report_artifacts = original_collect
                benchmark_report.cleanup_collected_artifacts = original_cleanup

        report = json.loads(raw_json)
        self.assertEqual(rc, 0)
        self.assertIn("warning: проект 'ad_hoc' не найден", stderr.getvalue())
        self.assertIsNone(report["description"])
        self.assertIsNone(report["what_it_tests"])

    def test_known_project_report_stores_what_it_tests(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            work_dir = Path(td) / "work"
            work_dir.mkdir()
            entry = {
                "prompt": "task from library",
                "description": "desc",
                "what_it_tests": ["one", "two"],
            }
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                with conn:
                    conn.execute(
                        """
                        INSERT INTO projects_library
                            (name, description, prompt, what_it_tests, raw_json)
                        VALUES (?, ?, ?, ?, ?)
                        """,
                        (
                            "known",
                            entry["description"],
                            entry["prompt"],
                            json.dumps(entry["what_it_tests"]),
                            json.dumps(entry),
                        ),
                    )
            finally:
                conn.close()

            original_connect = benchmark_report.connect
            original_prepare = benchmark_report.prepare_work_dirs
            original_run_copy = benchmark_report.run_copy
            original_get_pricing = benchmark_report.get_pricing
            original_collect = benchmark_report.collect_report_artifacts
            original_cleanup = benchmark_report.cleanup_collected_artifacts
            try:
                benchmark_report.connect = lambda: db.connect(db_path)
                benchmark_report.prepare_work_dirs = lambda *args: [work_dir]
                benchmark_report.run_copy = lambda *args, **kwargs: {
                    "index": 1,
                    "port": 4096,
                    "dir": str(work_dir),
                    "code": 0,
                    "elapsed": 0.1,
                    "usage": None,
                }
                benchmark_report.get_pricing = lambda provider, model: {
                    "prompt_per_1m": 0.0,
                    "completion_per_1m": 0.0,
                }
                benchmark_report.collect_report_artifacts = lambda results: SimpleNamespace(
                    artifacts=[],
                    summary=lambda: {},
                )
                benchmark_report.cleanup_collected_artifacts = lambda collection: None

                benchmark_report.run_benchmark(SimpleNamespace(
                    project="known",
                    file=None,
                    task=None,
                    provider="provider",
                    model="model",
                    copies=1,
                    base_port=4096,
                    agent="bench_coder",
                    timeout=1,
                    force_excluded=False,
                ))
                conn = db.connect(db_path)
                try:
                    raw_json = conn.execute(
                        "SELECT raw_json FROM reports WHERE project = 'known'",
                    ).fetchone()["raw_json"]
                finally:
                    conn.close()
            finally:
                benchmark_report.connect = original_connect
                benchmark_report.prepare_work_dirs = original_prepare
                benchmark_report.run_copy = original_run_copy
                benchmark_report.get_pricing = original_get_pricing
                benchmark_report.collect_report_artifacts = original_collect
                benchmark_report.cleanup_collected_artifacts = original_cleanup

        report = json.loads(raw_json)
        self.assertEqual(report["prompt"], "task from library")
        self.assertEqual(report["what_it_tests"], ["one", "two"])

    def test_check_models_filter_excluded_models(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                with conn:
                    db.block_model_exclusion(conn, "provider", "bad", "bad model")
            finally:
                conn.close()

            original_connect = check_models.connect
            try:
                check_models.connect = lambda: db.connect(db_path)
                refs = [
                    check_models.ModelRef("provider", "good"),
                    check_models.ModelRef("provider", "bad"),
                ]
                allowed, skipped = check_models.filter_excluded_models(refs)
            finally:
                check_models.connect = original_connect

        self.assertEqual([r.key for r in allowed], ["provider/good"])
        self.assertEqual([(r.key, reason) for r, reason in skipped],
                         [("provider/bad", "bad model")])

    def test_model_catalog_parses_simple_opencode_models_output(self):
        entries = model_catalog.parse_opencode_models_output(
            "opencode/glm-5.1\n"
            "openrouter/google/gemma-4-31b-it\n"
        )

        self.assertEqual([e.key for e in entries], [
            "opencode/glm-5.1",
            "openrouter/google/gemma-4-31b-it",
        ])
        self.assertEqual(entries[1].provider, "openrouter")
        self.assertEqual(entries[1].model, "google/gemma-4-31b-it")

    def test_model_catalog_parses_verbose_opencode_models_output(self):
        entries = model_catalog.parse_opencode_models_output(
            "opencode/big-pickle\n"
            "{\n"
            '  "id": "big-pickle",\n'
            '  "providerID": "opencode",\n'
            '  "name": "Big Pickle",\n'
            '  "cost": {"input": 0, "output": 0},\n'
            '  "limit": {"context": 200000, "output": 32000}\n'
            "}\n"
            "openrouter/google/gemma-4-31b-it\n"
            "{\n"
            '  "id": "google/gemma-4-31b-it",\n'
            '  "providerID": "openrouter",\n'
            '  "name": "Gemma 4 31B",\n'
            '  "cost": {"input": 0.1, "output": 0.2}\n'
            "}\n"
        )

        self.assertEqual([e.key for e in entries], [
            "opencode/big-pickle",
            "openrouter/google/gemma-4-31b-it",
        ])
        self.assertEqual(entries[0].name, "Big Pickle")
        self.assertEqual(entries[0].cost, {"input": 0, "output": 0})
        self.assertEqual(entries[1].name, "Gemma 4 31B")

    def test_model_catalog_skips_refresh_banner(self):
        entries = model_catalog.parse_opencode_models_output(
            "Models cache refreshed\n"
            "opencode/big-pickle\n"
            "{\n"
            '  "id": "big-pickle",\n'
            '  "providerID": "opencode",\n'
            '  "name": "Big Pickle"\n'
            "}\n"
        )

        self.assertEqual([e.key for e in entries], ["opencode/big-pickle"])
        self.assertEqual(entries[0].name, "Big Pickle")

    def test_model_catalog_wraps_invalid_model_key(self):
        with self.assertRaises(model_catalog.ModelCatalogError) as raised:
            model_catalog.parse_opencode_models_output("not a model key\n")

        self.assertIn("provider/model", str(raised.exception))

    def test_load_opencode_models_uses_cli_without_serve(self):
        calls = []

        class FakeCompleted:
            stdout = "opencode/free-model\n"
            stderr = ""

        def fake_run(cmd, **kwargs):
            calls.append((cmd, kwargs))
            return FakeCompleted()

        original_run = model_catalog.subprocess.run
        try:
            model_catalog.subprocess.run = fake_run
            entries = model_catalog.load_opencode_models(
                provider="opencode",
                refresh=True,
            )
        finally:
            model_catalog.subprocess.run = original_run

        self.assertEqual([e.key for e in entries], ["opencode/free-model"])
        self.assertEqual(calls[0][0], [
            "opencode",
            "models",
            "opencode",
            "--pure",
            "--refresh",
            "--verbose",
        ])
        self.assertEqual(
            calls[0][1]["timeout"],
            model_catalog.OPENCODE_MODELS_TIMEOUT,
        )

    def test_load_opencode_models_wraps_timeout(self):
        def fake_run(cmd, **kwargs):
            raise model_catalog.subprocess.TimeoutExpired(cmd, 60)

        original_run = model_catalog.subprocess.run
        try:
            model_catalog.subprocess.run = fake_run
            with self.assertRaises(model_catalog.ModelCatalogError) as raised:
                model_catalog.load_opencode_models()
        finally:
            model_catalog.subprocess.run = original_run

        self.assertIn("timed out", str(raised.exception))

    def test_check_models_resolves_catalog_without_server_and_query(self):
        entries = [
            model_catalog.ModelCatalogEntry(
                provider="opencode",
                model="big-pickle",
                name="Big Pickle",
                metadata={"cost": {"input": 0, "output": 0}},
            ),
            model_catalog.ModelCatalogEntry(
                provider="openrouter",
                model="google/gemma-4-31b-it",
                name="Gemma 4 31B",
                metadata={"cost": {"input": 0.1, "output": 0.2}},
            ),
        ]
        original_load = check_models.load_opencode_models
        original_rules = check_models.load_free_rules
        try:
            check_models.load_opencode_models = lambda **kwargs: entries
            check_models.load_free_rules = lambda: {
                "opencode": {"strategy": "cost-zero", "models": []},
                "openrouter": {"strategy": "name-free", "models": []},
            }
            refs, source, full_refs = check_models.resolve_model_list(
                SimpleNamespace(
                    models_file=None,
                    models=[],
                    provider=None,
                    pay_models=False,
                    query="pickle",
                    refresh_models=False,
                ),
            )
        finally:
            check_models.load_opencode_models = original_load
            check_models.load_free_rules = original_rules

        self.assertEqual(source, "opencode-models+free-only+query")
        self.assertEqual([r.key for r in refs], ["opencode/big-pickle"])
        self.assertEqual([r.key for r in full_refs], [
            "opencode/big-pickle",
            "openrouter/google/gemma-4-31b-it",
        ])

    def test_check_models_query_normalizes_model_name_separators(self):
        refs = [
            check_models.ModelRef(
                "provider",
                "opaque-id",
                name="MiniMax M2",
            ),
            check_models.ModelRef(
                "openrouter",
                "minimax/minimax-m2",
                name="MiniMax-M2",
            ),
            check_models.ModelRef(
                "openrouter",
                "minimax/minimax-m3",
                name="MiniMax-M3",
            ),
        ]

        self.assertEqual(
            [r.key for r in check_models.filter_model_query(refs, "minimax-m2")],
            [
                "provider/opaque-id",
                "openrouter/minimax/minimax-m2",
            ],
        )
        self.assertEqual(
            [
                r.key for r in check_models.filter_model_query(
                    refs,
                    "openrouter minimax minimax m3",
                )
            ],
            ["openrouter/minimax/minimax-m3"],
        )

    def test_check_models_list_models_does_not_start_server(self):
        original_argv = sys.argv
        original_load = check_models.load_opencode_models
        original_rules = check_models.load_free_rules
        original_filter = check_models.filter_excluded_models
        original_ensure = check_models.ensure_server_running
        original_install = check_models.install_shutdown_handlers
        try:
            sys.argv = [
                "check_models.py",
                "--list-models",
                "--pay-models",
                "--query",
                "pickle",
            ]
            check_models.load_opencode_models = lambda **kwargs: [
                model_catalog.ModelCatalogEntry(
                    provider="opencode",
                    model="big-pickle",
                    name="Big Pickle",
                    metadata={"cost": {"input": 0, "output": 0}},
                ),
            ]
            check_models.load_free_rules = lambda: {
                "opencode": {"strategy": "cost-zero", "models": []},
            }
            check_models.filter_excluded_models = lambda refs: (refs, [])

            def fail_ensure(*args, **kwargs):
                raise AssertionError("serve must not start")

            def fail_install():
                raise AssertionError("handlers must not install")

            check_models.ensure_server_running = fail_ensure
            check_models.install_shutdown_handlers = fail_install
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                check_models.main()
        finally:
            sys.argv = original_argv
            check_models.load_opencode_models = original_load
            check_models.load_free_rules = original_rules
            check_models.filter_excluded_models = original_filter
            check_models.ensure_server_running = original_ensure
            check_models.install_shutdown_handlers = original_install

        text = out.getvalue()
        self.assertIn("Моделей: 1", text)
        self.assertIn("opencode/big-pickle", text)

    def test_check_models_catalog_error_is_cli_error(self):
        original_argv = sys.argv
        original_load = check_models.load_opencode_models
        try:
            sys.argv = ["check_models.py", "--list-models"]

            def fail(**kwargs):
                raise model_catalog.ModelCatalogError("opencode models failed")

            check_models.load_opencode_models = fail
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as raised:
                    check_models.main()
        finally:
            sys.argv = original_argv
            check_models.load_opencode_models = original_load

        self.assertEqual(raised.exception.code, 2)
        self.assertIn("Не удалось получить список моделей", err.getvalue())
        self.assertIn("opencode models failed", err.getvalue())

    def test_cleanup_index_snapshot_deletes_existing_file_and_missing_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            index_path = Path(td) / "docs" / "data" / "index.json"
            index_path.parent.mkdir(parents=True)
            index_path.write_text("{}", encoding="utf-8")

            dashboard_server.cleanup_index_snapshot(index_path)
            dashboard_server.cleanup_index_snapshot(index_path)

        self.assertFalse(index_path.exists())

    def test_serve_removes_generated_index_on_exit(self):
        import socketserver

        original_project_root = dashboard_server.PROJECT_ROOT
        original_build_index = dashboard_server.build_index
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
                dashboard_server.PROJECT_ROOT = root
                dashboard_server.build_index = fake_build_index
                socketserver.TCPServer = FakeTCPServer
                dashboard_server.serve(9999)
            finally:
                dashboard_server.PROJECT_ROOT = original_project_root
                dashboard_server.build_index = original_build_index
                socketserver.TCPServer = original_tcp_server

            self.assertTrue(seen["index_exists_during_serve"])
            self.assertFalse(index_path.exists())

    def test_serve_cleans_index_on_sigterm_systemexit(self):
        # SIGTERM (через install_shutdown_handlers) поднимает SystemExit(143).
        # except KeyboardInterrupt его НЕ ловит, но finally обязан почистить
        # снапшот, а SystemExit — пробросить наружу.
        import socketserver

        original_project_root = dashboard_server.PROJECT_ROOT
        original_build_index = dashboard_server.build_index
        original_tcp_server = socketserver.TCPServer

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            index_path = root / "docs" / "data" / "index.json"

            def fake_build_index():
                index_path.parent.mkdir(parents=True, exist_ok=True)
                index_path.write_text('{"total": 0}', encoding="utf-8")
                return 0

            class SigtermTCPServer:
                def __init__(self, address, handler):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def serve_forever(self):
                    raise SystemExit(143)   # имитация доставленного SIGTERM

            try:
                dashboard_server.PROJECT_ROOT = root
                dashboard_server.build_index = fake_build_index
                socketserver.TCPServer = SigtermTCPServer
                with self.assertRaises(SystemExit) as ctx:
                    dashboard_server.serve(9999)
            finally:
                dashboard_server.PROJECT_ROOT = original_project_root
                dashboard_server.build_index = original_build_index
                socketserver.TCPServer = original_tcp_server

            self.assertEqual(ctx.exception.code, 143)
            self.assertFalse(index_path.exists())  # finally почистил снапшот

    def test_serve_branch_installs_shutdown_handlers(self):
        # bench.py serve должен ставить SIGTERM/SIGINT-хендлеры (иначе kill пройдёт
        # мимо finally в serve и оставит docs/data/index.json на диске).
        called = {"install": False, "port": None}
        original_install = bench.install_shutdown_handlers
        original_serve = bench.serve
        original_argv = sys.argv
        try:
            bench.install_shutdown_handlers = lambda: called.__setitem__("install", True)
            bench.serve = lambda port: called.__setitem__("port", port)
            sys.argv = ["bench.py", "serve", "--port", "8123"]
            bench.main()
        finally:
            bench.install_shutdown_handlers = original_install
            bench.serve = original_serve
            sys.argv = original_argv

        self.assertTrue(called["install"])
        self.assertEqual(called["port"], 8123)

    def test_serve_does_not_delete_index_when_server_never_started(self):
        import socketserver

        original_project_root = dashboard_server.PROJECT_ROOT
        original_build_index = dashboard_server.build_index
        original_tcp_server = socketserver.TCPServer

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            index_path = root / "docs" / "data" / "index.json"
            index_path.parent.mkdir(parents=True)
            index_path.write_text('{"existing": true}', encoding="utf-8")
            called = {"build_index": False}

            def fake_build_index():
                called["build_index"] = True
                index_path.write_text('{"new": true}', encoding="utf-8")
                return 0

            class FailingTCPServer:
                def __init__(self, address, handler):
                    raise OSError("port already in use")

            try:
                dashboard_server.PROJECT_ROOT = root
                dashboard_server.build_index = fake_build_index
                socketserver.TCPServer = FailingTCPServer
                with self.assertRaises(OSError):
                    dashboard_server.serve(9999)
            finally:
                dashboard_server.PROJECT_ROOT = original_project_root
                dashboard_server.build_index = original_build_index
                socketserver.TCPServer = original_tcp_server

            self.assertFalse(called["build_index"])
            self.assertEqual(index_path.read_text(encoding="utf-8"), '{"existing": true}')

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

            original_connect = index_builder.connect
            original_project_root = index_builder.PROJECT_ROOT
            try:
                index_builder.connect = lambda: db.connect(db_path)
                index_builder.PROJECT_ROOT = root
                count = index_builder.build_index()
            finally:
                index_builder.connect = original_connect
                index_builder.PROJECT_ROOT = original_project_root

            data = json.loads((root / "docs" / "data" / "index.json").read_text())

        run = data["projects"][0]["reports"][0]["runs"][0]
        self.assertEqual(count, 1)
        self.assertNotIn("usage", run)

    def test_build_index_hides_active_model_exclusions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                visible_report = {
                    "project": "p",
                    "provider": "provider",
                    "model": "visible",
                    "started_at": "2026-01-01T00:00:00",
                    "summary": {"ok": 1, "timeout": 0, "error": 0},
                    "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                    "usage_summary": {
                        "total_tokens": 100,
                        "estimated_cost_usd": 0.1,
                    },
                    "runs": [{"index": 1, "code": 0}],
                }
                hidden_report = {
                    "project": "p",
                    "provider": "provider",
                    "model": "hidden",
                    "started_at": "2026-01-02T00:00:00",
                    "summary": {"ok": 0, "timeout": 0, "error": 1},
                    "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                    "usage_summary": {
                        "total_tokens": 200,
                        "estimated_cost_usd": 0.2,
                    },
                    "runs": [{"index": 1, "code": 2}],
                }
                with conn:
                    db.upsert_report(
                        conn,
                        visible_report,
                        "data/result/p/visible/report.json",
                        json.dumps(visible_report),
                    )
                    db.upsert_report(
                        conn,
                        hidden_report,
                        "data/result/p/hidden/report.json",
                        json.dumps(hidden_report),
                    )
                    db.block_model_exclusion(conn, "provider", "hidden", "bad")
            finally:
                conn.close()

            original_connect = index_builder.connect
            original_project_root = index_builder.PROJECT_ROOT
            try:
                index_builder.connect = lambda: db.connect(db_path)
                index_builder.PROJECT_ROOT = root
                count = index_builder.build_index()
            finally:
                index_builder.connect = original_connect
                index_builder.PROJECT_ROOT = original_project_root

            data = json.loads((root / "docs" / "data" / "index.json").read_text())

        reports = data["projects"][0]["reports"]
        dashboard_summary = data["dashboard_summary"]
        self.assertEqual(count, 1)
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["total_models"], 1)
        self.assertEqual(dashboard_summary["project_count"], 1)
        self.assertEqual(dashboard_summary["model_count"], 2)
        self.assertEqual(dashboard_summary["report_count"], 2)
        self.assertEqual(dashboard_summary["run_count"], 2)
        self.assertEqual(dashboard_summary["ok"], 1)
        self.assertEqual(dashboard_summary["timeout"], 0)
        self.assertEqual(dashboard_summary["error"], 1)
        self.assertEqual(dashboard_summary["rate_limited"], 0)
        self.assertEqual(dashboard_summary["total_tokens"], 300)
        self.assertAlmostEqual(dashboard_summary["estimated_cost_usd"], 0.3)
        self.assertEqual(dashboard_summary["excluded_report_count"], 1)
        self.assertEqual(dashboard_summary["excluded_run_count"], 1)
        self.assertEqual(data["projects"][0]["model_count"], 1)
        self.assertEqual(data["projects"][0]["run_count"], 1)
        self.assertEqual(
            data["projects"][0]["summary"],
            {"ok": 1, "timeout": 0, "error": 0, "rate_limited": 0},
        )
        self.assertEqual([report["model"] for report in reports], ["visible"])
        self.assertEqual([row["model"] for row in data["model_ranking"]], ["visible"])
        self.assertIsNone(data["model_ranking"][0]["avg_tokens"])
        self.assertIsNone(data["model_ranking"][0]["avg_cost_usd"])

    def test_build_index_keeps_dashboard_summary_when_all_reports_excluded(self):
        report = {
            "project": "p",
            "provider": "provider",
            "model": "hidden",
            "started_at": "2026-01-01T00:00:00",
            "summary": {"ok": 0, "timeout": 1, "error": 0},
            "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
            "usage_summary": {
                "total_tokens": 100,
                "estimated_cost_usd": 0.1,
            },
            "runs": [{"index": 1, "code": 1}],
        }

        count, data = self._build_index_data(
            [report],
            exclusions=[("provider", "hidden", "bad")],
        )

        dashboard_summary = data["dashboard_summary"]
        self.assertEqual(count, 0)
        self.assertEqual(data["total"], 0)
        self.assertEqual(data["total_models"], 0)
        self.assertEqual(data["projects"], [])
        self.assertEqual(data["model_ranking"], [])
        self.assertEqual(dashboard_summary["project_count"], 1)
        self.assertEqual(dashboard_summary["model_count"], 1)
        self.assertEqual(dashboard_summary["report_count"], 1)
        self.assertEqual(dashboard_summary["run_count"], 1)
        self.assertEqual(dashboard_summary["timeout"], 1)
        self.assertEqual(dashboard_summary["total_tokens"], 100)
        self.assertAlmostEqual(dashboard_summary["estimated_cost_usd"], 0.1)
        self.assertEqual(dashboard_summary["excluded_report_count"], 1)
        self.assertEqual(dashboard_summary["excluded_run_count"], 1)

    def test_build_index_keeps_inactive_model_exclusions_visible(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                report = {
                    "project": "p",
                    "provider": "provider",
                    "model": "model",
                    "started_at": "2026-01-01T00:00:00",
                    "summary": {"ok": 0, "timeout": 1, "error": 0},
                    "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                    "runs": [{"index": 1, "code": 1}],
                }
                with conn:
                    db.upsert_report(
                        conn,
                        report,
                        "data/result/p/report.json",
                        json.dumps(report),
                    )
                    db.block_model_exclusion(conn, "provider", "model", "old")
                    db.unblock_model_exclusion(conn, "provider", "model")
            finally:
                conn.close()

            original_connect = index_builder.connect
            original_project_root = index_builder.PROJECT_ROOT
            try:
                index_builder.connect = lambda: db.connect(db_path)
                index_builder.PROJECT_ROOT = root
                count = index_builder.build_index()
            finally:
                index_builder.connect = original_connect
                index_builder.PROJECT_ROOT = original_project_root

            data = json.loads((root / "docs" / "data" / "index.json").read_text())

        self.assertEqual(count, 1)
        self.assertEqual(data["projects"][0]["reports"][0]["model"], "model")

    def test_build_index_uses_report_what_it_tests_as_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                report = {
                    "project": "p",
                    "provider": "provider",
                    "model": "model",
                    "started_at": "2026-01-01T00:00:00",
                    "summary": {"ok": 1, "timeout": 0, "error": 0},
                    "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                    "what_it_tests": ["fallback"],
                    "runs": [],
                }
                with conn:
                    db.upsert_report(
                        conn,
                        report,
                        "data/result/p/report.json",
                        json.dumps(report),
                    )
            finally:
                conn.close()

            original_connect = index_builder.connect
            original_project_root = index_builder.PROJECT_ROOT
            try:
                index_builder.connect = lambda: db.connect(db_path)
                index_builder.PROJECT_ROOT = root
                index_builder.build_index()
            finally:
                index_builder.connect = original_connect
                index_builder.PROJECT_ROOT = original_project_root

            data = json.loads((root / "docs" / "data" / "index.json").read_text())

        self.assertEqual(data["projects"][0]["what_it_tests"], ["fallback"])

    def test_build_index_counts_distinct_models_per_project(self):
        reports = [
            {
                "project": "p",
                "provider": "provider",
                "model": "same",
                "started_at": "2026-01-01T00:00:00",
                "summary": {"ok": 1, "timeout": 0, "error": 0},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": [{"index": 1, "code": 0, "elapsed": 1.0}],
            },
            {
                "project": "p",
                "provider": "provider",
                "model": "same",
                "started_at": "2026-01-02T00:00:00",
                "summary": {"ok": 1, "timeout": 0, "error": 0},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": [{"index": 1, "code": 0, "elapsed": 2.0}],
            },
            {
                "project": "p",
                "provider": "provider",
                "model": "other",
                "started_at": "2026-01-03T00:00:00",
                "summary": {"ok": 1, "timeout": 0, "error": 0},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": [{"index": 1, "code": 0, "elapsed": 3.0}],
            },
        ]

        count, data = self._build_index_data(reports)
        project = data["projects"][0]

        self.assertEqual(count, 3)
        self.assertEqual(project["report_count"], 3)
        self.assertEqual(project["model_count"], 2)
        self.assertEqual(data["total_models"], 2)

    def test_build_index_model_ranking_uses_latest_reports_and_successful_run_averages(self):
        def report(project, model, started_at, runs):
            return {
                "project": project,
                "provider": "provider",
                "model": model,
                "started_at": started_at,
                "summary": {"ok": len(runs), "timeout": 0, "error": 0},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": runs,
            }

        count, data = self._build_index_data([
            report(
                "p1",
                "model-a",
                "2026-01-01T00:00:00",
                [{
                    "index": 1,
                    "code": 0,
                    "elapsed": 100.0,
                    "usage": {"total_tokens": 1000, "estimated_cost_usd": 1.0},
                }],
            ),
            report(
                "p1",
                "model-a",
                "2026-01-03T00:00:00",
                [
                    {
                        "index": 1,
                        "code": 0,
                        "elapsed": 20.0,
                        "usage": {"total_tokens": 200, "estimated_cost_usd": 0.2},
                    },
                    {"index": 2, "code": 0, "elapsed": 40.0},
                ],
            ),
            report(
                "p2",
                "model-a",
                "2026-01-02T00:00:00",
                [{
                    "index": 1,
                    "code": 0,
                    "elapsed": 10.0,
                    "usage": {"total_tokens": 100, "estimated_cost_usd": 0.1},
                }],
            ),
            report(
                "p1",
                "model-b",
                "2026-01-04T00:00:00",
                [{"index": 1, "code": 0, "elapsed": 5.0}],
            ),
        ])

        ranking = {row["key"]: row for row in data["model_ranking"]}
        model_a = ranking["provider/model-a"]

        self.assertEqual(count, 4)
        self.assertEqual(model_a["projects"], ["p1", "p2"])
        self.assertEqual(model_a["project_count"], 2)
        self.assertEqual(model_a["successful_run_count"], 3)
        self.assertAlmostEqual(model_a["avg_elapsed"], 70.0 / 3.0)
        self.assertEqual(model_a["avg_tokens"], 150)
        self.assertAlmostEqual(model_a["avg_cost_usd"], 0.15)
        self.assertEqual(model_a["latest_started_at"], "2026-01-03T00:00:00")
        self.assertLess(ranking["provider/model-b"]["rank"], model_a["rank"])

    def test_build_index_model_ranking_hides_models_with_latest_failures(self):
        reports = [
            {
                "project": "p",
                "provider": "provider",
                "model": "regressed",
                "started_at": "2026-01-01T00:00:00",
                "summary": {"ok": 1, "timeout": 0, "error": 0},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": [{"index": 1, "code": 0, "elapsed": 1.0}],
            },
            {
                "project": "p",
                "provider": "provider",
                "model": "regressed",
                "started_at": "2026-01-02T00:00:00",
                "summary": {"ok": 0, "timeout": 1, "error": 0},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": [{"index": 1, "code": 1, "elapsed": 60.0}],
            },
            {
                "project": "p",
                "provider": "provider",
                "model": "errored",
                "started_at": "2026-01-03T00:00:00",
                "summary": {"ok": 0, "timeout": 0, "error": 1},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": [{"index": 1, "code": 2, "elapsed": 2.0}],
            },
            {
                "project": "p",
                "provider": "provider",
                "model": "clean",
                "started_at": "2026-01-04T00:00:00",
                "summary": {"ok": 1, "timeout": 0, "error": 0},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": [{"index": 1, "code": 0, "elapsed": 3.0}],
            },
        ]

        _, data = self._build_index_data(reports)

        self.assertEqual(
            [row["key"] for row in data["model_ranking"]],
            ["provider/clean"],
        )

    def test_build_index_counts_rate_limited(self):
        reports = [
            {
                "project": "p",
                "provider": "provider",
                "model": "limited",
                "started_at": "2026-01-01T00:00:00",
                "summary": {"ok": 0, "timeout": 0, "error": 0, "rate_limited": 2},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": [
                    {"index": 1, "code": 3, "elapsed": 80.0},
                    {"index": 2, "code": 3, "elapsed": 80.0},
                ],
            },
        ]

        _, data = self._build_index_data(reports)

        project = data["projects"][0]
        self.assertEqual(project["summary"]["rate_limited"], 2)
        # Модель, упёршаяся в лимит, не попадает в рейтинг «без сбоев».
        self.assertEqual([row["key"] for row in data["model_ranking"]], [])

    def test_build_index_old_report_without_rate_limited_key(self):
        # Старые отчёты без ключа rate_limited -> агрегат 0 (обратная совместимость).
        reports = [
            {
                "project": "p",
                "provider": "provider",
                "model": "legacy",
                "started_at": "2026-01-01T00:00:00",
                "summary": {"ok": 1, "timeout": 0, "error": 0},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": [{"index": 1, "code": 0, "elapsed": 1.0}],
            },
        ]

        _, data = self._build_index_data(reports)

        self.assertEqual(data["projects"][0]["summary"]["rate_limited"], 0)

    def test_model_unstable_helpers_mark_unmark_and_reactivate(self):
        # Round-trip API статуса unstable (зеркало denylist-хелперов).
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                with conn:
                    first = db.mark_model_unstable(
                        conn, " provider ", " model ", "таймауты")
                    second = db.mark_model_unstable(
                        conn, "provider", "model", "лимит провайдера")

                active = db.list_model_unstable(conn)
                amap = db.active_unstable_map(conn)

                with conn:
                    unmarked = db.unmark_model_unstable(conn, "provider", "model")

                active_after = db.list_model_unstable(conn)
                inactive = db.get_model_unstable(
                    conn, "provider", "model", active_only=False)
            finally:
                conn.close()

        self.assertEqual(first["provider"], "provider")           # _clean_model_ref
        self.assertEqual(second["reason"], "лимит провайдера")     # reason обновился
        self.assertEqual(second["created_at"], first["created_at"])  # created_at не сброшен
        self.assertEqual(len(active), 1)
        self.assertEqual(amap, {("provider", "model"): "лимит провайдера"})
        self.assertEqual(unmarked["active"], 0)
        self.assertEqual(active_after, [])
        self.assertEqual(inactive["reason"], "лимит провайдера")

    def test_build_index_unstable_model_ranked_by_clean_projects_only(self):
        # Модель помечена unstable: чистый проект p_ok (5 успешных) + грязный p_bad
        # (3 ok + 2 timeout). Должна быть в рейтинге со status=unstable, метрики —
        # ТОЛЬКО по p_ok; грязный проект — в unstable_projects.
        def run(i, code, elapsed):
            return {"index": i, "code": code, "elapsed": elapsed,
                    "usage": {"total_tokens": 100, "estimated_cost_usd": 0.1}}

        reports = [
            {
                "project": "p_ok", "provider": "prov", "model": "m",
                "started_at": "2026-01-01T00:00:00",
                "summary": {"ok": 5, "timeout": 0, "error": 0, "rate_limited": 0},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": [run(i, 0, 10.0) for i in range(1, 6)],
            },
            {
                "project": "p_bad", "provider": "prov", "model": "m",
                "started_at": "2026-01-02T00:00:00",
                "summary": {"ok": 3, "timeout": 2, "error": 0, "rate_limited": 0},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": ([run(i, 0, 999.0) for i in range(1, 4)]
                         + [run(i, 1, 450.0) for i in range(4, 6)]),
            },
        ]

        _, data = self._build_index_data(
            reports, unstable=[("prov", "m", "таймауты на p_bad")])

        ranking = data["model_ranking"]
        row = next((r for r in ranking if r["key"] == "prov/m"), None)
        self.assertIsNotNone(row, "unstable-модель должна остаться в рейтинге")
        self.assertEqual(row["status"], "unstable")
        # метрики только по чистому p_ok: 5 успешных, avg по elapsed=10 (не 999)
        self.assertEqual(row["successful_run_count"], 5)
        self.assertEqual(row["avg_elapsed"], 10.0)
        self.assertEqual(row["unstable_projects"], ["p_bad"])
        self.assertEqual(row["unstable_reason"], "таймауты на p_bad")

    def test_build_index_unmarked_model_with_failure_excluded_from_ranking(self):
        # Контроль: та же грязная модель БЕЗ метки unstable — в рейтинг не попадает
        # (прежнее поведение has_failures).
        reports = [
            {
                "project": "p_ok", "provider": "prov", "model": "m",
                "started_at": "2026-01-01T00:00:00",
                "summary": {"ok": 5, "timeout": 0, "error": 0, "rate_limited": 0},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": [{"index": i, "code": 0, "elapsed": 10.0}
                         for i in range(1, 6)],
            },
            {
                "project": "p_bad", "provider": "prov", "model": "m",
                "started_at": "2026-01-02T00:00:00",
                "summary": {"ok": 3, "timeout": 2, "error": 0, "rate_limited": 0},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": ([{"index": i, "code": 0, "elapsed": 10.0}
                          for i in range(1, 4)]
                         + [{"index": i, "code": 1, "elapsed": 450.0}
                            for i in range(4, 6)]),
            },
        ]

        _, data = self._build_index_data(reports)  # без unstable-метки

        keys = [r["key"] for r in data["model_ranking"]]
        self.assertNotIn("prov/m", keys)

    def test_refresh_cache_clears_cached_db_models_after_successful_write(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                with conn:
                    conn.execute(
                        """
                        INSERT INTO openrouter_cache_meta (id, fetched_at)
                        VALUES (1, 0)
                        """,
                    )
                    conn.execute(
                        """
                        INSERT INTO openrouter_cache (model_id, prompt, completion)
                        VALUES ('old/model', '1', '2')
                        """,
                    )
            finally:
                conn.close()

            class FakeModels:
                def list(self):
                    return SimpleNamespace(data=[
                        SimpleNamespace(
                            id="new/model",
                            pricing=SimpleNamespace(prompt="3", completion="4"),
                        ),
                    ])

            class FakeOpenRouter:
                def __init__(self, *args, **kwargs):
                    self.models = FakeModels()

                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

            original_connect = pricing.connect
            original_openrouter = pricing.OpenRouter
            try:
                pricing.connect = lambda: db.connect(db_path)
                pricing.OpenRouter = FakeOpenRouter
                pricing._read_cached_models.cache_clear()
                pricing.refresh_cache.cache_clear()

                self.assertIn("old/model", pricing._read_cached_models())
                pricing.refresh_cache()
                cached = pricing._read_cached_models()
            finally:
                pricing.connect = original_connect
                pricing.OpenRouter = original_openrouter
                pricing._read_cached_models.cache_clear()
                pricing.refresh_cache.cache_clear()

        self.assertNotIn("old/model", cached)
        self.assertEqual(cached["new/model"], {"prompt": "3", "completion": "4"})


    def test_run_copy_propagates_reason_from_probe_result(self):
        # issue #31: причина исхода из SessionProbeResult должна доходить до
        # результата run_copy, а не теряться вместе с code/usage.
        orig_ensure = benchmark_report.ensure_server_running
        orig_probe_session = benchmark_report.probe_session
        try:
            benchmark_report.ensure_server_running = lambda work_dir, port, status: True
            benchmark_report.probe_session = lambda **kwargs: runtime.SessionProbeResult(
                code=3,
                reason="HTTP 429: Too Many Requests",
                usage=None,
                rate_limited=True,
            )
            with tempfile.TemporaryDirectory() as td:
                result = benchmark_report.run_copy(
                    index=1,
                    work_dir=Path(td),
                    port=4096,
                    task="task",
                    model="m",
                    provider="p",
                    agent="bench_coder",
                    timeout=1,
                )
        finally:
            benchmark_report.ensure_server_running = orig_ensure
            benchmark_report.probe_session = orig_probe_session

        self.assertEqual(result["code"], 3)
        self.assertEqual(result["reason"], "HTTP 429: Too Many Requests")

    def test_run_copy_error_branches_set_human_readable_reason(self):
        # Ветки-ошибки (сбой сессии / сервер не поднялся) тоже не должны быть
        # беззвучными: reason заполняется человекочитаемым текстом.
        orig_ensure = benchmark_report.ensure_server_running
        orig_probe_session = benchmark_report.probe_session
        try:
            benchmark_report.ensure_server_running = lambda work_dir, port, status: True

            def crash(**kwargs):
                raise RuntimeError("simulated crash")

            benchmark_report.probe_session = crash
            with tempfile.TemporaryDirectory() as td:
                crashed = benchmark_report.run_copy(
                    index=1, work_dir=Path(td), port=4096, task="t",
                    model="m", provider="p", agent="bench_coder", timeout=1,
                )

            benchmark_report.ensure_server_running = (
                lambda work_dir, port, status: False)
            with tempfile.TemporaryDirectory() as td:
                not_ready = benchmark_report.run_copy(
                    index=1, work_dir=Path(td), port=4096, task="t",
                    model="m", provider="p", agent="bench_coder", timeout=1,
                )
        finally:
            benchmark_report.ensure_server_running = orig_ensure
            benchmark_report.probe_session = orig_probe_session

        self.assertEqual(crashed["code"], 2)
        self.assertIn("simulated crash", crashed["reason"])
        self.assertEqual(not_ready["code"], 2)
        self.assertIn("не поднялся", not_ready["reason"])

    def test_run_benchmark_stores_reason_in_raw_json(self):
        # reason должен сохраниться в reports.raw_json в САНИРОВАННОМ виде (без
        # сырого тела провайдера/секретов), схема таблицы runs не меняется. Вторая
        # копия возвращает старый словарь БЕЗ reason — обратная совместимость.
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            work_dir = Path(td) / "work"
            work_dir.mkdir()
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
            finally:
                conn.close()

            def fake_run_copy(index, *args, **kwargs):
                base = {
                    "index": index, "port": 4096 + index,
                    "dir": str(work_dir), "elapsed": 0.1, "usage": None,
                }
                if index == 1:
                    # Причина с секрето-подобной строкой — НЕ должна попасть в отчёт.
                    return {**base, "code": 3,
                            "reason": "HTTP 429: quota exceeded key sk-SECRET1234567890ABCD"}
                # Старый формат: словарь без ключа "reason".
                return {**base, "code": 0}

            originals = {
                "connect": benchmark_report.connect,
                "prepare": benchmark_report.prepare_work_dirs,
                "run_copy": benchmark_report.run_copy,
                "get_pricing": benchmark_report.get_pricing,
                "collect": benchmark_report.collect_report_artifacts,
                "cleanup": benchmark_report.cleanup_collected_artifacts,
            }
            try:
                benchmark_report.connect = lambda: db.connect(db_path)
                benchmark_report.prepare_work_dirs = lambda *args: [work_dir, work_dir]
                benchmark_report.run_copy = fake_run_copy
                benchmark_report.get_pricing = lambda provider, model: {
                    "prompt_per_1m": 0.0,
                    "completion_per_1m": 0.0,
                }
                benchmark_report.collect_report_artifacts = lambda results: SimpleNamespace(
                    artifacts=[], summary=lambda: {},
                )
                benchmark_report.cleanup_collected_artifacts = lambda collection: None

                with contextlib.redirect_stderr(io.StringIO()):
                    benchmark_report.run_benchmark(SimpleNamespace(
                        project="ad_hoc", file=None, task="task",
                        provider="provider", model="model", copies=2,
                        base_port=4096, agent="bench_coder", timeout=1,
                        force_excluded=False,
                    ))

                conn = db.connect(db_path)
                try:
                    raw_json = conn.execute(
                        "SELECT raw_json FROM reports WHERE project = 'ad_hoc'",
                    ).fetchone()["raw_json"]
                    runs_cols = [r[1] for r in conn.execute(
                        "PRAGMA table_info(runs)").fetchall()]
                finally:
                    conn.close()
            finally:
                benchmark_report.connect = originals["connect"]
                benchmark_report.prepare_work_dirs = originals["prepare"]
                benchmark_report.run_copy = originals["run_copy"]
                benchmark_report.get_pricing = originals["get_pricing"]
                benchmark_report.collect_report_artifacts = originals["collect"]
                benchmark_report.cleanup_collected_artifacts = originals["cleanup"]

        report = json.loads(raw_json)
        runs = {run["index"]: run for run in report["runs"]}
        # Санированная причина: каркас сохранён, секрет вырезан.
        self.assertEqual(runs[1]["reason"], "HTTP 429: превышен лимит/квота")
        self.assertNotIn("sk-SECRET1234567890ABCD", raw_json)
        # Старый run без reason собрался без падения, reason стал None.
        self.assertIsNone(runs[2]["reason"])
        # Причина в raw_json, но НЕ в SQL-индексе runs (схема не мигрирует).
        self.assertNotIn("reason", runs_cols)

    def test_load_project_logs_db_error_instead_of_masking(self):
        # issue #31 / #21: ошибка БД не должна молча выглядеть как «проект не
        # найден» — она логируется отдельно и отличается от отсутствующего проекта.
        orig_connect = benchmark_report.connect
        try:
            def boom():
                raise RuntimeError("db is locked")

            benchmark_report.connect = boom
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                result = benchmark_report.load_project("whatever")
        finally:
            benchmark_report.connect = orig_connect

        self.assertIs(result, benchmark_report.PROJECT_LOAD_ERROR)
        self.assertIn("не удалось прочитать проект", stderr.getvalue())
        self.assertIn("db is locked", stderr.getvalue())

    def test_run_benchmark_does_not_print_not_found_after_db_error(self):
        orig_load_project = benchmark_report.load_project
        orig_ensure_model_is_allowed = benchmark_report.ensure_model_is_allowed
        try:
            def db_error(project):
                print("warning: db failed; продолжаю как ad-hoc", file=sys.stderr)
                return benchmark_report.PROJECT_LOAD_ERROR

            def stop_before_work(*args, **kwargs):
                raise RuntimeError("stop before work")

            benchmark_report.load_project = db_error
            benchmark_report.ensure_model_is_allowed = stop_before_work
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                with self.assertRaisesRegex(RuntimeError, "stop before work"):
                    benchmark_report.run_benchmark(SimpleNamespace(
                        project="whatever", file=None, task="task",
                        provider="provider", model="model", copies=1,
                        base_port=4096, agent="bench_coder", timeout=1,
                        force_excluded=False,
                    ))
        finally:
            benchmark_report.load_project = orig_load_project
            benchmark_report.ensure_model_is_allowed = orig_ensure_model_is_allowed

        warning = stderr.getvalue()
        self.assertIn("db failed", warning)
        self.assertNotIn("не найден в библиотеке", warning)

    def test_public_reason_redacts_secrets_keeps_category(self):
        # Codex adversarial review (#32): публичная причина не должна выпускать
        # сырой текст провайдера/секреты, но обязана сохранять код+категорию.
        out = runtime.public_reason(
            "HTTP 401: invalid api key sk-ABCDEF1234567890XYZ unauthorized")
        self.assertEqual(out, "HTTP 401: ошибка авторизации")
        self.assertNotIn("sk-ABCDEF1234567890XYZ", out)

        billing = runtime.public_reason(
            "HTTP 402: insufficient credits, billing https://p.co/pay?token=abc "
            "user@example.com")
        self.assertEqual(billing, "HTTP 402: проблема аккаунта/биллинга")
        for secret in ("token=abc", "user@example.com", "https://"):
            self.assertNotIn(secret, billing)

        limit = runtime.public_reason("HTTP 429: Too Many Requests | quota for org")
        self.assertEqual(limit, "HTTP 429: превышен лимит/квота")

    def test_public_reason_unknown_category_drops_provider_body(self):
        # Нераспознанная категория: код сохраняется, но тело провайдера не
        # публикуется вообще. Скраббер — не allowlist.
        out = runtime.public_reason(
            "HTTP 500: Internal error password=hunter2 key=short "
            "org=acme user_id=42 request=abc123")
        self.assertEqual(out, "HTTP 500: ошибка провайдера")
        for secret in ("hunter2", "short", "acme", "user_id", "abc123"):
            self.assertNotIn(secret, out)

    def test_public_reason_preserves_local_failure_reason(self):
        # Локальные сбои не должны выглядеть как «ошибка провайдера», но текст всё
        # равно проходит через публичный скраббер.
        out = runtime.public_reason(
            "сбой запуска сервера: FileNotFoundError: No such file: 'opencode' "
            "sk-LOCALSECRET1234567890")
        self.assertIn("сбой запуска сервера", out)
        self.assertIn("opencode", out)
        self.assertNotIn("sk-LOCALSECRET1234567890", out)
        self.assertNotEqual(out, "ошибка провайдера")

        self.assertEqual(
            runtime.public_reason("opencode serve не поднялся"),
            "opencode serve не поднялся",
        )
        forbidden = runtime.public_reason(
            "сбой копии: PermissionError: [Errno 13] Permission denied: "
            "'forbidden_dir'")
        self.assertIn("сбой копии", forbidden)
        self.assertIn("forbidden_dir", forbidden)
        self.assertNotEqual(forbidden, "ошибка авторизации")

    def test_public_reason_passthrough_and_none(self):
        # Success → None; таймаут без provider-текста проходит, но приклеенный
        # provider-tail с секретом отбрасывается.
        self.assertIsNone(runtime.public_reason(None))
        self.assertIsNone(runtime.public_reason(""))
        self.assertEqual(runtime.public_reason("нет ответа за 60с"), "нет ответа за 60с")
        tailed = runtime.public_reason(
            "нет ответа за 60с | ERROR at https://x.io/cb?key=SEKRET")
        self.assertEqual(tailed, "нет ответа за 60с")
        self.assertNotIn("SEKRET", tailed)


class Issue23Tests(unittest.TestCase):
    """TDD-тесты для фиксов issue #23.

    Проверяют два рефакторинга в db.py:
    1. db.session() — контекстный менеджер (connect + init_schema + auto-close).
    2. _EXCLUSION_COLUMNS — константа для повторяющегося списка колонок.
    """

    # --- db.session() context manager -----------------------------------------

    def test_session_opens_connection_and_initializes_schema(self):
        # session() должен открыть соединение и создать все таблицы.
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            with db.session(db_path) as conn:
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn("reports", tables)
                self.assertIn("runs", tables)
                self.assertIn("model_exclusions", tables)
                self.assertIn("model_unstability", tables)
                self.assertIn("file_blobs", tables)

    def test_session_returns_writable_connection(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            with db.session(db_path) as conn:
                row = db.block_model_exclusion(conn, "prov", "mdl", "test")
                self.assertEqual(row["provider"], "prov")
                fetched = db.get_model_exclusion(conn, "prov", "mdl")
                self.assertEqual(fetched["reason"], "test")

    def test_session_auto_closes_on_normal_exit(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            with db.session(db_path) as conn:
                pass
            with self.assertRaises(Exception):
                conn.execute("SELECT 1")

    def test_session_auto_closes_on_exception(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            conn_ref = None
            with self.assertRaises(RuntimeError):
                with db.session(db_path) as conn:
                    conn_ref = conn
                    raise RuntimeError("boom")
            with self.assertRaises(Exception):
                conn_ref.execute("SELECT 1")

    def test_session_creates_parent_directory(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "sub" / "dir" / "test.db"
            with db.session(db_path) as conn:
                tables = {
                    row["name"]
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertIn("reports", tables)
            self.assertTrue(db_path.exists())

    # --- _EXCLUSION_COLUMNS constant -----------------------------------------

    def test_exclusion_columns_constant_exists(self):
        self.assertTrue(
            hasattr(db, "_EXCLUSION_COLUMNS"),
            "db._EXCLUSION_COLUMNS not defined",
        )

    def test_exclusion_columns_has_expected_columns(self):
        expected = ("provider", "model", "reason", "active", "created_at", "updated_at")
        self.assertEqual(db._EXCLUSION_COLUMNS, expected)

    def test_exclusion_columns_is_tuple(self):
        self.assertIsInstance(db._EXCLUSION_COLUMNS, tuple)

    # --- Exclusion/unstable functions return rows with constant column names ---

    def test_exclusion_functions_return_constant_columns(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                with conn:
                    blocked = db.block_model_exclusion(
                        conn, "p", "m", "reason text")
                col_names = tuple(blocked.keys())
                self.assertEqual(col_names, db._EXCLUSION_COLUMNS)

                fetched = db.get_model_exclusion(conn, "p", "m")
                self.assertEqual(tuple(fetched.keys()), db._EXCLUSION_COLUMNS)

                listed = db.list_model_exclusions(conn)
                self.assertEqual(len(listed), 1)
                self.assertEqual(tuple(listed[0].keys()), db._EXCLUSION_COLUMNS)
            finally:
                conn.close()

    def test_unstable_functions_return_constant_columns(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                with conn:
                    marked = db.mark_model_unstable(
                        conn, "p", "m", "unstable reason")
                col_names = tuple(marked.keys())
                self.assertEqual(col_names, db._EXCLUSION_COLUMNS)

                fetched = db.get_model_unstable(conn, "p", "m")
                self.assertEqual(tuple(fetched.keys()), db._EXCLUSION_COLUMNS)

                listed = db.list_model_unstable(conn)
                self.assertEqual(len(listed), 1)
                self.assertEqual(tuple(listed[0].keys()), db._EXCLUSION_COLUMNS)
            finally:
                conn.close()


class PricingUsageTests(unittest.TestCase):
    """Tests for pricing.empty_pricing, _resolve_catalog_id, and usage helpers."""

    # --- pricing.empty_pricing ---

    def test_empty_pricing_without_note(self):
        result = pricing.empty_pricing()
        self.assertEqual(result, {"prompt_per_1m": None, "completion_per_1m": None})
        self.assertNotIn("note", result)

    def test_empty_pricing_with_note(self):
        result = pricing.empty_pricing(note="custom note")
        self.assertEqual(result["prompt_per_1m"], None)
        self.assertEqual(result["completion_per_1m"], None)
        self.assertEqual(result["note"], "custom note")

    def test_empty_pricing_note_none_same_as_without(self):
        result = pricing.empty_pricing(note=None)
        self.assertEqual(result, {"prompt_per_1m": None, "completion_per_1m": None})
        self.assertNotIn("note", result)

    # --- pricing._resolve_catalog_id ---

    def test_resolve_catalog_id_alias_match(self):
        cache = {"vendor/model-a": {"prompt": "1", "completion": "2"}}
        aliases = {"prov/m": "vendor/model-a"}
        result = pricing._resolve_catalog_id(cache, "prov/m", "m", aliases)
        self.assertEqual(result, "vendor/model-a")

    def test_resolve_catalog_id_exact_key_match(self):
        cache = {"prov/model": {"prompt": "1", "completion": "2"}}
        result = pricing._resolve_catalog_id(cache, "prov/model", "model", {})
        self.assertEqual(result, "prov/model")

    def test_resolve_catalog_id_paid_preferred_over_free(self):
        cache = {
            "vendor/m:free": {"prompt": "0", "completion": "0"},
            "vendor/m": {"prompt": "1", "completion": "2"},
        }
        result = pricing._resolve_catalog_id(cache, "prov/m", "m", {})
        self.assertEqual(result, "vendor/m")

    def test_resolve_catalog_id_no_match_returns_none(self):
        cache = {"other/model": {"prompt": "1", "completion": "2"}}
        result = pricing._resolve_catalog_id(cache, "prov/m", "m", {})
        self.assertIsNone(result)

    # --- usage.as_token ---

    def test_as_token_int(self):
        self.assertEqual(usage_metrics.as_token(42), 42)

    def test_as_token_float_truncation(self):
        self.assertEqual(usage_metrics.as_token(1.7), 1)

    def test_as_token_str(self):
        self.assertEqual(usage_metrics.as_token("123"), 123)

    def test_as_token_none(self):
        self.assertIsNone(usage_metrics.as_token(None))

    def test_as_token_bool_filtered(self):
        self.assertIsNone(usage_metrics.as_token(True))

    def test_as_token_nan(self):
        self.assertIsNone(usage_metrics.as_token(float("nan")))

    def test_as_token_invalid_str(self):
        self.assertIsNone(usage_metrics.as_token("abc"))

    # --- usage.as_money ---

    def test_as_money_float(self):
        self.assertEqual(usage_metrics.as_money(1.5), 1.5)

    def test_as_money_int(self):
        self.assertEqual(usage_metrics.as_money(5), 5.0)

    def test_as_money_str(self):
        self.assertEqual(usage_metrics.as_money("2.5"), 2.5)

    def test_as_money_none(self):
        self.assertIsNone(usage_metrics.as_money(None))

    def test_as_money_bool_filtered(self):
        self.assertIsNone(usage_metrics.as_money(True))

    def test_as_money_inf(self):
        self.assertIsNone(usage_metrics.as_money(float("inf")))

    # --- usage.field ---

    def test_field_dict(self):
        self.assertEqual(usage_metrics.field({"a": 1}, "a"), 1)

    def test_field_object_attr(self):
        obj = SimpleNamespace(x=42)
        self.assertEqual(usage_metrics.field(obj, "x"), 42)

    def test_field_missing_key(self):
        self.assertIsNone(usage_metrics.field({"a": 1}, "b"))
        self.assertIsNone(usage_metrics.field(SimpleNamespace(), "missing"))

    # --- usage.format_tokens ---

    def test_format_tokens_number(self):
        self.assertEqual(usage_metrics.format_tokens(1000), "1,000")

    def test_format_tokens_none(self):
        self.assertEqual(usage_metrics.format_tokens(None), "N/A")

    def test_format_tokens_zero(self):
        self.assertEqual(usage_metrics.format_tokens(0), "0")

    # --- usage.merge_usages ---

    def test_merge_usages_sums_tokens(self):
        u1 = usage_metrics.Usage(input_tokens=100, output_tokens=50)
        u2 = usage_metrics.Usage(input_tokens=200, output_tokens=30)
        merged = usage_metrics.merge_usages([u1, u2])
        self.assertEqual(merged.input_tokens, 300)
        self.assertEqual(merged.output_tokens, 80)

    def test_merge_usages_empty_list(self):
        self.assertIsNone(usage_metrics.merge_usages([]))

    # --- usage.summarize_usages ---

    def test_summarize_usages_correct_totals(self):
        u1 = usage_metrics.Usage(
            input_tokens=100, output_tokens=50,
            estimated_cost_usd=0.01,
        )
        u2 = usage_metrics.Usage(
            input_tokens=200, output_tokens=30,
            estimated_cost_usd=0.02,
        )
        summary = usage_metrics.summarize_usages([u1, u2])
        self.assertEqual(summary["input_tokens"], 300)
        self.assertEqual(summary["output_tokens"], 80)
        self.assertAlmostEqual(summary["estimated_cost_usd"], 0.03)
        self.assertEqual(summary["runs_with_usage"], 2)
        self.assertEqual(summary["runs_with_estimated_cost"], 2)

    def test_summarize_usages_all_none(self):
        summary = usage_metrics.summarize_usages([None, None])
        self.assertIsNone(summary["input_tokens"])
        self.assertIsNone(summary["output_tokens"])
        self.assertIsNone(summary["total_tokens"])
        self.assertIsNone(summary["estimated_cost_usd"])
        self.assertEqual(summary["runs_with_usage"], 0)
        self.assertEqual(summary["runs_with_estimated_cost"], 0)


class ArtifactsDbRuntimeTests(unittest.TestCase):
    """Tests for artifacts, db, and opencode_runtime functions with no prior coverage."""

    # --- artifacts._is_excluded_file ---

    def test_is_excluded_file_ds_store(self):
        self.assertTrue(artifacts._is_excluded_file(Path(".DS_Store")))

    def test_is_excluded_file_pyc(self):
        self.assertTrue(artifacts._is_excluded_file(Path("mod.pyc")))

    def test_is_excluded_file_report_json(self):
        self.assertTrue(artifacts._is_excluded_file(Path("report.json")))

    def test_is_excluded_file_normal_file(self):
        self.assertFalse(artifacts._is_excluded_file(Path("hello.py")))

    def test_is_excluded_file_run_log(self):
        self.assertFalse(artifacts._is_excluded_file(Path("run.log")))

    # --- artifacts.collect_run_artifacts ---

    def test_collect_run_artifacts_empty_dir(self):
        with tempfile.TemporaryDirectory() as td:
            collection = artifacts.collect_run_artifacts(0, Path(td))
            self.assertEqual(collection.artifacts, [])
            self.assertEqual(collection.errors, [])

    def test_collect_run_artifacts_with_run_log(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "run.log").write_text("log line\n", encoding="utf-8")
            collection = artifacts.collect_run_artifacts(0, Path(td))
            self.assertEqual(len(collection.artifacts), 1)
            self.assertEqual(collection.artifacts[0].kind, "log")
            self.assertEqual(collection.artifacts[0].path, "run.log")

    def test_collect_run_artifacts_excludes_ds_store(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".DS_Store").write_bytes(b"\x00")
            collection = artifacts.collect_run_artifacts(0, Path(td))
            self.assertEqual(collection.artifacts, [])
            paths = [str(p) for p in collection.trash_paths]
            self.assertTrue(any(".DS_Store" in p for p in paths))

    def test_collect_run_artifacts_nonexistent_dir(self):
        collection = artifacts.collect_run_artifacts(0, Path("/nonexistent/path/xyz"))
        self.assertEqual(collection.artifacts, [])
        self.assertEqual(len(collection.errors), 1)
        self.assertIn("missing", collection.errors[0])

    # --- artifacts._prune_empty_dirs ---

    def test_prune_empty_dirs_removes_nested_empty(self):
        with tempfile.TemporaryDirectory() as td:
            nested = Path(td) / "a" / "b"
            nested.mkdir(parents=True)
            artifacts._prune_empty_dirs(Path(td))
            self.assertFalse((Path(td) / "a").exists())

    def test_prune_empty_dirs_keeps_nonempty(self):
        with tempfile.TemporaryDirectory() as td:
            subdir = Path(td) / "keep"
            subdir.mkdir()
            (subdir / "file.txt").write_text("data", encoding="utf-8")
            artifacts._prune_empty_dirs(Path(td))
            self.assertTrue(subdir.exists())

    def test_prune_empty_dirs_nonexistent_root_no_error(self):
        artifacts._prune_empty_dirs(Path("/nonexistent/root/abc"))

    # --- artifacts.cleanup_collected_artifacts ---

    def test_cleanup_deletes_existing_artifact_file(self):
        with tempfile.TemporaryDirectory() as td:
            f = Path(td) / "run.log"
            f.write_text("log", encoding="utf-8")
            art = artifacts.RunArtifact(
                run_idx=0, path="run.log", kind="log",
                size_bytes=3, sha256="abc", content=b"log",
                source_path=f,
            )
            collection = artifacts.ArtifactCollection(
                artifacts=[art], trash_paths=[], errors=[],
            )
            artifacts.cleanup_collected_artifacts(collection)
            self.assertFalse(f.exists())

    def test_cleanup_already_deleted_file_no_error(self):
        f = Path("/nonexistent/deleted_file.log")
        art = artifacts.RunArtifact(
            run_idx=0, path="deleted_file.log", kind="log",
            size_bytes=0, sha256="x", content=b"",
            source_path=f,
        )
        collection = artifacts.ArtifactCollection(
            artifacts=[art], trash_paths=[], errors=[],
        )
        # FileNotFoundError caught internally — no exception raised.
        artifacts.cleanup_collected_artifacts(collection)

    # --- db.split_model_ref ---

    def test_split_model_ref_normal(self):
        self.assertEqual(db.split_model_ref("prov/model"), ("prov", "model"))

    def test_split_model_ref_nested_model(self):
        self.assertEqual(
            db.split_model_ref("prov/model/sub"), ("prov", "model/sub"),
        )

    def test_split_model_ref_no_slash_raises(self):
        with self.assertRaises(ValueError):
            db.split_model_ref("nomodel")

    def test_split_model_ref_empty_parts_raises(self):
        with self.assertRaises(ValueError):
            db.split_model_ref(" / ")

    # --- db.read_artifact ---

    def test_read_artifact_existing(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                content = b"hello artifact"
                sha = hashlib.sha256(content).hexdigest()
                report = {
                    "project": "p", "provider": "v", "model": "m",
                    "started_at": "2026-01-01T00:00:00",
                    "summary": {"ok": 1, "timeout": 0, "error": 0},
                    "runs": [{"index": 0, "port": 4000, "dir": "/x",
                              "status": "ok", "code": 0, "elapsed": 1.0}],
                }
                with conn:
                    rid = db.upsert_report(
                        conn, report, "r.json", json.dumps(report),
                    )
                    art = artifacts.RunArtifact(
                        run_idx=0, path="hello.py", kind="agent_file",
                        size_bytes=len(content), sha256=sha,
                        content=content, source_path=Path("/x/hello.py"),
                    )
                    db.replace_report_artifacts(conn, rid, [art])
                result = db.read_artifact(conn, rid, 0, "hello.py")
                self.assertEqual(result, content)
            finally:
                conn.close()

    def test_read_artifact_nonexistent_raises(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                with self.assertRaises(FileNotFoundError):
                    db.read_artifact(conn, 99999, 0, "nope.txt")
            finally:
                conn.close()

    # --- runtime.sanitize_name ---

    def test_sanitize_name_collapses_dots(self):
        # Two dots collapse to one (not removed entirely).
        self.assertEqual(runtime.sanitize_name("a..b"), "a.b")

    def test_sanitize_name_leading_dot_stripped(self):
        self.assertEqual(runtime.sanitize_name(".hidden"), "hidden")

    def test_sanitize_name_empty_fallback(self):
        self.assertEqual(runtime.sanitize_name(""), "x")

    def test_sanitize_name_normal_unchanged(self):
        self.assertEqual(runtime.sanitize_name("normal-name"), "normal-name")

    # --- runtime.base_url ---

    def test_base_url_port_4000(self):
        self.assertEqual(runtime.base_url(4000), "http://127.0.0.1:4000")

    # --- runtime._scrub_secrets ---

    def test_scrub_secrets_bearer_token(self):
        result = runtime._scrub_secrets("Bearer sk-abc123")
        self.assertNotIn("sk-abc123", result)
        self.assertIn("[скрыто]", result)

    def test_scrub_secrets_url(self):
        result = runtime._scrub_secrets("https://example.com/path")
        self.assertNotIn("example.com", result)
        self.assertIn("[скрыто]", result)

    def test_scrub_secrets_clean_text_unchanged(self):
        text = "clean text without secrets"
        self.assertEqual(runtime._scrub_secrets(text), text)


class Issue37CharacterizationTests(unittest.TestCase):
    """Characterization tests (issue #37): фиксируют текущее поведение функций,
    которые планирует рефакторить PR #33 (issue #23).

    Порядок: пишем на main → прогоняем (зелёные) → cherry-pick на ветку
    рефакторинга → если снова зелёные — поведение сохранено, можно мерджить.
    """

    # --- pricing._fmt_usd ----------------------------------------------------

    def test_fmt_usd_below_threshold(self):
        self.assertEqual(pricing._fmt_usd(0.05), "$0.0500")

    def test_fmt_usd_above_threshold(self):
        self.assertEqual(pricing._fmt_usd(0.50), "$0.50")

    def test_fmt_usd_at_threshold(self):
        self.assertEqual(pricing._fmt_usd(0.1), "$0.10")

    def test_fmt_usd_zero(self):
        self.assertEqual(pricing._fmt_usd(0.0), "$0.0000")

    def test_fmt_usd_large_value(self):
        self.assertEqual(pricing._fmt_usd(15.0), "$15.00")

    # --- pricing.format_price_display ----------------------------------------

    def test_format_price_display_free(self):
        result = pricing.format_price_display(
            {"prompt_per_1m": 0.0, "completion_per_1m": 0.0})
        self.assertEqual(result, "Free")

    def test_format_price_display_paid(self):
        result = pricing.format_price_display(
            {"prompt_per_1m": 0.5, "completion_per_1m": 1.5})
        self.assertIn("$0.50", result)
        self.assertIn("$1.50", result)

    def test_format_price_display_cheap(self):
        result = pricing.format_price_display(
            {"prompt_per_1m": 0.05, "completion_per_1m": 0.03})
        self.assertIn("$0.0500", result)
        self.assertIn("$0.0300", result)

    def test_format_price_display_missing_prices(self):
        self.assertEqual(pricing.format_price_display({}), "N/A")

    def test_format_price_display_na_with_note(self):
        result = pricing.format_price_display(
            {"prompt_per_1m": None, "note": "no data"})
        self.assertEqual(result, "N/A (no data)")

    # --- usage.format_usd_cost -----------------------------------------------

    def test_format_usd_cost_zero(self):
        self.assertEqual(usage_metrics.format_usd_cost(0), "$0")

    def test_format_usd_cost_below_threshold(self):
        self.assertEqual(usage_metrics.format_usd_cost(0.001), "$0.001000")

    def test_format_usd_cost_above_threshold(self):
        self.assertEqual(usage_metrics.format_usd_cost(0.05), "$0.0500")

    def test_format_usd_cost_at_threshold(self):
        self.assertEqual(usage_metrics.format_usd_cost(0.01), "$0.0100")

    def test_format_usd_cost_none(self):
        self.assertEqual(usage_metrics.format_usd_cost(None), "N/A")

    def test_format_usd_cost_nan(self):
        self.assertEqual(usage_metrics.format_usd_cost(float("nan")), "N/A")

    def test_format_usd_cost_inf(self):
        self.assertEqual(usage_metrics.format_usd_cost(float("inf")), "N/A")

    # --- artifacts.collect_report_artifacts ----------------------------------

    def test_collect_report_artifacts_empty_list(self):
        col = artifacts.collect_report_artifacts([])
        self.assertEqual(len(col.artifacts), 0)
        self.assertEqual(len(col.trash_paths), 0)
        self.assertEqual(len(col.errors), 0)

    def test_collect_report_artifacts_bad_result(self):
        col = artifacts.collect_report_artifacts([{"index": None}])
        self.assertEqual(len(col.artifacts), 0)
        self.assertEqual(len(col.errors), 1)
        self.assertIn("bad run result", col.errors[0])

    def test_collect_report_artifacts_missing_dir(self):
        col = artifacts.collect_report_artifacts(
            [{"index": 0, "dir": "/nonexistent/path/abc"}])
        self.assertEqual(len(col.artifacts), 0)
        self.assertTrue(len(col.errors) > 0)

    def test_collect_report_artifacts_with_files(self):
        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td)
            (work_dir / "run.log").write_text("ok", encoding="utf-8")
            col = artifacts.collect_report_artifacts(
                [{"index": 0, "dir": str(work_dir)}])
            self.assertTrue(len(col.artifacts) > 0)
            self.assertEqual(col.artifacts[0].path, "run.log")


if __name__ == "__main__":
    unittest.main()

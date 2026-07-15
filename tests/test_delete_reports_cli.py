"""scripts/delete_reports.py — выборочное ручное удаление отчётов (issue #121).

4 режима: отчёт целиком (--report-id), одна копия (--report-id + --run-idx),
все результаты модели (provider/model [+ --project]), проект целиком (--project).
Dry-run по умолчанию ничего не удаляет и печатает счётчики; --apply удаляет.
"""

import contextlib
import hashlib
import io
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import db
import scripts.delete_reports as delete_reports
from artifacts import RunArtifact, write_run_active_marker


def _make_report(provider, model, project, ok, fail, started_at, fail_code=1):
    """Полный report-dict: `ok` успешных + `fail` фейловых прогонов."""
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
        "summary": {"ok": ok, "timeout": fail, "error": 0},
        "pricing": {}, "usage_summary": {}, "artifact_summary": {},
        "runs": runs,
    }


def _artifact(run_idx, name, content: bytes) -> RunArtifact:
    return RunArtifact(
        run_idx=run_idx, path=name, kind="agent_file", size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(), content=content,
        source_path=Path(f"/x/{name}"),
    )


def _upsert(conn, report, rel_path, artifacts=None):
    with conn:
        return db.upsert_report(conn, report, rel_path, json.dumps(report),
                                artifacts=artifacts)


class DeleteReportsCliTests(unittest.TestCase):
    def _connect(self, td):
        conn = db.connect(Path(td) / "main.db")
        db.init_schema(conn)
        self.addCleanup(conn.close)
        return conn

    def _run(self, conn, **kwargs):
        """delete_reports.run с перехваченным stdout; возвращает (rc, stdout)."""
        rc, out, _err = self._run_captured(conn, **kwargs)
        return rc, out

    def _run_captured(self, conn, **kwargs):
        """То же, но возвращает также stderr для проверок ошибок безопасности."""
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = delete_reports.run(conn, **kwargs)
        return rc, out.getvalue(), err.getvalue()

    # --- режим 1: отчёт целиком (--report-id) --------------------------------

    def test_report_id_mode_dry_run_then_apply(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._connect(td)
            report = _make_report("prov", "m", "p1", 2, 0, "2026-01-01T00:00:00")
            rid = _upsert(conn, report, "data/result/r1.json",
                          artifacts=[_artifact(1, "a.py", b"one"),
                                     _artifact(2, "b.py", b"two")])

            rc, out = self._run(conn, report_id=rid)  # dry-run по умолчанию
            self.assertEqual(rc, 0)
            self.assertIn("dry-run", out)
            self.assertIn("отчётов=1", out)
            self.assertIn("runs=2", out)
            self.assertIn("артефактов=2", out)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM reports").fetchone()[0], 1,
                "dry-run не должен ничего удалять")

            rc, _ = self._run(conn, report_id=rid, apply=True)
            self.assertEqual(rc, 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM reports").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM runs").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM run_artifacts").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM file_blobs").fetchone()[0], 0,
                "осиротевшие блобы подметаются")

    def test_report_id_mode_missing_report_fails(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._connect(td)
            rc, _ = self._run(conn, report_id=999)
            self.assertNotEqual(rc, 0)

    # --- режим 2: одна копия (--report-id + --run-idx) ------------------------

    def test_run_idx_mode_rebuilds_raw_json_of_survivor(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._connect(td)
            report = _make_report("prov", "m", "p1", 1, 1, "2026-01-01T00:00:00")
            rid = _upsert(conn, report, "data/result/r1.json",
                          artifacts=[_artifact(1, "ok.py", b"ok"),
                                     _artifact(2, "bad.py", b"bad")])

            rc, out = self._run(conn, report_id=rid, run_idx=2)  # dry-run
            self.assertEqual(rc, 0)
            self.assertIn("dry-run", out)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM runs").fetchone()[0], 2)

            rc, _ = self._run(conn, report_id=rid, run_idx=2, apply=True)
            self.assertEqual(rc, 0)
            # копия 2 и её артефакты удалены, копия 1 жива
            self.assertEqual([r["idx"] for r in conn.execute(
                "SELECT idx FROM runs WHERE report_id=?", (rid,))], [1])
            self.assertEqual([a["path"] for a in conn.execute(
                "SELECT path FROM run_artifacts WHERE report_id=?", (rid,))],
                ["ok.py"])
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM file_blobs").fetchone()[0], 1)
            # raw_json выжившего отчёта пересобран из выживших runs
            raw = json.loads(conn.execute(
                "SELECT raw_json FROM reports WHERE id=?", (rid,)).fetchone()[0])
            self.assertEqual([r["index"] for r in raw["runs"]], [1])
            self.assertEqual(raw["copies"], 1)
            self.assertEqual(raw["summary"], {"ok": 1, "timeout": 0, "error": 0})

    def test_run_idx_mode_refuses_mismatched_raw_json_without_changes(self):
        """C1: apply не теряет SQL-run, отсутствующий в raw_json.runs[]."""
        with tempfile.TemporaryDirectory() as td:
            conn = self._connect(td)
            report = _make_report("prov", "m", "p1", 2, 0,
                                  "2026-01-01T00:00:00")
            rid = _upsert(conn, report, "data/result/r1.json",
                          artifacts=[_artifact(1, "one.py", b"one"),
                                     _artifact(2, "two.py", b"two")])
            degraded = dict(report)
            degraded["runs"] = report["runs"][:1]
            with conn:
                conn.execute(
                    "UPDATE reports SET raw_json=? WHERE id=?",
                    (json.dumps(degraded), rid),
                )

            before = self._report_state(conn, rid)
            rc, _out, err = self._run_captured(
                conn, report_id=rid, run_idx=1, apply=True)

            self.assertNotEqual(rc, 0)
            self.assertIn("рассинхрон", err)
            self.assertIn(
                f"python scripts/regenerate_raw_json.py --report-id {rid}", err)
            self.assertEqual(self._report_state(conn, rid), before)

    def test_run_idx_mode_dry_run_reports_mismatched_raw_json(self):
        """C1: dry-run тоже валидирует наборы idx, не меняя данные."""
        with tempfile.TemporaryDirectory() as td:
            conn = self._connect(td)
            report = _make_report("prov", "m", "p1", 2, 0,
                                  "2026-01-01T00:00:00")
            rid = _upsert(conn, report, "data/result/r1.json")
            degraded = dict(report)
            degraded["runs"] = report["runs"][:1]
            with conn:
                conn.execute(
                    "UPDATE reports SET raw_json=? WHERE id=?",
                    (json.dumps(degraded), rid),
                )

            before = self._report_state(conn, rid)
            rc, _out, err = self._run_captured(
                conn, report_id=rid, run_idx=1)

            self.assertNotEqual(rc, 0)
            self.assertIn("рассинхрон", err)
            self.assertEqual(self._report_state(conn, rid), before)

    def test_run_idx_mode_deletes_emptied_report(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._connect(td)
            report = _make_report("prov", "m", "p1", 1, 0, "2026-01-01T00:00:00")
            rid = _upsert(conn, report, "data/result/r1.json",
                          artifacts=[_artifact(1, "only.py", b"only")])

            rc, _ = self._run(conn, report_id=rid, run_idx=1, apply=True)
            self.assertEqual(rc, 0)
            # единственная копия удалена — опустевший отчёт удалён целиком
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM reports").fetchone()[0], 0)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM file_blobs").fetchone()[0], 0)

    def test_run_idx_mode_missing_run_fails(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._connect(td)
            report = _make_report("prov", "m", "p1", 1, 0, "2026-01-01T00:00:00")
            rid = _upsert(conn, report, "data/result/r1.json")
            rc, _ = self._run(conn, report_id=rid, run_idx=99)
            self.assertNotEqual(rc, 0)

    # --- режим 3: все результаты модели (provider/model [+ --project]) --------

    def test_model_mode_scoped_to_project_and_global(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._connect(td)
            _upsert(conn, _make_report("prov", "m", "p1", 1, 0,
                                       "2026-01-01T00:00:00"),
                    "data/result/r1.json")
            _upsert(conn, _make_report("prov", "m", "p1", 1, 0,
                                       "2026-01-02T00:00:00"),
                    "data/result/r2.json")
            _upsert(conn, _make_report("prov", "m", "p2", 1, 0,
                                       "2026-01-03T00:00:00"),
                    "data/result/r3.json")
            _upsert(conn, _make_report("prov", "other", "p1", 1, 0,
                                       "2026-01-04T00:00:00"),
                    "data/result/r4.json")

            rc, out = self._run(conn, model="prov/m")  # dry-run: вся модель
            self.assertEqual(rc, 0)
            self.assertIn("отчётов=3", out)
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM reports").fetchone()[0], 4)

            rc, _ = self._run(conn, model="prov/m", project="p2", apply=True)
            self.assertEqual(rc, 0)
            rest = {(r["project"], r["model"]) for r in conn.execute(
                "SELECT project, model FROM reports")}
            self.assertEqual(rest, {("p1", "m"), ("p1", "other")},
                             "--project сужает удаление до одного проекта")

            rc, _ = self._run(conn, model="prov/m", apply=True)
            self.assertEqual(rc, 0)
            rest = {(r["project"], r["model"]) for r in conn.execute(
                "SELECT project, model FROM reports")}
            self.assertEqual(rest, {("p1", "other")},
                             "чужая модель не затрагивается")

    # --- режим 4: проект целиком (--project без модели) ------------------------

    def test_project_mode_deletes_project_and_result_dir(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._connect(td)
            _upsert(conn, _make_report("prov", "m", "p1", 1, 0,
                                       "2026-01-01T00:00:00"),
                    "data/result/r1.json")
            _upsert(conn, _make_report("prov", "m", "p2", 1, 0,
                                       "2026-01-02T00:00:00"),
                    "data/result/r2.json")
            with conn:
                conn.execute(
                    "INSERT INTO projects_library (name, prompt, raw_json) "
                    "VALUES ('p1', 't', '{}')")
            result_root = Path(td) / "result"
            (result_root / "p1").mkdir(parents=True)
            (result_root / "p1" / "trace.log").write_text("x", encoding="utf-8")
            (result_root / "p2").mkdir()

            rc, out = self._run(conn, project="p1", result_root=result_root)
            self.assertEqual(rc, 0)
            self.assertIn("dry-run", out)
            self.assertTrue((result_root / "p1").exists(),
                            "dry-run не трогает файлы")
            self.assertEqual(conn.execute(
                "SELECT COUNT(*) FROM reports").fetchone()[0], 2)

            rc, _ = self._run(conn, project="p1", result_root=result_root,
                              apply=True)
            self.assertEqual(rc, 0)
            self.assertEqual([r["project"] for r in conn.execute(
                "SELECT project FROM reports")], ["p2"])
            self.assertIsNone(conn.execute(
                "SELECT 1 FROM projects_library WHERE name='p1'").fetchone())
            self.assertFalse((result_root / "p1").exists(),
                             "каталог проекта чистится после commit")
            self.assertTrue((result_root / "p2").exists())

    def test_project_mode_refuses_active_run_without_changes(self):
        """C2: apply не удаляет БД и диск проекта с живым прогоном."""
        with tempfile.TemporaryDirectory() as td:
            conn = self._connect(td)
            report = _make_report("prov", "m", "p1", 1, 0,
                                  "2026-01-01T00:00:00")
            _upsert(conn, report, "data/result/r1.json")
            with conn:
                conn.execute(
                    "INSERT INTO projects_library (name, prompt, raw_json) "
                    "VALUES ('p1', 't', '{}')")
            result_root = Path(td) / "result"
            copy_dir = result_root / "p1" / "prov_m" / "run_1"
            copy_dir.mkdir(parents=True)
            sentinel = copy_dir / "run.log"
            sentinel.write_text("active", encoding="utf-8")
            write_run_active_marker(copy_dir, pid=os.getpid())

            before_reports = conn.execute(
                "SELECT count(*) FROM reports WHERE project='p1'").fetchone()[0]
            rc, _out, err = self._run_captured(
                conn, project="p1", result_root=result_root, apply=True)

            self.assertNotEqual(rc, 0)
            self.assertIn("проект p1 имеет активный прогон", err)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM reports WHERE project='p1'"
            ).fetchone()[0], before_reports)
            self.assertIsNotNone(conn.execute(
                "SELECT 1 FROM projects_library WHERE name='p1'").fetchone())
            self.assertEqual(sentinel.read_text(encoding="utf-8"), "active")

    def test_project_mode_dry_run_reports_active_run(self):
        """C2: dry-run сообщает о живом прогоне тем же ненулевым кодом."""
        with tempfile.TemporaryDirectory() as td:
            conn = self._connect(td)
            report = _make_report("prov", "m", "p1", 1, 0,
                                  "2026-01-01T00:00:00")
            _upsert(conn, report, "data/result/r1.json")
            result_root = Path(td) / "result"
            copy_dir = result_root / "p1" / "prov_m" / "run_1"
            copy_dir.mkdir(parents=True)
            write_run_active_marker(copy_dir, pid=os.getpid())

            rc, _out, err = self._run_captured(
                conn, project="p1", result_root=result_root)

            self.assertNotEqual(rc, 0)
            self.assertIn("активный прогон", err)
            self.assertEqual(conn.execute(
                "SELECT count(*) FROM reports WHERE project='p1'"
            ).fetchone()[0], 1)
            self.assertTrue(copy_dir.exists())

    def test_project_mode_missing_project_fails(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._connect(td)
            rc, _ = self._run(conn, project="ghost", apply=True)
            self.assertNotEqual(rc, 0)

    # --- валидация CLI-аргументов ----------------------------------------------

    def test_cli_rejects_invalid_mode_combinations(self):
        for argv in (
            ["delete_reports.py"],                              # нет режима
            ["delete_reports.py", "--run-idx", "1"],            # run-idx без отчёта
            ["delete_reports.py", "prov/m", "--report-id", "1"],
            ["delete_reports.py", "--report-id", "1", "--project", "p"],
            ["delete_reports.py", "not-a-model-ref"],           # нет '/'
        ):
            with mock.patch.object(sys, "argv", argv):
                with self.assertRaises(SystemExit, msg=argv) as ctx:
                    delete_reports.main()
                self.assertNotEqual(ctx.exception.code, 0, argv)

    @staticmethod
    def _report_state(conn, report_id):
        """Снимок всех затрагиваемых C1 строк для проверки отсутствия мутаций."""
        report = tuple(conn.execute(
            "SELECT raw_json, copies, summary_ok, summary_timeout, summary_error "
            "FROM reports WHERE id=?", (report_id,)).fetchone())
        runs = [tuple(row) for row in conn.execute(
            "SELECT idx, port, dir, status, code, elapsed FROM runs "
            "WHERE report_id=? ORDER BY idx", (report_id,))]
        artifacts = [tuple(row) for row in conn.execute(
            "SELECT run_idx, path, sha256 FROM run_artifacts "
            "WHERE report_id=? ORDER BY run_idx, path", (report_id,))]
        blobs = [tuple(row) for row in conn.execute(
            "SELECT sha256, size_bytes, content_encoding, content_blob "
            "FROM file_blobs ORDER BY sha256")]
        return report, runs, artifacts, blobs


class DeleteModelReportsDbTests(unittest.TestCase):
    """db.delete_model_reports — общий помощник _delete_reports_by_ids (issue #121)."""

    def test_delete_model_reports_counts_and_prunes_blobs(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                shared = b"shared blob"
                report = _make_report("prov", "m", "p1", 1, 1,
                                      "2026-01-01T00:00:00")
                _upsert(conn, report, "data/result/r1.json",
                        artifacts=[_artifact(1, "a.py", shared),
                                   _artifact(2, "b.py", b"own")])
                keeper = _make_report("prov", "other", "p1", 1, 0,
                                      "2026-01-02T00:00:00")
                _upsert(conn, keeper, "data/result/r2.json",
                        artifacts=[_artifact(1, "a.py", shared)])

                with conn:
                    result = db.delete_model_reports(conn, "prov", "m")

                self.assertEqual(result["reports"], 1)
                self.assertEqual(result["runs"], 2)
                self.assertEqual(result["artifacts"], 2)
                # общий блоб уцелел (нужен keeper-отчёту), свой подметён
                self.assertEqual(conn.execute(
                    "SELECT COUNT(*) FROM file_blobs").fetchone()[0], 1)

                # несуществующая модель — нули, без исключений
                with conn:
                    empty = db.delete_model_reports(conn, "prov", "ghost")
                self.assertEqual(empty["reports"], 0)
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

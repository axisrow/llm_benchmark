"""issue #110 — безопасное удаление проекта (слои БД + файловая очистка).

Контракт (тело #110):
- db.delete_project(conn, project_name) — транзакционно удаляет строку
  projects_library, все reports с ТОЧНЫМ совпадением project и каскадно
  runs/agent_questions/question_reviews/run_artifacts; orphan blobs подметаются
  ОДИН раз после удаления всех отчётов; общий блоб чужого проекта уцелевает.
- Удаление по точному имени, одноимённые префиксы не затрагиваются.
- Возвращает структуру со счётчиками (reports/runs/artifacts).
- Несуществующий проект → предсказуемый признак «ничего не удалено», не частичный
  успех (для API это 404).
- Атомарность: ошибка внутри транзакции не оставляет наполовину удалённый проект.
- Файловая очистка data/result/<project>/ — безопасная (без follow symlink за
  пределы data/result), отказ при живом .bench-active.json marker.
- Повторное удаление не повреждает другие данные.
- PRAGMA foreign_key_check не даёт новых нарушений после удаления.
"""

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import artifacts
import db


def _report(project, provider="v", model="m", started_at="2026-01-01T00:00:00",
            *, with_question=False, run_dir="/x"):
    """Отчёт для сидирования. with_question=True добавляет planning-вопрос,
    чтобы протестировать каскад agent_questions/question_reviews."""
    run = {"index": 0, "port": 4000, "dir": run_dir, "status": "готово",
           "code": 0, "elapsed": 10.0, "usage": None}
    if with_question:
        run["questions"] = [{
            "attempt_idx": 1, "session_id": "s", "request_id": "req",
            "round_idx": 1, "question_idx": 1, "header": "H",
            "question": "Какой формат?", "multiple": False, "custom": True,
            "options": [{"label": "JSON"}], "answer": ["JSON"],
            "responder": "first", "fallback_used": False,
            "reply_status": "replied", "reply_error": None, "elapsed": 0.1,
        }]
    return {
        "project": project, "provider": provider, "model": model,
        "prompt": "t", "description": None, "what_it_tests": None, "copies": 1,
        "started_at": started_at, "run_elapsed": 1.0,
        "summary": {"ok": 1, "timeout": 0, "error": 0}, "pricing": {},
        "usage_summary": {}, "artifact_summary": {},
        "runs": [run],
    }


def _art(path, content, run_idx=0):
    blob = content.encode()
    return artifacts.RunArtifact(
        run_idx=run_idx, path=path, kind="agent_file",
        size_bytes=len(blob), sha256=hashlib.sha256(blob).hexdigest(),
        content=blob, source_path=Path("/x"))


def _seed_library(conn, name, *, description="", prompt="", what_it_tests=None):
    """Пишет строку projects_library (delete_project обязан её тоже снести)."""
    raw = json.dumps({"name": name, "description": description, "prompt": prompt,
                      "what_it_tests": what_it_tests or []}, ensure_ascii=False)
    conn.execute(
        "INSERT INTO projects_library (name, description, prompt, what_it_tests, raw_json) "
        "VALUES (?, ?, ?, ?, ?)",
        (name, description, prompt, json.dumps(what_it_tests or []), raw))


class DeleteProjectDbTests(unittest.TestCase):
    def _conn(self, td):
        conn = db.connect(Path(td) / "main.db")
        db.init_schema(conn)
        return conn

    def test_deletes_library_row_and_all_reports(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._conn(td)
            try:
                with conn:
                    _seed_library(conn, "doomed")
                    db.upsert_report(conn, _report("doomed", model="m1"),
                                     "r1.json", json.dumps({"x": 1}))
                    db.upsert_report(conn, _report("doomed", model="m2"),
                                     "r2.json", json.dumps({"x": 2}))
                with conn:
                    result = db.delete_project(conn, "doomed")
                self.assertEqual(result["reports"], 2)
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM reports WHERE project=?",
                    ("doomed",)).fetchone()[0], 0)
                self.assertIsNone(conn.execute(
                    "SELECT 1 FROM projects_library WHERE name=?",
                    ("doomed",)).fetchone())
            finally:
                conn.close()

    def test_cascade_runs_questions_reviews_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._conn(td)
            try:
                with conn:
                    _seed_library(conn, "doomed")
                    rid = db.upsert_report(
                        conn, _report("doomed", with_question=True),
                        "r.json", json.dumps({"x": 1}),
                        artifacts=[_art("a.txt", "aaa")])
                    db.put_question_review(
                        conn, report_id=rid, run_idx=0, attempt_idx=1,
                        request_id="req", question_idx=1, verdict="useful")
                # предусловие: всё на месте
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM question_reviews").fetchone()[0], 1)
                with conn:
                    result = db.delete_project(conn, "doomed")
                for table in ("runs", "agent_questions", "question_reviews",
                              "run_artifacts"):
                    self.assertEqual(conn.execute(
                        f"SELECT count(*) FROM {table} WHERE report_id=?",
                        (rid,)).fetchone()[0], 0, table)
                self.assertGreaterEqual(result["runs"], 1)
                self.assertGreaterEqual(result["artifacts"], 1)
            finally:
                conn.close()

    def test_orphan_blobs_pruned_shared_kept(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._conn(td)
            try:
                shared = _art("shared.txt", "shared")
                with conn:
                    _seed_library(conn, "keep")
                    _seed_library(conn, "doomed")
                    db.upsert_report(conn, _report("keep"),
                                     "keep.json", json.dumps({"x": 1}),
                                     artifacts=[shared])
                    db.upsert_report(conn, _report("doomed"),
                                     "del.json", json.dumps({"x": 2}),
                                     artifacts=[shared, _art("only.txt", "only")])
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM file_blobs").fetchone()[0], 2)
                with conn:
                    db.delete_project(conn, "doomed")
                # only.txt осиротел → подметён; shared остался у проекта keep
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM file_blobs").fetchone()[0], 1)
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM file_blobs WHERE sha256=?",
                    (shared.sha256,)).fetchone()[0], 1)
                # чужой проект нетронут
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM reports WHERE project=?",
                    ("keep",)).fetchone()[0], 1)
            finally:
                conn.close()

    def test_exact_name_match_not_prefix(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._conn(td)
            try:
                with conn:
                    _seed_library(conn, "proj")
                    _seed_library(conn, "proj_v2")
                    db.upsert_report(conn, _report("proj"),
                                     "a.json", json.dumps({"x": 1}))
                    db.upsert_report(conn, _report("proj_v2"),
                                     "b.json", json.dumps({"x": 2}))
                with conn:
                    result = db.delete_project(conn, "proj")
                self.assertEqual(result["reports"], 1)
                # одноимённый префикс уцелел целиком
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM reports WHERE project=?",
                    ("proj_v2",)).fetchone()[0], 1)
                self.assertIsNotNone(conn.execute(
                    "SELECT 1 FROM projects_library WHERE name=?",
                    ("proj_v2",)).fetchone())
            finally:
                conn.close()

    def test_nonexistent_project_returns_zero_no_side_effects(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._conn(td)
            try:
                with conn:
                    _seed_library(conn, "keep")
                    db.upsert_report(conn, _report("keep"),
                                     "k.json", json.dumps({"x": 1}))
                with conn:
                    result = db.delete_project(conn, "ghost")
                self.assertEqual(result["reports"], 0)
                self.assertFalse(result["existed"])
                # чужие данные нетронуты
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM reports").fetchone()[0], 1)
            finally:
                conn.close()

    def test_repeated_delete_is_idempotent(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._conn(td)
            try:
                with conn:
                    _seed_library(conn, "keep")
                    _seed_library(conn, "doomed")
                    db.upsert_report(conn, _report("keep"),
                                     "k.json", json.dumps({"x": 1}))
                    db.upsert_report(conn, _report("doomed"),
                                     "d.json", json.dumps({"x": 2}))
                with conn:
                    first = db.delete_project(conn, "doomed")
                with conn:
                    second = db.delete_project(conn, "doomed")
                self.assertEqual(first["reports"], 1)
                self.assertEqual(second["reports"], 0)
                self.assertFalse(second["existed"])
                # keep-проект не повреждён повторным запросом
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM reports WHERE project=?",
                    ("keep",)).fetchone()[0], 1)
            finally:
                conn.close()

    def test_no_new_foreign_key_violations_after_delete(self):
        with tempfile.TemporaryDirectory() as td:
            conn = self._conn(td)
            try:
                with conn:
                    _seed_library(conn, "doomed")
                    _seed_library(conn, "keep")
                    rid = db.upsert_report(
                        conn, _report("doomed", with_question=True),
                        "r.json", json.dumps({"x": 1}),
                        artifacts=[_art("a.txt", "aaa")])
                    db.put_question_review(
                        conn, report_id=rid, run_idx=0, attempt_idx=1,
                        request_id="req", question_idx=1, verdict="useful")
                    db.upsert_report(conn, _report("keep"),
                                     "k.json", json.dumps({"x": 2}),
                                     artifacts=[_art("b.txt", "bbb")])
                self.assertEqual(list(conn.execute("PRAGMA foreign_key_check")), [])
                with conn:
                    db.delete_project(conn, "doomed")
                self.assertEqual(list(conn.execute("PRAGMA foreign_key_check")), [])
            finally:
                conn.close()

    def test_rollback_on_error_leaves_project_intact(self):
        """Ошибка внутри транзакции удаления не оставляет проект наполовину
        удалённым: `with conn` откатывает всё."""
        with tempfile.TemporaryDirectory() as td:
            conn = self._conn(td)
            try:
                with conn:
                    _seed_library(conn, "doomed")
                    db.upsert_report(conn, _report("doomed", model="m1"),
                                     "a.json", json.dumps({"x": 1}))
                    db.upsert_report(conn, _report("doomed", model="m2"),
                                     "b.json", json.dumps({"x": 2}))
                before = conn.execute(
                    "SELECT count(*) FROM reports WHERE project=?",
                    ("doomed",)).fetchone()[0]

                # Инъекция сбоя: prune_orphan_blobs падает уже после части DELETE.
                orig = db.prune_orphan_blobs

                def boom(_conn):
                    raise RuntimeError("disk full")

                db.prune_orphan_blobs = boom
                try:
                    with self.assertRaises(RuntimeError):
                        with conn:
                            db.delete_project(conn, "doomed")
                finally:
                    db.prune_orphan_blobs = orig

                # Откат: все отчёты и строка библиотеки на месте.
                self.assertEqual(conn.execute(
                    "SELECT count(*) FROM reports WHERE project=?",
                    ("doomed",)).fetchone()[0], before)
                self.assertIsNotNone(conn.execute(
                    "SELECT 1 FROM projects_library WHERE name=?",
                    ("doomed",)).fetchone())
            finally:
                conn.close()


class ProjectActiveRunTests(unittest.TestCase):
    """Отказ при активном прогоне: живой .bench-active.json marker в
    data/result/<project>/ означает, что проект удалять нельзя."""

    def test_no_marker_no_active_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "proj" / "v_m" / "20260101_1").mkdir(parents=True)
            self.assertFalse(
                artifacts.project_has_active_run(root, "proj"))

    def test_live_marker_reports_active(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            copy_dir = root / "proj" / "v_m" / "20260101_1"
            copy_dir.mkdir(parents=True)
            # marker текущего живого процесса (наш PID) — гарантированно alive
            artifacts.write_run_active_marker(copy_dir, pid=os.getpid())
            self.assertTrue(
                artifacts.project_has_active_run(root, "proj"))

    def test_dead_marker_not_active(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            copy_dir = root / "proj" / "v_m" / "20260101_1"
            copy_dir.mkdir(parents=True)
            artifacts.write_run_active_marker(
                copy_dir, pid=999_999_999, started_at=time.time())
            self.assertFalse(
                artifacts.project_has_active_run(root, "proj"))

    def test_active_run_in_other_project_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            other = root / "other" / "v_m" / "20260101_1"
            other.mkdir(parents=True)
            artifacts.write_run_active_marker(other, pid=os.getpid())
            # проверяем ДРУГОЙ проект — активный прогон соседа не мешает
            self.assertFalse(
                artifacts.project_has_active_run(root, "proj"))


class DeleteProjectResultDirTests(unittest.TestCase):
    """Безопасное удаление data/result/<project>/ (после commit БД)."""

    def test_removes_project_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            proj = root / "proj" / "v_m" / "20260101_1"
            proj.mkdir(parents=True)
            (proj / "run.log").write_text("log", encoding="utf-8")
            keep = root / "keep" / "v_m" / "20260101_1"
            keep.mkdir(parents=True)
            (keep / "run.log").write_text("log", encoding="utf-8")

            artifacts.delete_project_result_dir(root, "proj")
            self.assertFalse((root / "proj").exists())
            # чужой проект нетронут
            self.assertTrue(keep.exists())

    def test_missing_dir_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # не должно бросать — проект без файлов на диске штатен
            artifacts.delete_project_result_dir(root, "no_such_project")

    def test_does_not_follow_symlink_outside_root(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td) / "result"
            root.mkdir()
            outside = Path(td) / "outside"
            outside.mkdir()
            (outside / "precious.txt").write_text("keep me", encoding="utf-8")
            # проект — симлинк наружу; удаление НЕ должно снести содержимое outside
            link = root / "proj"
            os.symlink(outside, link)

            artifacts.delete_project_result_dir(root, "proj")
            self.assertTrue((outside / "precious.txt").exists())

    def test_exact_name_not_prefix_on_disk(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "proj" / "v_m" / "1").mkdir(parents=True)
            (root / "proj_v2" / "v_m" / "1").mkdir(parents=True)
            artifacts.delete_project_result_dir(root, "proj")
            self.assertFalse((root / "proj").exists())
            self.assertTrue((root / "proj_v2").exists())


if __name__ == "__main__":
    unittest.main()

import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import time
import zipfile
from io import BytesIO
from pathlib import Path
import unittest

# `artifacts`/`db` лежат в корне (pytest держит его на sys.path); `run_artifacts`
# и `cleanup_result_dir` остаются в scripts/ — добавляем её только ради них.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import artifacts
import cleanup_result_dir
import db
import run_artifacts


def _report() -> dict:
    return {
        "project": "p",
        "provider": "provider",
        "model": "model",
        "started_at": "2026-01-01T00:00:00",
        "summary": {"ok": 2, "timeout": 0, "error": 0},
        "runs": [
            {
                "index": 1,
                "port": 4096,
                "dir": "/tmp/run1",
                "status": "готово",
                "code": 0,
                "elapsed": 1.0,
            },
            {
                "index": 2,
                "port": 4097,
                "dir": "/tmp/run2",
                "status": "готово",
                "code": 0,
                "elapsed": 1.0,
            },
        ],
    }


class ArtifactTests(unittest.TestCase):
    def test_collect_run_artifacts_includes_logs_and_agent_files_only(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "run.log").write_text("log", encoding="utf-8")
            (root / "hello.py").write_text("print('hi')\n", encoding="utf-8")
            (root / "nested").mkdir()
            (root / "nested" / "data.bin").write_bytes(b"\x00\x01")
            (root / "report.json").write_text("{}", encoding="utf-8")
            (root / ".DS_Store").write_bytes(b"noise")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "hello.pyc").write_bytes(b"pyc")

            collection = artifacts.collect_run_artifacts(1, root)

        by_path = {artifact.path: artifact for artifact in collection.artifacts}
        # B8: агентский report.json теперь собирается как обычный файл, а не
        # выбрасывается в trash (см. tests/test_fix_B8.py).
        self.assertEqual(
            set(by_path),
            {"run.log", "hello.py", "nested/data.bin", "report.json"},
        )
        self.assertEqual(by_path["run.log"].kind, "log")
        self.assertEqual(by_path["hello.py"].kind, "agent_file")
        self.assertEqual(by_path["report.json"].kind, "agent_file")
        self.assertEqual(collection.summary()["files"], 4)
        self.assertTrue(any(path.name == "__pycache__" for path in collection.trash_paths))
        self.assertTrue(any(path.name == ".DS_Store" for path in collection.trash_paths))

    def test_collect_run_artifacts_skips_oversized_files(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "run.log").write_text("log", encoding="utf-8")
            big = root / "big.bin"
            big.write_bytes(b"x" * (artifacts.MAX_ARTIFACT_BYTES + 1))

            collection = artifacts.collect_run_artifacts(1, root)

        self.assertEqual([artifact.path for artifact in collection.artifacts], ["run.log"])
        self.assertTrue(any("big.bin" in error and "exceeds" in error
                            for error in collection.errors))

    def test_cleanup_removes_collected_files_and_trash_keeps_others(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "run.log").write_text("log", encoding="utf-8")
            (root / "hello.py").write_text("print('hi')\n", encoding="utf-8")
            (root / "nested").mkdir()
            (root / "nested" / "data.bin").write_bytes(b"\x00\x01")
            (root / ".DS_Store").write_bytes(b"noise")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "hello.pyc").write_bytes(b"pyc")

            collection = artifacts.collect_run_artifacts(1, root)

            # Файл, появившийся ПОСЛЕ сбора, не в collection — cleanup не должен
            # его трогать (удаляются только собранные артефакты и trash).
            keep = root / "keep_me"
            keep.mkdir()
            (keep / "later.txt").write_text("later", encoding="utf-8")

            artifacts.cleanup_collected_artifacts(collection)

            # Собранные артефакты и их (опустевшие) каталоги удалены.
            self.assertFalse((root / "run.log").exists())
            self.assertFalse((root / "hello.py").exists())
            self.assertFalse((root / "nested" / "data.bin").exists())
            self.assertFalse((root / "nested").exists())
            # Мусор (trash) удалён целиком.
            self.assertFalse((root / ".DS_Store").exists())
            self.assertFalse((root / "__pycache__").exists())
            # Несобранный файл и его непустой каталог сохранены.
            self.assertTrue((keep / "later.txt").exists())

    def test_cleanup_removes_emptied_work_dir(self):
        # issue #42: после сбора артефактов в базу и удаления файлов сама
        # папка копии оставалась на диске — пустые
        # data/result/<proj>/<prov_model>/<ts>_<N>/ копились сотнями.
        with tempfile.TemporaryDirectory() as td:
            run_root = Path(td) / "prov_model"
            work_dir = run_root / "20260101-120000_1"
            work_dir.mkdir(parents=True)
            (work_dir / "run.log").write_text("log", encoding="utf-8")
            (work_dir / "nested").mkdir()
            (work_dir / "nested" / "data.bin").write_bytes(b"\x00")

            collection = artifacts.collect_run_artifacts(1, work_dir)
            artifacts.cleanup_collected_artifacts(collection)

            self.assertFalse(work_dir.exists(),
                             "опустевшая папка копии должна удаляться целиком")
            # Родителя (папку модели) не трогаем — не наша зона ответственности.
            self.assertTrue(run_root.exists())

    def test_cleanup_is_safe_when_files_already_gone(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "run.log").write_text("log", encoding="utf-8")
            collection = artifacts.collect_run_artifacts(1, root)
            # Файл исчез до очистки — cleanup не должен падать (FileNotFoundError).
            (root / "run.log").unlink()
            artifacts.cleanup_collected_artifacts(collection)
            self.assertFalse((root / "run.log").exists())

    def test_upsert_report_stores_deduped_artifacts_and_reads_bytes(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            first = root / "run1" / "hello.py"
            second = root / "run2" / "hello.py"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_bytes(b"same")
            second.write_bytes(b"same")
            art1 = artifacts.collect_run_artifacts(1, first.parent).artifacts[0]
            art2 = artifacts.collect_run_artifacts(2, second.parent).artifacts[0]

            conn = db.connect(root / "main.db")
            try:
                db.init_schema(conn)
                report = _report()
                with conn:
                    report_id = db.upsert_report(
                        conn,
                        report,
                        "data/result/p/report.json",
                        json.dumps(report),
                        artifacts=[art1, art2],
                    )
                blob_count = conn.execute("SELECT COUNT(*) FROM file_blobs").fetchone()[0]
                mapping_count = conn.execute(
                    "SELECT COUNT(*) FROM run_artifacts WHERE report_id = ?",
                    (report_id,),
                ).fetchone()[0]
                content = db.read_artifact(conn, report_id, 1, "hello.py")
            finally:
                conn.close()

        self.assertEqual(blob_count, 1)
        self.assertEqual(mapping_count, 2)
        self.assertEqual(content, b"same")

    def test_upsert_report_replaces_artifact_mapping_without_blob_duplication(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir = root / "run1"
            run_dir.mkdir()
            path = run_dir / "hello.py"
            path.write_bytes(b"first")
            first_artifact = artifacts.collect_run_artifacts(1, run_dir).artifacts[0]

            conn = db.connect(root / "main.db")
            try:
                db.init_schema(conn)
                report = _report()
                with conn:
                    report_id = db.upsert_report(
                        conn,
                        report,
                        "data/result/p/report.json",
                        json.dumps(report),
                        artifacts=[first_artifact],
                    )

                path.write_bytes(b"second")
                second_artifact = artifacts.collect_run_artifacts(1, run_dir).artifacts[0]
                with conn:
                    db.upsert_report(
                        conn,
                        report,
                        "data/result/p/report.json",
                        json.dumps(report),
                        artifacts=[second_artifact],
                    )

                mappings = db.list_artifacts(conn, report_id)
                blob_count = conn.execute("SELECT COUNT(*) FROM file_blobs").fetchone()[0]
                content = db.read_artifact(conn, report_id, 1, "hello.py")
            finally:
                conn.close()

        self.assertEqual(len(mappings), 1)
        # После фикса B3 перезапись отчёта новым содержимым подметает осиротевший
        # старый blob (b"first"): остаётся ровно один живой blob (b"second").
        self.assertEqual(blob_count, 1)
        self.assertEqual(content, b"second")

    def test_old_upsert_without_artifacts_remains_valid(self):
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                report = _report()
                with conn:
                    report_id = db.upsert_report(
                        conn,
                        report,
                        "data/result/p/report.json",
                        json.dumps(report),
                    )
                self.assertEqual(db.list_artifacts(conn, report_id), [])
            finally:
                conn.close()

    def test_backfill_keeps_stored_artifacts_when_dir_has_only_trash(self):
        # issue #42: папка прогона осталась на диске, но содержит только мусор
        # (.DS_Store от Finder). backfill не должен перезаписывать артефакты
        # отчёта пустым списком — это тихая потеря данных в коммитящейся базе.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir = root / "run1"
            run_dir.mkdir()
            (run_dir / "hello.py").write_text("print('hi')\n", encoding="utf-8")
            collection = artifacts.collect_run_artifacts(1, run_dir)

            db_path = root / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                report = _report()
                report["runs"][0]["dir"] = str(run_dir)
                with conn:
                    report_id = db.upsert_report(
                        conn,
                        report,
                        "data/result/p/report.json",
                        json.dumps(report),
                        artifacts=collection.artifacts,
                    )
            finally:
                conn.close()

            # Штатная зачистка после прогона + мусор, появившийся позже.
            artifacts.cleanup_collected_artifacts(collection)
            run_dir.mkdir(exist_ok=True)
            (run_dir / ".DS_Store").write_bytes(b"noise")

            rc = run_artifacts.cmd_backfill(argparse.Namespace(
                db=db_path, report_id=report_id, keep_files=False))

            conn = db.connect(db_path)
            try:
                stored = db.list_artifacts(conn, report_id)
            finally:
                conn.close()

        self.assertEqual(rc, 0)
        self.assertEqual([row["path"] for row in stored], ["hello.py"],
                         "backfill по папке без артефактов не должен стирать "
                         "уже сохранённые артефакты отчёта")
        # Мусор при этом с диска подметён (keep_files=False).
        self.assertFalse((run_dir / ".DS_Store").exists())

    def test_backfill_preserves_artifacts_of_missing_run_dirs(self):
        # Триаж adversarial-ревью PR #43: replace по всему отчёту стирал
        # артефакты копий, чьи папки уже зачищены, если хотя бы одна папка
        # дала артефакты. Частичный backfill обязан трогать только run_idx,
        # по которым реально что-то собрано.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run1 = root / "run1"
            run2 = root / "run2"
            run1.mkdir()
            run2.mkdir()
            (run1 / "hello.py").write_text("print('hi')\n", encoding="utf-8")
            (run2 / "world.py").write_text("print('world')\n", encoding="utf-8")
            both = (artifacts.collect_run_artifacts(1, run1).artifacts
                    + artifacts.collect_run_artifacts(2, run2).artifacts)

            db_path = root / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                report = _report()
                report["runs"][0]["dir"] = str(run1)
                report["runs"][1]["dir"] = str(run2)
                with conn:
                    report_id = db.upsert_report(
                        conn,
                        report,
                        "data/result/p/report.json",
                        json.dumps(report),
                        artifacts=both,
                    )
            finally:
                conn.close()

            # Папка run2 уже зачищена (артефакты живут только в базе),
            # run1 уцелела — например, прерванный прогон.
            shutil.rmtree(run2)

            rc = run_artifacts.cmd_backfill(argparse.Namespace(
                db=db_path, report_id=report_id, keep_files=False))

            conn = db.connect(db_path)
            try:
                stored = {(row["run_idx"], row["path"])
                          for row in db.list_artifacts(conn, report_id)}
            finally:
                conn.close()

        self.assertEqual(rc, 0)
        self.assertIn((1, "hello.py"), stored)
        self.assertIn((2, "world.py"), stored,
                      "артефакты копии с зачищенной папкой должны пережить "
                      "частичный backfill")

    def test_zip_export_contains_report_json_and_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir = root / "run1"
            run_dir.mkdir()
            (run_dir / "run.log").write_text("log", encoding="utf-8")
            (run_dir / "hello.py").write_text("print('hi')\n", encoding="utf-8")
            collection = artifacts.collect_run_artifacts(1, run_dir)

            conn = db.connect(root / "main.db")
            try:
                db.init_schema(conn)
                report = _report()
                with conn:
                    report_id = db.upsert_report(
                        conn,
                        report,
                        "data/result/p/report.json",
                        json.dumps(report),
                        artifacts=collection.artifacts,
                    )
                zip_bytes = run_artifacts._zip_report(conn, report_id)
            finally:
                conn.close()

        with zipfile.ZipFile(BytesIO(zip_bytes)) as zf:
            self.assertEqual(json.loads(zf.read("report.json"))["project"], "p")
            self.assertEqual(zf.read("runs/1/run.log"), b"log")
            self.assertEqual(zf.read("runs/1/hello.py"), b"print('hi')\n")


def _report_with_dir(run_dir: Path) -> dict:
    """Отчёт с одним прогоном, чей runs[0].dir указывает на work_dir копии."""
    report = _report()
    report["runs"] = [report["runs"][0]]
    report["runs"][0]["dir"] = str(run_dir)
    report["summary"] = {"ok": 1, "timeout": 0, "error": 0}
    return report


class CleanupResultDirTests(unittest.TestCase):
    """issue #99: безопасная очистка исторических остатков под data/result/.

    Скрипт должен: dry-run по умолчанию, удалять только файлы, чьё содержимое
    совпадает по SHA с записью в БД, перечислять отдельно неизвестные/несовпадающие
    файлы и симлинки, не выходить за границу data/result/.
    """

    def _setup_run(self, td: Path, content: bytes = b"log body\n"):
        """Создаёт data/result/<proj>/<prov_model>/<stamp>_1/run.log + запись в БД.

        Возвращает (db_path, work_root, work_dir).
        """
        work_root = td / "data" / "result" / "p" / "provider_model"
        work_dir = work_root / "20260101-120000_1"
        work_dir.mkdir(parents=True)
        (work_dir / "run.log").write_bytes(content)

        db_path = td / "main.db"
        conn = db.connect(db_path)
        try:
            db.init_schema(conn)
            collection = artifacts.collect_run_artifacts(1, work_dir)
            report = _report_with_dir(work_dir)
            with conn:
                db.upsert_report(
                    conn, report, "data/result/p/report.json",
                    json.dumps(report), artifacts=collection.artifacts,
                )
        finally:
            conn.close()
        return db_path, work_root, work_dir

    def test_dry_run_does_not_remove_anything(self):
        # dry-run по умолчанию: ничего не меняется на диске.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path, work_root, work_dir = self._setup_run(root)

            ns = argparse.Namespace(db=db_path, result_root=work_root,
                                    apply=False)
            rc = cleanup_result_dir.cmd_cleanup(ns)

            self.assertEqual(rc, 0)
            self.assertTrue((work_dir / "run.log").exists(),
                            "dry-run не должен удалять файлы")

    def test_apply_removes_only_sha_confirmed_files(self):
        # --apply удаляет файл, чей SHA совпадает с записью в БД.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path, work_root, work_dir = self._setup_run(root)

            ns = argparse.Namespace(db=db_path, result_root=work_root, apply=True)
            rc = cleanup_result_dir.cmd_cleanup(ns)

            self.assertEqual(rc, 0)
            self.assertFalse((work_dir / "run.log").exists(),
                             "подтверждённый по SHA файл должен удалиться")
            # Опустевшие каталоги в границах data/result/ зачищаются.
            self.assertFalse(work_dir.exists())

    def test_apply_removes_nested_artifact_by_relative_path(self):
        # issue #99: агентские файлы могут лежать во вложенных папках
        # (run_artifacts.path = «nested/data.bin», путь внутри work_dir).
        # Скрипт должен сопоставлять такой файл по полному относительному
        # пути, а не только по basename — иначе вложенные артефакты уйдут в
        # unknown и не очистятся (контракт нарушения чистоты data/result).
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            work_root = root / "data" / "result" / "p" / "provider_model"
            work_dir = work_root / "20260101-120000_1"
            (work_dir / "nested").mkdir(parents=True)
            (work_dir / "nested" / "data.bin").write_bytes(b"\x00\x01")

            db_path = root / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                collection = artifacts.collect_run_artifacts(1, work_dir)
                report = _report_with_dir(work_dir)
                with conn:
                    db.upsert_report(conn, report, "data/result/p/report.json",
                                     json.dumps(report),
                                     artifacts=collection.artifacts)
            finally:
                conn.close()

            ns = argparse.Namespace(db=db_path, result_root=work_root, apply=True)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cleanup_result_dir.cmd_cleanup(ns)

            self.assertEqual(rc, 0)
            self.assertFalse((work_dir / "nested" / "data.bin").exists(),
                             "вложенный артефакт должен удалиться по полному rel")
            self.assertIn(
                "nested/data.bin",
                [a.path for a in collection.artifacts],
                "предусловие: collect хранит путь внутри work_dir",
            )
            self.assertNotIn("unknown", out.getvalue().lower(),
                             "вложенный артефакт не должен уходить в unknown")

    def test_apply_keeps_mismatched_and_unknown_files(self):
        # Несовпадающий по содержимому и неизвестный файлы остаются; они
        # перечисляются отдельно (mismatched / unknown), не удаляются вслепую.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path, work_root, work_dir = self._setup_run(root)
            # 1) файл с тем же путём run.log, но другим содержимым → mismatched.
            (work_dir / "run.log").write_bytes(b"DIFFERENT body\n")
            # 2) файл, которого нет в БД → unknown.
            (work_dir / "extra.py").write_text("print('x')\n", encoding="utf-8")

            ns = argparse.Namespace(db=db_path, result_root=work_root, apply=True)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cleanup_result_dir.cmd_cleanup(ns)

            self.assertEqual(rc, 0)
            self.assertTrue((work_dir / "run.log").exists(),
                            "несовпадающий по SHA файл не должен удаляться")
            self.assertTrue((work_dir / "extra.py").exists(),
                            "неизвестный файл не должен удаляться вслепую")
            text = out.getvalue()
            self.assertIn("mismatch", text.lower())
            self.assertIn("unknown", text.lower())

    def test_apply_lists_symlinks_separately_and_keeps_them(self):
        # Симлинки не удаляются и перечисляются отдельно.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path, work_root, work_dir = self._setup_run(root)
            target = work_dir / "run.log"
            link = work_dir / "link.log"
            link.symlink_to(target)

            ns = argparse.Namespace(db=db_path, result_root=work_root, apply=True)
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                rc = cleanup_result_dir.cmd_cleanup(ns)

            self.assertEqual(rc, 0)
            # Симлинк уцелел (он не подтверждён по SHA — это не обычный файл).
            self.assertTrue(link.is_symlink())
            self.assertIn("symlink", out.getvalue().lower())

    def test_cleanup_does_not_escape_result_root(self):
        # Очистка не должна трогать ничего за пределами result_root: ни файлы
        # снаружи, ни сиблинг-каталог рядом с result_root.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path, work_root, _work_dir = self._setup_run(root)
            # Файл-свидетель ВНЕ result_root: не должен быть тронут.
            outside = root / "outside.txt"
            outside.write_text("keep", encoding="utf-8")
            # Сиблинг result_root: лежит рядом с work_root (под data/result/p/),
            # но вне самого result_root — _prune_empty_dirs не должен его сносить.
            sibling_dir = work_root.parent / "other_model"
            sibling_dir.mkdir()
            (sibling_dir / "keep.log").write_text("keep", encoding="utf-8")

            ns = argparse.Namespace(db=db_path, result_root=work_root, apply=True)
            rc = cleanup_result_dir.cmd_cleanup(ns)

            self.assertEqual(rc, 0)
            self.assertTrue(outside.exists(),
                            "очистка не должна выходить за границу data/result/")
            self.assertTrue((sibling_dir / "keep.log").exists(),
                            "сиблинг result_root не должен зачищаться")

    def test_dry_run_missing_db_does_not_create_database(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            result_root = root / "data" / "result"
            result_root.mkdir(parents=True)
            missing_db = root / "missing.db"

            rc = cleanup_result_dir.cmd_cleanup(argparse.Namespace(
                db=missing_db, result_root=result_root, apply=False,
                abandoned_after_hours=24.0,
            ))

            self.assertEqual(rc, 2)
            self.assertFalse(missing_db.exists(),
                             "dry-run не должен создавать пустую БД")

    def test_symlink_result_root_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            outside = root / "outside"
            outside.mkdir()
            victim = outside / "victim.txt"
            victim.write_text("keep", encoding="utf-8")
            result_link = root / "result-link"
            result_link.symlink_to(outside, target_is_directory=True)
            db_path = root / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
            finally:
                conn.close()

            rc = cleanup_result_dir.cmd_cleanup(argparse.Namespace(
                db=db_path, result_root=result_link, apply=True,
                abandoned_after_hours=24.0,
            ))

            self.assertEqual(rc, 2)
            self.assertTrue(victim.exists(),
                            "symlink-root не должен позволять удаление снаружи")

    def test_apply_removes_generated_cache_in_known_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path, work_root, work_dir = self._setup_run(root)
            cache = work_dir / "__pycache__"
            cache.mkdir()
            (cache / "module.pyc").write_bytes(b"cache")

            rc = cleanup_result_dir.cmd_cleanup(argparse.Namespace(
                db=db_path, result_root=work_root, apply=True,
                abandoned_after_hours=24.0,
            ))

            self.assertEqual(rc, 0)
            self.assertFalse(cache.exists())

    def _make_old_orphan(self, root: Path) -> Path:
        work_dir = root / "data" / "result" / "p" / "model" / "old_1"
        work_dir.mkdir(parents=True)
        (work_dir / "run.log").write_text("orphan", encoding="utf-8")
        old = time.time() - 25 * 60 * 60
        os.utime(work_dir, (old, old))
        return work_dir

    def test_apply_removes_old_orphan_without_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            work_dir = self._make_old_orphan(root)
            db_path = root / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
            finally:
                conn.close()

            rc = cleanup_result_dir.cmd_cleanup(argparse.Namespace(
                db=db_path, result_root=root / "data" / "result", apply=True,
                abandoned_after_hours=24.0,
            ))

            self.assertEqual(rc, 0)
            self.assertFalse(work_dir.exists())

    def test_apply_keeps_old_orphan_with_live_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            work_dir = self._make_old_orphan(root)
            artifacts.write_run_active_marker(
                work_dir, pid=os.getpid(), started_at=time.time() - 25 * 60 * 60,
            )
            old = time.time() - 25 * 60 * 60
            os.utime(work_dir, (old, old))
            db_path = root / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
            finally:
                conn.close()

            rc = cleanup_result_dir.cmd_cleanup(argparse.Namespace(
                db=db_path, result_root=root / "data" / "result", apply=True,
                abandoned_after_hours=24.0,
            ))

            self.assertEqual(rc, 0)
            self.assertTrue(work_dir.exists())

    def test_apply_keeps_young_orphan(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            work_dir = root / "data" / "result" / "p" / "model" / "new_1"
            work_dir.mkdir(parents=True)
            (work_dir / "run.log").write_text("active-ish", encoding="utf-8")
            db_path = root / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
            finally:
                conn.close()

            rc = cleanup_result_dir.cmd_cleanup(argparse.Namespace(
                db=db_path, result_root=root / "data" / "result", apply=True,
                abandoned_after_hours=24.0,
            ))

            self.assertEqual(rc, 0)
            self.assertTrue(work_dir.exists())

    def test_apply_keeps_old_orphan_with_malformed_marker(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            work_dir = self._make_old_orphan(root)
            marker = work_dir / artifacts.RUN_ACTIVE_MARKER
            marker.write_text("not-json", encoding="utf-8")
            old = time.time() - 25 * 60 * 60
            os.utime(work_dir, (old, old))
            db_path = root / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
            finally:
                conn.close()

            err = io.StringIO()
            out = io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = cleanup_result_dir.cmd_cleanup(argparse.Namespace(
                    db=db_path, result_root=root / "data" / "result", apply=True,
                    abandoned_after_hours=24.0,
                ))

            self.assertEqual(rc, 0)
            self.assertTrue(work_dir.exists())
            self.assertIn("marker", out.getvalue().lower())


if __name__ == "__main__":
    unittest.main()

import json
import sys
import tempfile
import zipfile
from io import BytesIO
from pathlib import Path
import unittest

# `artifacts`/`db` лежат в корне (pytest держит его на sys.path); `run_artifacts`
# остаётся в scripts/ — добавляем её только ради него.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import artifacts
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
        self.assertEqual(set(by_path), {"run.log", "hello.py", "nested/data.bin"})
        self.assertEqual(by_path["run.log"].kind, "log")
        self.assertEqual(by_path["hello.py"].kind, "agent_file")
        self.assertEqual(collection.summary()["files"], 3)
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
        self.assertEqual(blob_count, 2)
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


if __name__ == "__main__":
    unittest.main()

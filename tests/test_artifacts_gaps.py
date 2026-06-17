"""Покрытие нетестированных функций artifacts.py (issue #38 P1).

Дополняет tests/test_artifacts.py точечными юнит-проверками четырёх функций:
_is_excluded_file (по каждому правилу исключения), collect_run_artifacts
(структура ArtifactCollection/RunArtifact, kinds, пропуск trash),
_prune_empty_dirs (удаление пустых деревьев) и cleanup_collected_artifacts
(удаление собранных файлов). Сеть/opencode не задействованы, вся работа с ФС —
через tempfile.TemporaryDirectory; data/main.db не трогается.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import artifacts


class IsExcludedFileTests(unittest.TestCase):
    def test_excluded_by_ds_store_name(self):
        self.assertTrue(artifacts._is_excluded_file(Path("/x/.DS_Store")))

    def test_excluded_by_pyc_suffix(self):
        self.assertTrue(artifacts._is_excluded_file(Path("/x/mod.pyc")))
        # Суффикс распознаётся независимо от каталога.
        self.assertTrue(artifacts._is_excluded_file(Path("/x/sub/another.pyc")))

    def test_not_excluded_ordinary_files(self):
        self.assertFalse(artifacts._is_excluded_file(Path("/x/hello.py")))
        self.assertFalse(artifacts._is_excluded_file(Path("/x/run.log")))
        self.assertFalse(artifacts._is_excluded_file(Path("/x/report.json")))
        # .DS_Store исключается только как basename; имя-подстрока — нет.
        self.assertFalse(artifacts._is_excluded_file(Path("/x/not.DS_Store.txt")))


class CollectRunArtifactsTests(unittest.TestCase):
    def test_returns_artifact_collection_with_expected_structure(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "run.log").write_text("log-line\n", encoding="utf-8")
            (root / "hello.py").write_text("print('hi')\n", encoding="utf-8")
            # Исключаемый по суффиксу файл — должен попасть в trash, не в artifacts.
            (root / "mod.pyc").write_bytes(b"\x00pyc")

            collection = artifacts.collect_run_artifacts(7, root)

        self.assertIsInstance(collection, artifacts.ArtifactCollection)
        self.assertEqual(collection.errors, [])
        # work_dirs хранит resolved-корень обхода (нужен cleanup-у).
        self.assertEqual(collection.work_dirs, [root.resolve()])

        by_path = {art.path: art for art in collection.artifacts}
        self.assertEqual(set(by_path), {"run.log", "hello.py"})

        log = by_path["run.log"]
        self.assertIsInstance(log, artifacts.RunArtifact)
        self.assertEqual(log.run_idx, 7)
        self.assertEqual(log.kind, artifacts.ARTIFACT_KIND_LOG)

        agent = by_path["hello.py"]
        self.assertEqual(agent.run_idx, 7)
        self.assertEqual(agent.kind, artifacts.ARTIFACT_KIND_AGENT_FILE)
        self.assertEqual(agent.content, b"print('hi')\n")
        self.assertEqual(agent.size_bytes, len(b"print('hi')\n"))
        self.assertEqual(agent.source_path, (root / "hello.py").resolve())

        # Исключённый .pyc отброшен в trash, а не собран.
        self.assertNotIn("mod.pyc", by_path)
        self.assertTrue(any(p.name == "mod.pyc" for p in collection.trash_paths))

    def test_missing_dir_yields_error_and_no_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "does_not_exist"
            collection = artifacts.collect_run_artifacts(1, missing)

        self.assertEqual(collection.artifacts, [])
        self.assertEqual(collection.work_dirs, [])
        self.assertTrue(any("missing" in err for err in collection.errors))


class PruneEmptyDirsTests(unittest.TestCase):
    def test_removes_empty_nested_dirs_keeps_non_empty(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            # Пустое вложенное дерево.
            empty_tree = root / "empty" / "deep" / "deeper"
            empty_tree.mkdir(parents=True)
            # Ветка с файлом — должна уцелеть целиком.
            kept = root / "kept"
            kept.mkdir()
            (kept / "file.txt").write_text("x", encoding="utf-8")

            artifacts._prune_empty_dirs(root)

            self.assertFalse((root / "empty").exists(),
                             "пустое дерево должно быть удалено")
            self.assertTrue(kept.exists())
            self.assertTrue((kept / "file.txt").exists())
            # Сам корень _prune_empty_dirs не трогает (os.walk не отдаёт корень).
            self.assertTrue(root.exists())

    def test_missing_root_is_noop(self):
        with tempfile.TemporaryDirectory() as td:
            missing = Path(td) / "nope"
            # Не должно бросать исключений.
            artifacts._prune_empty_dirs(missing)
            self.assertFalse(missing.exists())


class CleanupCollectedArtifactsTests(unittest.TestCase):
    def test_deletes_collected_files_and_prunes_emptied_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td) / "prov_model" / "20260101-000000_1"
            work_dir.mkdir(parents=True)
            (work_dir / "run.log").write_text("log", encoding="utf-8")
            (work_dir / "sub").mkdir()
            (work_dir / "sub" / "data.bin").write_bytes(b"\x00\x01")
            (work_dir / "mod.pyc").write_bytes(b"pyc")

            collection = artifacts.collect_run_artifacts(1, work_dir)
            artifacts.cleanup_collected_artifacts(collection)

            # Собранные файлы удалены.
            self.assertFalse((work_dir / "run.log").exists())
            self.assertFalse((work_dir / "sub" / "data.bin").exists())
            # Trash (.pyc) подметён.
            self.assertFalse((work_dir / "mod.pyc").exists())
            # Опустевшая папка копии удалена целиком (вместе с вложенной sub).
            self.assertFalse(work_dir.exists())
            # Родителя (папку модели) не трогаем.
            self.assertTrue((Path(td) / "prov_model").exists())


if __name__ == "__main__":
    unittest.main()

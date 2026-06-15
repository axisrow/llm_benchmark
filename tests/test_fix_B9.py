"""Регресс-тесты B9: пустая папка копии остаётся, если в прогоне нет top-level
артефакта.

cleanup_collected_artifacts строит кандидатов на rmdir только из
source_path.parent артефактов и из trash-путей. Если в копии нет ни одного
top-level файла (только вложенные файлы вроде src/main.py или только trash
вроде __pycache__/), сама папка копии (work_dir) не попадает в кандидаты и
после удаления вложенного содержимого остаётся пустой на диске.
"""

import tempfile
import unittest
from pathlib import Path

import artifacts


class CleanupEmptyWorkDirTests(unittest.TestCase):
    def test_cleanup_removes_work_dir_with_only_nested_file(self):
        # Случай A: в копии нет top-level файла — только src/main.py.
        with tempfile.TemporaryDirectory() as td:
            run_root = Path(td) / "prov_model"
            work_dir = run_root / "20260101-120000_1"
            (work_dir / "src").mkdir(parents=True)
            (work_dir / "src" / "main.py").write_text(
                "print('hi')\n", encoding="utf-8")

            collection = artifacts.collect_run_artifacts(1, work_dir)
            artifacts.cleanup_collected_artifacts(collection)

            self.assertFalse(
                work_dir.exists(),
                "опустевшая папка копии без top-level файла должна удаляться",
            )
            # Родителя (папку модели) не трогаем.
            self.assertTrue(run_root.exists())

    def test_cleanup_removes_work_dir_with_only_trash(self):
        # Случай B: в копии только trash-папка __pycache__/m.pyc.
        with tempfile.TemporaryDirectory() as td:
            run_root = Path(td) / "prov_model"
            work_dir = run_root / "20260101-120000_2"
            (work_dir / "__pycache__").mkdir(parents=True)
            (work_dir / "__pycache__" / "m.pyc").write_bytes(b"pyc")

            collection = artifacts.collect_run_artifacts(2, work_dir)
            artifacts.cleanup_collected_artifacts(collection)

            self.assertFalse(
                work_dir.exists(),
                "опустевшая папка копии (только trash) должна удаляться",
            )
            self.assertTrue(run_root.exists())


if __name__ == "__main__":
    unittest.main()

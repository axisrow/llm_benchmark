"""Регресс-тест B8: агентский report.json не должен молча теряться/удаляться.

Баг: 'report.json' лежал в _EXCLUDED_FILE_NAMES и матчился по basename на любой
глубине walked-дерева КОПИИ. Но оркестраторский report.json пишется в run_root
(РОДИТЕЛЬ папок копий), вне обходимого collect_run_artifacts дерева. Значит это
исключение не защищало ничего легитимного — оно лишь теряло агентский вывод
(в т.ч. вложенный out/report.json): не сохранялся в БД и удалялся с диска.
"""

import tempfile
import unittest
from pathlib import Path

import artifacts


class FixB8AgentReportJsonTests(unittest.TestCase):
    def test_top_level_report_json_collected_not_trashed(self):
        """report.json верхнего уровня копии должен попасть в артефакты, не в trash."""
        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td)
            (work_dir / "run.log").write_text("log line\n", encoding="utf-8")
            (work_dir / "report.json").write_text(
                '{"agent": "output"}', encoding="utf-8",
            )

            collection = artifacts.collect_run_artifacts(0, work_dir)

        collected = {artifact.path for artifact in collection.artifacts}
        self.assertEqual(collected, {"run.log", "report.json"})

        report = next(
            artifact for artifact in collection.artifacts
            if artifact.path == "report.json"
        )
        self.assertEqual(report.kind, artifacts.ARTIFACT_KIND_AGENT_FILE)

        trash_names = {path.name for path in collection.trash_paths}
        self.assertNotIn("report.json", trash_names)

    def test_nested_report_json_collected(self):
        """Вложенный out/report.json — тоже легитимный агентский вывод."""
        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td)
            (work_dir / "run.log").write_text("log\n", encoding="utf-8")
            (work_dir / "out").mkdir()
            (work_dir / "out" / "report.json").write_text(
                '{"nested": true}', encoding="utf-8",
            )

            collection = artifacts.collect_run_artifacts(0, work_dir)

        collected = {artifact.path for artifact in collection.artifacts}
        self.assertIn("out/report.json", collected)
        self.assertNotIn(
            "report.json",
            {path.name for path in collection.trash_paths},
        )

    def test_cleanup_does_not_drop_unsaved_report_json(self):
        """После cleanup агентский report.json либо сохранён в артефакты, либо цел.

        До фикса report.json попадал в trash и удалялся с диска, при этом
        отсутствуя в artifacts — то есть исчезал бесследно.
        """
        with tempfile.TemporaryDirectory() as td:
            work_dir = Path(td)
            (work_dir / "run.log").write_text("log\n", encoding="utf-8")
            report_path = work_dir / "report.json"
            report_path.write_text('{"agent": "output"}', encoding="utf-8")

            collection = artifacts.collect_run_artifacts(0, work_dir)

            # Инвариант: ничего не должно одновременно отсутствовать в artifacts
            # и удаляться cleanup-ом — иначе данные теряются.
            saved = {artifact.path for artifact in collection.artifacts}
            self.assertIn("report.json", saved)

            artifacts.cleanup_collected_artifacts(collection)

            # report.json сохранён как артефакт (его байты в БД), значит штатно
            # удалён с диска cleanup-ом — но не «потерян».
            self.assertTrue("report.json" in saved or report_path.exists())


if __name__ == "__main__":
    unittest.main()

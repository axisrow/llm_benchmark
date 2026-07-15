"""Сводка проекта считается по ВСЕМ отчётам (issue #121; ранее — регресс B4).

История: B4 вводил latest-wins (сводка по последнему отчёту на (provider,
model)), чтобы устаревший упавший переран не метил починенный проект. Issue #121
сменил семантику на дозапись: рейтинг и сводка проекта СУММИРУЮТ все отчёты,
фейлы не скрываются, а отражаются в success-rate; устаревшие/ошибочные отчёты
удаляются только вручную через scripts/delete_reports.py.
"""

import unittest

# Тело сборки index.json вынесено в conftest (issue #54 #9); возвращает (count, data).
from conftest import build_index_data as _build_index_data


class FixB4Tests(unittest.TestCase):
    def test_project_summary_sums_all_reports_of_model(self):
        # Один проект p, одна модель m: старый упавший прогон + свежий чистый.
        reports = [
            {
                "project": "p",
                "provider": "prov",
                "model": "m",
                "started_at": "2026-01-01T00:00:00",
                "summary": {"ok": 0, "timeout": 2, "error": 0, "rate_limited": 0},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": [
                    {"index": 1, "code": 1, "elapsed": 60.0},
                    {"index": 2, "code": 1, "elapsed": 60.0},
                ],
            },
            {
                "project": "p",
                "provider": "prov",
                "model": "m",
                "started_at": "2026-01-02T00:00:00",
                "summary": {"ok": 2, "timeout": 0, "error": 0, "rate_limited": 0},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": [
                    {"index": 1, "code": 0, "elapsed": 1.0},
                    {"index": 2, "code": 0, "elapsed": 2.0},
                ],
            },
        ]

        count, data = _build_index_data(reports)
        project = data["projects"][0]

        self.assertEqual(count, 2)
        # issue #121: сводка суммирует ВСЮ историю ячейки — и успехи, и фейлы.
        self.assertEqual(project["summary"]["ok"], 2)
        self.assertEqual(project["summary"]["timeout"], 2)
        self.assertEqual(project["summary"]["error"], 0)
        self.assertEqual(project["summary"]["rate_limited"], 0)
        # run_count тоже по всей истории: 4 прогона.
        self.assertEqual(project["run_count"], 4)

    def test_project_summary_aggregates_across_distinct_models(self):
        # Две разные модели в проекте — обе идут в сводку (не теряем данные).
        reports = [
            {
                "project": "p",
                "provider": "prov",
                "model": "m1",
                "started_at": "2026-01-02T00:00:00",
                "summary": {"ok": 1, "timeout": 0, "error": 0, "rate_limited": 0},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": [{"index": 1, "code": 0, "elapsed": 1.0}],
            },
            {
                "project": "p",
                "provider": "prov",
                "model": "m2",
                "started_at": "2026-01-02T00:00:00",
                "summary": {"ok": 0, "timeout": 1, "error": 0, "rate_limited": 0},
                "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
                "runs": [{"index": 1, "code": 1, "elapsed": 60.0}],
            },
        ]

        _, data = _build_index_data(reports)
        project = data["projects"][0]

        self.assertEqual(project["summary"]["ok"], 1)
        self.assertEqual(project["summary"]["timeout"], 1)
        self.assertEqual(project["run_count"], 2)


if __name__ == "__main__":
    unittest.main()

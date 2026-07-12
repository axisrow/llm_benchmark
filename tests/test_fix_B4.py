"""Регресс-тест B4: сводка проекта должна считаться по ПОСЛЕДНЕМУ отчёту
на (provider, model), а не суммировать всю историю переран'ов.

Баг: group_by_project/_accumulate_summary копили summary и run_count по
КАЖДОМУ отчёту проекта (включая устаревшие упавшие переран'ы той же модели),
в противоречие build_model_ranking, который дедуплицирует до последнего отчёта
на (project, provider, model). Из-за этого починенный проект (свежий чистый
отчёт + старый упавший) навсегда показывался упавшим на фронте.
"""

import unittest

# Тело сборки index.json вынесено в conftest (issue #54 #9); возвращает (count, data).
from conftest import build_index_data as _build_index_data


class FixB4Tests(unittest.TestCase):
    def test_project_summary_uses_only_latest_report_per_model(self):
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
        # Сводка считается ТОЛЬКО по последнему (чистому) отчёту модели m.
        self.assertEqual(project["summary"]["ok"], 2)
        self.assertEqual(project["summary"]["timeout"], 0)
        self.assertEqual(project["summary"]["error"], 0)
        self.assertEqual(project["summary"]["rate_limited"], 0)
        # run_count тоже по последнему отчёту: 2 прогона, а не 4 за всю историю.
        self.assertEqual(project["run_count"], 2)

    def test_project_summary_aggregates_across_distinct_models(self):
        # Две разные модели в проекте — обе latest идут в сводку (не теряем данные).
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

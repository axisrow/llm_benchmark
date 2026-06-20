"""Регресс-тест B5: порядок projects[].reports[] в index.json должен быть
детерминированным при равном started_at.

Баг: load_reports делал ORDER BY started_at DESC без вторичного ключа. При
совпадении started_at у нескольких отчётов SQLite возвращает ties в порядке
rowid (физических строк), который нестабилен между VACUUM/реимпортом. CLAUDE.md
требует байт-в-байт воспроизводимости index.json.
"""

import unittest

from conftest import build_index_data


def _build_index_data(reports):
    """index.json из списка отчётов — тело в conftest (issue #54 #9). B5 нужен
    только data (порядок reports[]), поэтому отбрасываем count."""
    return build_index_data(reports)[1]


def _report(model):
    # У всех отчётов ОДИНАКОВЫЙ started_at — провоцируем ties по сортировке.
    return {
        "project": "p",
        "provider": "prov",
        "model": model,
        "started_at": "2026-01-01T00:00:00",
        "summary": {"ok": 1, "timeout": 0, "error": 0, "rate_limited": 0},
        "pricing": {"prompt_per_1m": 0.0, "completion_per_1m": 0.0},
        "runs": [{"index": 1, "code": 0, "elapsed": 1.0}],
    }


class FixB5Tests(unittest.TestCase):
    def test_reports_order_is_deterministic_under_equal_started_at(self):
        # Одни и те же 4 отчёта, вставленные в ПРОТИВОПОЛОЖНЫХ порядках вставки.
        forward = _build_index_data([_report(m) for m in ("a", "b", "c", "d")])
        backward = _build_index_data([_report(m) for m in ("d", "c", "b", "a")])

        order_forward = [r["model"] for r in forward["projects"][0]["reports"]]
        order_backward = [r["model"] for r in backward["projects"][0]["reports"]]

        # Порядок должен быть идентичен независимо от порядка вставки.
        self.assertEqual(order_forward, order_backward)
        # И при этом отсортирован по детерминированному ключу (provider, model).
        self.assertEqual(order_forward, ["a", "b", "c", "d"])


if __name__ == "__main__":
    unittest.main()

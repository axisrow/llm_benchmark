"""Тест issue #54 (находка #5): построитель сводки из единой таксономии RUN_CODES.

Раньше счётчики/ярлыки сводки хардкодились в benchmark_report и check_models.
summary_counts/summary_line живут рядом с RUN_CODES — 5-й код исхода подхватится
автоматически, без правки во всех местах.
"""

import unittest

import opencode_runtime as rt


class SummaryHelpersTests(unittest.TestCase):
    def test_summary_counts_taxonomy(self):
        self.assertEqual(
            rt.summary_counts([0, 0, 1, 3]),
            {"ok": 2, "timeout": 1, "error": 0, "rate_limited": 1})

    def test_summary_counts_keys_are_run_codes(self):
        # Ключи ровно из RUN_CODES — добавление кода туда расширит сводку везде.
        self.assertEqual(
            set(rt.summary_counts([])),
            {key for _code, (key, _label) in rt.RUN_CODES.items()})

    def test_summary_line_default_labels_with_total(self):
        line = rt.summary_line(rt.summary_counts([0, 0, 1, 3]), total=5)
        self.assertEqual(line, "2 готово / 1 таймаут / 0 ошибка / 1 лимит (из 5)")

    def test_summary_line_label_override_no_total(self):
        # check_models показывает «доступно» вместо «готово» для ok.
        line = rt.summary_line(rt.summary_counts([0, 1, 3]),
                               labels={"ok": "доступно"})
        self.assertEqual(line, "1 доступно / 1 таймаут / 0 ошибка / 1 лимит")


if __name__ == "__main__":
    unittest.main()

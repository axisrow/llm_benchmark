"""Юнит-покрытие нетронутых хелперов usage.py (issue #38, P1).

Закрывает: field, as_token, as_money, merge_usages, summarize_usages,
format_tokens. Чистые функции — без сети и без data/main.db.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from usage import (
    Usage,
    as_money,
    as_token,
    field,
    format_tokens,
    merge_usages,
    summarize_usages,
)


class _Obj:
    """Лёгкий стенд под SDK-объект для getattr-ветки field()."""

    def __init__(self, **kw):
        self.__dict__.update(kw)


class FieldTests(unittest.TestCase):
    def test_dict_hit(self):
        self.assertEqual(field({"input": 7}, "input"), 7)

    def test_dict_missing_returns_none(self):
        self.assertIsNone(field({"input": 7}, "output"))

    def test_attr_hit(self):
        self.assertEqual(field(_Obj(cost=1.5), "cost"), 1.5)

    def test_attr_missing_returns_none(self):
        self.assertIsNone(field(_Obj(cost=1.5), "tokens"))

    def test_dict_value_can_be_none(self):
        # Явный None в dict возвращается как None, а не как «нет ключа».
        self.assertIsNone(field({"input": None}, "input"))


class AsTokenTests(unittest.TestCase):
    def test_int(self):
        self.assertEqual(as_token(42), 42)

    def test_numeric_string(self):
        self.assertEqual(as_token("123"), 123)

    def test_float_truncates_toward_zero(self):
        self.assertEqual(as_token(3.9), 3)

    def test_float_string(self):
        self.assertEqual(as_token("7.6"), 7)

    def test_none(self):
        self.assertIsNone(as_token(None))

    def test_garbage_string(self):
        self.assertIsNone(as_token("abc"))

    def test_bool_is_rejected(self):
        # bool — подкласс int, но usage.py намеренно его отсекает.
        self.assertIsNone(as_token(True))
        self.assertIsNone(as_token(False))

    def test_negative_passes_through(self):
        # Отрицательные не фильтруются — поведение «как есть».
        self.assertEqual(as_token(-5), -5)

    def test_non_finite_rejected(self):
        self.assertIsNone(as_token(float("inf")))
        self.assertIsNone(as_token(float("nan")))


class AsMoneyTests(unittest.TestCase):
    def test_int(self):
        self.assertEqual(as_money(3), 3.0)
        self.assertIsInstance(as_money(3), float)

    def test_float(self):
        self.assertEqual(as_money(2.5), 2.5)

    def test_numeric_string(self):
        self.assertEqual(as_money("0.125"), 0.125)

    def test_none(self):
        self.assertIsNone(as_money(None))

    def test_garbage_string(self):
        self.assertIsNone(as_money("free"))

    def test_bool_is_rejected(self):
        self.assertIsNone(as_money(True))

    def test_negative_passes_through(self):
        self.assertEqual(as_money(-1.5), -1.5)

    def test_non_finite_rejected(self):
        self.assertIsNone(as_money(float("inf")))
        self.assertIsNone(as_money(float("nan")))


class MergeUsagesTests(unittest.TestCase):
    def test_empty_is_none(self):
        self.assertIsNone(merge_usages([]))

    def test_sums_token_fields(self):
        merged = merge_usages([
            Usage(input_tokens=1, output_tokens=2, reasoning_tokens=3,
                  cache_read_tokens=4, cache_write_tokens=5),
            Usage(input_tokens=10, output_tokens=20, reasoning_tokens=30,
                  cache_read_tokens=40, cache_write_tokens=50),
        ])
        self.assertEqual(merged.input_tokens, 11)
        self.assertEqual(merged.output_tokens, 22)
        self.assertEqual(merged.reasoning_tokens, 33)
        self.assertEqual(merged.cache_read_tokens, 44)
        self.assertEqual(merged.cache_write_tokens, 55)
        self.assertEqual(merged.total_tokens, 11 + 22 + 33)

    def test_sums_opencode_cost(self):
        merged = merge_usages([
            Usage(opencode_cost_usd=0.10),
            Usage(opencode_cost_usd=0.05),
        ])
        self.assertAlmostEqual(merged.opencode_cost_usd, 0.15)

    def test_cost_none_when_all_missing(self):
        merged = merge_usages([Usage(input_tokens=1), Usage(input_tokens=2)])
        self.assertIsNone(merged.opencode_cost_usd)

    def test_cost_partial_only_sums_present(self):
        # None-стоимости пропускаются; суммируется только присутствующая.
        merged = merge_usages([
            Usage(opencode_cost_usd=0.20),
            Usage(opencode_cost_usd=None),
        ])
        self.assertAlmostEqual(merged.opencode_cost_usd, 0.20)

    def test_does_not_merge_estimated_cost(self):
        # merge_usages не переносит estimated_*; они остаются дефолтными None.
        merged = merge_usages([Usage(estimated_cost_usd=1.0)])
        self.assertIsNone(merged.estimated_cost_usd)


class SummarizeUsagesTests(unittest.TestCase):
    def test_keys_present(self):
        result = summarize_usages([Usage(input_tokens=1)])
        self.assertEqual(set(result), {
            "input_tokens", "output_tokens", "reasoning_tokens",
            "total_tokens", "estimated_cost_usd",
            "runs_with_usage", "runs_with_estimated_cost",
        })

    def test_all_none_entries(self):
        result = summarize_usages([None, None])
        self.assertIsNone(result["input_tokens"])
        self.assertIsNone(result["total_tokens"])
        self.assertIsNone(result["estimated_cost_usd"])
        self.assertEqual(result["runs_with_usage"], 0)
        self.assertEqual(result["runs_with_estimated_cost"], 0)

    def test_skips_none_and_aggregates(self):
        result = summarize_usages([
            None,
            Usage(input_tokens=2, output_tokens=3, reasoning_tokens=1,
                  estimated_cost_usd=0.5),
            Usage(input_tokens=8, output_tokens=7, reasoning_tokens=0,
                  estimated_cost_usd=0.25),
        ])
        self.assertEqual(result["input_tokens"], 10)
        self.assertEqual(result["output_tokens"], 10)
        self.assertEqual(result["reasoning_tokens"], 1)
        self.assertEqual(result["total_tokens"], 21)
        self.assertAlmostEqual(result["estimated_cost_usd"], 0.75)
        self.assertEqual(result["runs_with_usage"], 2)
        self.assertEqual(result["runs_with_estimated_cost"], 2)

    def test_counts_runs_with_estimated_cost(self):
        # Один прогон без estimated_cost_usd не идёт в runs_with_estimated_cost.
        result = summarize_usages([
            Usage(input_tokens=1, estimated_cost_usd=0.1),
            Usage(input_tokens=1),
        ])
        self.assertEqual(result["runs_with_usage"], 2)
        self.assertEqual(result["runs_with_estimated_cost"], 1)
        self.assertAlmostEqual(result["estimated_cost_usd"], 0.1)


class FormatTokensTests(unittest.TestCase):
    def test_thousands_separator(self):
        self.assertEqual(format_tokens(1234567), "1,234,567")

    def test_zero(self):
        self.assertEqual(format_tokens(0), "0")

    def test_none(self):
        self.assertEqual(format_tokens(None), "N/A")

    def test_garbage(self):
        self.assertEqual(format_tokens("oops"), "N/A")

    def test_numeric_string_is_formatted(self):
        # format_tokens проходит через as_token, поэтому строки-числа работают.
        self.assertEqual(format_tokens("1000"), "1,000")

    def test_float_truncated_then_formatted(self):
        self.assertEqual(format_tokens(1999.9), "1,999")


if __name__ == "__main__":
    unittest.main()

"""Регресс-тесты бага B6: `_resolve_catalog_id` игнорирует приоритет тиров и
недетерминирован по порядку строк БД.

Докстринг `_resolve_catalog_id` обещает строгий приоритет тиров:
1) явный alias, 2) точный ключ `provider/model`, 3) `model` как id,
4) leaf/суффикс-поиск. `:free` проигрывает платному лишь как финальный тай-брейк
ВНУТРИ тира. Старая реализация сплющивала все совпадения в один список и брала
`min(..., key=endswith(':free'))`, из-за чего leaf-совпадение чужого вендора
могло побить точный ключ, а среди равных платных результат зависел от порядка
итерации dict (== физический порядок строк кэш-SELECT без ORDER BY).
"""

import unittest

import pricing


class ResolveCatalogIdTierPriorityTests(unittest.TestCase):
    """Тиры должны соблюдаться, а тай-брейк внутри тира — быть детерминированным."""

    def test_exact_key_beats_leaf_of_other_vendor(self):
        # 'aaa/m' (leaf 'm', тир 4) вставлен ПЕРВЫМ и тоже платный; 'prov/m' —
        # точный ключ (тир 2). Точный ключ обязан победить, а не первый по
        # порядку dict leaf-кандидат.
        cache = {
            "aaa/m": {"prompt": "1", "completion": "2"},
            "prov/m": {"prompt": "5", "completion": "6"},
        }
        result = pricing._resolve_catalog_id(cache, "prov/m", "m", {})
        self.assertEqual(result, "prov/m")

    def test_exact_key_beats_leaf_regardless_of_insertion_order(self):
        # Тот же набор, но точный ключ вставлен ПЕРВЫМ — результат не меняется.
        cache = {
            "prov/m": {"prompt": "5", "completion": "6"},
            "aaa/m": {"prompt": "1", "completion": "2"},
        }
        result = pricing._resolve_catalog_id(cache, "prov/m", "m", {})
        self.assertEqual(result, "prov/m")

    def test_leaf_tiebreak_is_deterministic_across_dict_order(self):
        # Точного ключа нет → срабатывает тир 4 (leaf-поиск). Два равноприоритетных
        # платных leaf-кандидата ('azure/gpt-4' и 'openai/gpt-4'): выбор НЕ должен
        # зависеть от порядка вставки в dict.
        cache_a = {
            "azure/gpt-4": {"prompt": "1", "completion": "2"},
            "openai/gpt-4": {"prompt": "3", "completion": "4"},
        }
        cache_b = {
            "openai/gpt-4": {"prompt": "3", "completion": "4"},
            "azure/gpt-4": {"prompt": "1", "completion": "2"},
        }
        result_a = pricing._resolve_catalog_id(cache_a, "none/gpt-4", "gpt-4", {})
        result_b = pricing._resolve_catalog_id(cache_b, "none/gpt-4", "gpt-4", {})
        self.assertEqual(result_a, result_b)

    def test_model_as_id_beats_leaf_of_other_vendor(self):
        # `model` уже в формате 'vendor/model' (тир 3) — должен победить чужой
        # leaf-вариант (тир 4), независимо от порядка строк.
        cache = {
            "aaa/model": {"prompt": "1", "completion": "2"},
            "vendor/model": {"prompt": "5", "completion": "6"},
        }
        result = pricing._resolve_catalog_id(cache, "prov/x", "vendor/model", {})
        self.assertEqual(result, "vendor/model")

    def test_paid_preferred_over_free_within_leaf_tier(self):
        # Тай-брейк платный > :free сохраняется внутри тира (не сломан фиксом).
        cache = {
            "vendor/m:free": {"prompt": "0", "completion": "0"},
            "vendor/m": {"prompt": "1", "completion": "2"},
        }
        result = pricing._resolve_catalog_id(cache, "prov/m", "m", {})
        self.assertEqual(result, "vendor/m")


if __name__ == "__main__":
    unittest.main()

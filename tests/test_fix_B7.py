"""Регресс-тест B7: пустой успешный ответ OpenRouter не должен затирать кэш.

Баг: при УСПЕШНОМ вызове `models.list()`, вернувшем пустой `data` (или все
модели без pricing), `refresh_cache` всё равно выполнял destructive write —
`DELETE FROM openrouter_cache` + бамп `openrouter_cache_meta.fetched_at`. Это
уничтожало валидные цены и метило пустой кэш свежим на 24ч, из-за чего сеть не
перезапрашивалась, а `get_pricing` отдавал `None` для всего каталога.

Правильное поведение: при пустом результате успешного запроса прежний кэш
сохраняется (как в ветке сетевой ошибки) — без DELETE и без бампа fetched_at.
"""

import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import db
import pricing


class EmptyOpenRouterResponseTests(unittest.TestCase):
    """Пустой успешный ответ OpenRouter не должен трогать валидный кэш."""

    def _seed_db(self, db_path: Path) -> None:
        """Засевает кэш: устаревшую мету (fetched_at=0) и одну валидную цену."""
        conn = db.connect(db_path)
        try:
            db.init_schema(conn)
            with conn:
                conn.execute(
                    "INSERT INTO openrouter_cache_meta (id, fetched_at) VALUES (1, 0)",
                )
                conn.execute(
                    "INSERT INTO openrouter_cache (model_id, prompt, completion) "
                    "VALUES ('good/model', '1', '2')",
                )
        finally:
            conn.close()

    @staticmethod
    def _fake_openrouter(data):
        """Фабрика мока OpenRouter, у которого models.list().data == `data`."""

        class FakeModels:
            def list(self):
                return SimpleNamespace(data=data)

        class FakeOpenRouter:
            def __init__(self, *args, **kwargs):
                self.models = FakeModels()

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

        return FakeOpenRouter

    def test_empty_data_keeps_previous_cache(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            self._seed_db(db_path)

            original_connect = pricing.connect
            original_openrouter = pricing.OpenRouter
            try:
                pricing.connect = lambda: db.connect(db_path)
                pricing.OpenRouter = self._fake_openrouter([])  # успех, но пусто
                pricing._read_cached_models.cache_clear()
                pricing.refresh_cache.cache_clear()

                # Предусловие: старый валидный кэш на месте.
                self.assertIn("good/model", pricing._read_cached_models())

                pricing.refresh_cache()

                # Считываем мету ДО восстановления connect.
                conn = db.connect(db_path)
                try:
                    meta = conn.execute(
                        "SELECT fetched_at FROM openrouter_cache_meta WHERE id = 1"
                    ).fetchone()
                finally:
                    conn.close()

                pricing._read_cached_models.cache_clear()
                cached = pricing._read_cached_models()
            finally:
                pricing.connect = original_connect
                pricing.OpenRouter = original_openrouter
                pricing._read_cached_models.cache_clear()
                pricing.refresh_cache.cache_clear()

        # Прежний кэш сохранён, пустой ответ его не затёр.
        self.assertIn("good/model", cached)
        self.assertEqual(cached["good/model"], {"prompt": "1", "completion": "2"})
        # fetched_at не помечен свежим — кэш по-прежнему «протух» (остался 0).
        self.assertEqual(meta["fetched_at"], 0)

    def test_all_models_without_pricing_keep_previous_cache(self):
        # Аналогично: ответ непустой, но у всех моделей pricing is None →
        # отфильтрованный models пуст; кэш трогать нельзя.
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            self._seed_db(db_path)

            data = [SimpleNamespace(id="broken/model", pricing=None)]
            original_connect = pricing.connect
            original_openrouter = pricing.OpenRouter
            try:
                pricing.connect = lambda: db.connect(db_path)
                pricing.OpenRouter = self._fake_openrouter(data)
                pricing._read_cached_models.cache_clear()
                pricing.refresh_cache.cache_clear()

                pricing.refresh_cache()

                pricing._read_cached_models.cache_clear()
                cached = pricing._read_cached_models()
            finally:
                pricing.connect = original_connect
                pricing.OpenRouter = original_openrouter
                pricing._read_cached_models.cache_clear()
                pricing.refresh_cache.cache_clear()

        self.assertIn("good/model", cached)

    def test_nonempty_response_still_updates_cache(self):
        # Обычный путь не сломан: непустой ответ обновляет кэш и мету.
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            self._seed_db(db_path)

            data = [SimpleNamespace(
                id="new/model",
                pricing=SimpleNamespace(prompt="3", completion="4"),
            )]
            original_connect = pricing.connect
            original_openrouter = pricing.OpenRouter
            before = time.time()
            try:
                pricing.connect = lambda: db.connect(db_path)
                pricing.OpenRouter = self._fake_openrouter(data)
                pricing._read_cached_models.cache_clear()
                pricing.refresh_cache.cache_clear()

                pricing.refresh_cache()

                conn = db.connect(db_path)
                try:
                    meta = conn.execute(
                        "SELECT fetched_at FROM openrouter_cache_meta WHERE id = 1"
                    ).fetchone()
                finally:
                    conn.close()

                pricing._read_cached_models.cache_clear()
                cached = pricing._read_cached_models()
            finally:
                pricing.connect = original_connect
                pricing.OpenRouter = original_openrouter
                pricing._read_cached_models.cache_clear()
                pricing.refresh_cache.cache_clear()

        self.assertNotIn("good/model", cached)
        self.assertEqual(cached["new/model"], {"prompt": "3", "completion": "4"})
        self.assertGreaterEqual(meta["fetched_at"], before)


if __name__ == "__main__":
    unittest.main()

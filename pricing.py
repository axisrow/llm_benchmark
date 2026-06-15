"""Каталог цен LLM-моделей поверх OpenRouter SDK.

Публичный эндпоинт `GET /models` отдаёт цены без авторизации — SDK нужен
только для типизированного доступа.  Для моделей не из OpenRouter (opencode,
ollama-cloud, zai-coding-plan, github-copilot) используются ручные цены из
таблиц базы (`price_overrides`/`price_aliases`/`provider_notes`). Кэш каталога
OpenRouter тоже живёт в базе (`openrouter_cache`/`openrouter_cache_meta`).
"""

import functools
import logging
import time

from openrouter import OpenRouter

from db import connect, init_schema

log = logging.getLogger(__name__)

# SDK требует непустой api_key для хедера Authorization; для публичного
# /models подойдёт фиктивный — сервер его не валидирует.
_DUMMY_KEY = "sk-or-price-lookup"

# Максимальный возраст кэша в секундах (24 часа).
_CACHE_TTL = 24 * 3600
_OPENROUTER_TIMEOUT_MS = 5000

# $0.10: ниже этого порога показываем 4 десятичных знака вместо 2.
PRICE_DETAIL_THRESHOLD = 0.1


def empty_pricing(note: str | None = None) -> dict:
    """Единая форма «цена неизвестна»: `{prompt_per_1m, completion_per_1m, note?}`.

    Совпадает с тем, что отдаёт `get_pricing` в своей None-ветке, чтобы потребители
    (`format_price_display`, `index_builder`) видели одинаковый набор ключей."""
    pricing = {"prompt_per_1m": None, "completion_per_1m": None}
    if note is not None:
        pricing["note"] = note
    return pricing


def _str_to_per_1m(s: str | None) -> float | None:
    """Конвертация строки USD/токен → USD за 1M токенов."""
    if s is None:
        return None
    try:
        return float(s) * 1_000_000
    except (ValueError, TypeError):
        return None


@functools.lru_cache(maxsize=1)
def _read_cached_models() -> dict[str, dict]:
    """Читает models из таблицы кэша; пустой dict при отсутствии/ошибке.

    Мемоизировано на процесс, как `refresh_cache`/`_load_local_prices`: каталог
    read-only в рамках сборки. Без этого `build_index` открывал бы соединение и
    сканировал таблицу заново на каждый отчёт без цены (ветка `refresh=False`)."""
    try:
        conn = connect()
        try:
            rows = conn.execute(
                "SELECT model_id, prompt, completion FROM openrouter_cache"
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Не удалось прочитать кэш цен из базы: %s", exc)
        return {}
    return {r["model_id"]: {"prompt": r["prompt"], "completion": r["completion"]}
            for r in rows}


@functools.lru_cache(maxsize=1)
def refresh_cache() -> dict[str, dict]:
    """Запрашивает каталог моделей у OpenRouter и кэширует его в базе.

    Возвращает `{model_id: {"prompt": <str>, "completion": <str>}}`.
    Результат мемоизируется на время жизни процесса (каталог read-only).
    При ошибке сети или невалидном ответе — возвращает предыдущий кэш
    (или пустой dict), бенчмарк не падает.
    """
    # Свежий кэш в базе — используем его, без сетевого запроса. Мету и модели
    # читаем одним соединением (горячая ветка: вызывается на каждую модель).
    try:
        conn = connect()
        try:
            meta = conn.execute(
                "SELECT fetched_at FROM openrouter_cache_meta WHERE id = 1"
            ).fetchone()
            fetched_at = meta["fetched_at"] if meta else 0
            if isinstance(fetched_at, (int, float)) and time.time() - fetched_at < _CACHE_TTL:
                cached = {r["model_id"]: {"prompt": r["prompt"], "completion": r["completion"]}
                          for r in conn.execute(
                              "SELECT model_id, prompt, completion FROM openrouter_cache")}
                if cached:
                    return cached
        finally:
            conn.close()
    except Exception as exc:
        # Не падаем — ниже сходим в сеть; но не молчим, чтобы причина «почему
        # кэш не использован» (БД заблокирована/не инициализирована) была видна.
        log.warning("Не удалось прочитать свежий кэш цен из базы: %s", exc)

    try:
        with OpenRouter(api_key=_DUMMY_KEY, timeout_ms=_OPENROUTER_TIMEOUT_MS) as client:
            res = client.models.list()
        # Пропускаем записи без pricing — одна «битая» модель не должна ронять
        # сборку всего каталога (иначе fetch отдаст пустой фолбэк на весь процесс).
        models = {m.id: {"prompt": m.pricing.prompt, "completion": m.pricing.completion}
                  for m in res.data if m.pricing is not None}
    except Exception as exc:
        log.warning("Не удалось обновить кэш OpenRouter: %s", exc)
        return _read_cached_models()  # старый кэш как фолбэк

    # Успех, но каталог пуст (data == [] или у всех моделей pricing is None).
    # Не делаем destructive write: иначе DELETE затрёт валидные цены, а бамп
    # fetched_at пометит пустой кэш свежим на сутки. Ведём себя как ветка выше —
    # сохраняем прежний кэш.
    if not models:
        log.warning("OpenRouter вернул пустой каталог — прежний кэш цен сохранён")
        return _read_cached_models()

    try:
        conn = connect()
        try:
            init_schema(conn)
            with conn:
                conn.execute("DELETE FROM openrouter_cache")
                conn.executemany(
                    "INSERT INTO openrouter_cache (model_id, prompt, completion) "
                    "VALUES (?, ?, ?)",
                    [(mid, e["prompt"], e["completion"]) for mid, e in models.items()],
                )
                conn.execute(
                    "INSERT INTO openrouter_cache_meta (id, fetched_at) VALUES (1, ?) "
                    "ON CONFLICT (id) DO UPDATE SET fetched_at = excluded.fetched_at",
                    (time.time(),),
                )
            _read_cached_models.cache_clear()
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Не удалось записать кэш в базу: %s", exc)

    return models


@functools.lru_cache(maxsize=1)
def _load_local_prices() -> dict:
    """Собирает ручные цены из таблиц базы:
    `{overrides, catalog_aliases, provider_notes}` (мемоизировано на процесс).
    Пустые dict'ы при ошибке — бенчмарк не падает."""
    try:
        conn = connect()
        try:
            overrides = {
                r["key"]: {"prompt_per_1m": r["prompt_per_1m"],
                           "completion_per_1m": r["completion_per_1m"]}
                for r in conn.execute(
                    "SELECT key, prompt_per_1m, completion_per_1m FROM price_overrides")
            }
            aliases = {r["local_key"]: r["openrouter_id"] for r in conn.execute(
                "SELECT local_key, openrouter_id FROM price_aliases")}
            notes = {r["provider"]: r["note"] for r in conn.execute(
                "SELECT provider, note FROM provider_notes")}
        finally:
            conn.close()
    except Exception as exc:
        log.warning("Не удалось прочитать ручные цены из базы: %s", exc)
        return {}
    return {"overrides": overrides, "catalog_aliases": aliases,
            "provider_notes": notes}


def _resolve_catalog_id(cache: dict, key: str, model: str, aliases: dict) -> str | None:
    """Подбирает id модели в каталоге OpenRouter по приоритету:
    1. явный alias; 2. точный ключ `provider/model`; 3. сама `model` как id
    (когда она уже в формате `vendor/model[:free]`); 4. суффикс-поиск по
    последнему сегменту имени. Среди равных платный вариант важнее `:free`.
    """
    # Тир 1: явный alias.
    if key in aliases:
        return aliases[key]
    # Тир 2: точный ключ `provider/model`.
    if key in cache:
        return key
    # Тир 3: `model` уже в формате `vendor/model[:free]` — берём как id.
    if model in cache:
        return model
    # Тир 4: суффикс-поиск по последнему сегменту имени (без `:free`). Возвращаем
    # на первом сработавшем тире, а не сплющиваем все совпадения в один список:
    # иначе чужой вендор с тем же leaf побивал бы точный ключ.
    leaf = model.rsplit("/", 1)[-1].removesuffix(":free")
    candidates = [c for c in cache
                  if c.rsplit("/", 1)[-1].removesuffix(":free") == leaf]
    if not candidates:
        return None
    # Ничего бесплатного не бывает: платный аналог приоритетнее `:free`. При
    # равенстве выбираем детерминированно (sorted), а не по порядку строк БД.
    return min(sorted(candidates), key=lambda c: c.endswith(":free"))


def get_pricing(provider: str, model: str, *, refresh: bool = True) -> dict:
    """Возвращает `{prompt_per_1m, completion_per_1m, note?}` для модели.

    В отчёт пишем рыночную цену модели по каталогу OpenRouter независимо от
    того, через какой провайдер она тестировалась (подписка/self-hosted/free —
    лишь способ гонять тесты дешевле). Порядок поиска:
    1. price_overrides (ручная цена для моделей, которых нет в каталоге).
    2. Каталог OpenRouter (см. `_resolve_catalog_id`): alias → точный ключ →
       `model` как id → суффикс-поиск; платный аналог важнее `:free`.
    3. provider_notes — фолбэк для моделей, которых в каталоге нет вообще.
    4. Иначе → цены `None`.
    `refresh=False` читает только локальный кэш из базы: это нужно для
    детерминированной сборки статического индекса без сетевого ожидания.
    """
    key = f"{provider}/{model}"
    local = _load_local_prices()

    entry = local.get("overrides", {}).get(key)
    if entry is not None:
        return {"prompt_per_1m": entry.get("prompt_per_1m"),
                "completion_per_1m": entry.get("completion_per_1m")}

    cache = refresh_cache() if refresh else _read_cached_models()
    catalog_id = _resolve_catalog_id(cache, key, model, local.get("catalog_aliases", {}))
    entry = cache.get(catalog_id) if catalog_id else None
    if entry is not None:
        return {"prompt_per_1m": _str_to_per_1m(entry.get("prompt")),
                "completion_per_1m": _str_to_per_1m(entry.get("completion"))}

    note = local.get("provider_notes", {}).get(provider)
    return empty_pricing(note)


def _fmt_usd(value: float) -> str:
    """Цена за 1M токенов: 2 знака, но 4 для суб-десятицентовых, чтобы
    дешёвые модели не округлялись в «$0.00». Та же логика в docs/index.html."""
    return f"${value:.4f}" if value < PRICE_DETAIL_THRESHOLD else f"${value:.2f}"


def format_price_display(pricing: dict) -> str:
    """Человекочитаемая строка цены для stdout."""
    prompt = pricing.get("prompt_per_1m")
    comp = pricing.get("completion_per_1m")

    if prompt is None or comp is None:
        note = pricing.get("note")
        return f"N/A ({note})" if note else "N/A"

    if prompt == 0.0 and comp == 0.0:
        return "Free"

    return f"{_fmt_usd(prompt)} / {_fmt_usd(comp)} за 1M токенов (prompt/completion)"

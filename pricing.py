"""Каталог цен LLM-моделей поверх OpenRouter SDK.

Публичный эндпоинт `GET /models` отдаёт цены без авторизации — SDK нужен
только для типизированного доступа.  Для моделей не из OpenRouter (opencode,
ollama-cloud, zai-coding-plan, github-copilot) используется локальный
`prices.json` с ручными ценами.
"""

import functools
import json
import logging
import time
from pathlib import Path

from openrouter import OpenRouter

log = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent
CACHE_PATH = PROJECT_ROOT / "data" / ".openrouter_cache.json"
LOCAL_PRICES_PATH = PROJECT_ROOT / "prices.json"

# SDK требует непустой api_key для хедера Authorization; для публичного
# /models подойдёт фиктивный — сервер его не валидирует.
_DUMMY_KEY = "sk-or-price-lookup"

# Максимальный возраст кэша в секундах (24 часа).
_CACHE_TTL = 24 * 3600


def _str_to_per_1m(s: str | None) -> float | None:
    """Конвертация строки USD/токен → USD за 1M токенов."""
    if s is None:
        return None
    try:
        return float(s) * 1_000_000
    except (ValueError, TypeError):
        return None


def _read_cached_models() -> dict[str, dict]:
    """Читает models из файла кэша; пустой dict при отсутствии/порче."""
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8")).get("models", {})
    except (json.JSONDecodeError, OSError):
        return {}


@functools.lru_cache(maxsize=1)
def refresh_cache() -> dict[str, dict]:
    """Запрашивает каталог моделей у OpenRouter и кэширует его локально.

    Возвращает `{model_id: {"prompt": <str>, "completion": <str>}}`.
    Результат мемоизируется на время жизни процесса (каталог read-only).
    При ошибке сети или невалидном ответе — возвращает предыдущий кэш
    (или пустой dict), бенчмарк не падает.
    """
    # Свежий дисковый кэш — используем его, без сетевого запроса.
    if CACHE_PATH.exists():
        try:
            cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            fetched_at = cached.get("fetched_at", 0)
            if isinstance(fetched_at, (int, float)) and time.time() - fetched_at < _CACHE_TTL:
                return cached.get("models", {})
        except (json.JSONDecodeError, OSError):
            pass

    try:
        with OpenRouter(api_key=_DUMMY_KEY) as client:
            res = client.models.list()
        models = {m.id: {"prompt": m.pricing.prompt, "completion": m.pricing.completion}
                  for m in res.data}
    except Exception as exc:
        log.warning("Не удалось обновить кэш OpenRouter: %s", exc)
        return _read_cached_models()  # старый кэш как фолбэк

    try:
        CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        CACHE_PATH.write_text(
            json.dumps({"fetched_at": time.time(), "models": models}, ensure_ascii=False),
            encoding="utf-8",
        )
    except OSError as exc:
        log.warning("Не удалось записать кэш: %s", exc)

    return models


@functools.lru_cache(maxsize=1)
def _load_local_prices() -> dict:
    """Загружает prices.json (мемоизировано на процесс).

    Возвращает сам объект с ключами `overrides` (цена по модели) и
    `provider_notes` (причина отсутствия цены по провайдеру).
    """
    try:
        return json.loads(LOCAL_PRICES_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _resolve_catalog_id(cache: dict, key: str, model: str, aliases: dict) -> str | None:
    """Подбирает id модели в каталоге OpenRouter по приоритету:
    1. явный alias; 2. точный ключ `provider/model`; 3. сама `model` как id
    (когда она уже в формате `vendor/model[:free]`); 4. суффикс-поиск по
    последнему сегменту имени. Среди равных платный вариант важнее `:free`.
    """
    if key in aliases:
        return aliases[key]
    # Имя без вендора и без суффикса `:free` — по нему сводим free/платный варианты.
    leaf = model.rsplit("/", 1)[-1].removesuffix(":free")
    candidates = [c for c in cache
                  if c in (key, model) or c.rsplit("/", 1)[-1].removesuffix(":free") == leaf]
    if not candidates:
        return None
    # Ничего бесплатного не бывает: платный аналог приоритетнее `:free`.
    return min(candidates, key=lambda c: c.endswith(":free"))


def get_pricing(provider: str, model: str) -> dict:
    """Возвращает `{prompt_per_1m, completion_per_1m, note?}` для модели.

    В отчёт пишем рыночную цену модели по каталогу OpenRouter независимо от
    того, через какой провайдер она тестировалась (подписка/self-hosted/free —
    лишь способ гонять тесты дешевле). Порядок поиска:
    1. prices.json → overrides (ручная цена для моделей, которых нет в каталоге).
    2. Каталог OpenRouter (см. `_resolve_catalog_id`): alias → точный ключ →
       `model` как id → суффикс-поиск; платный аналог важнее `:free`.
    3. provider_notes — фолбэк для моделей, которых в каталоге нет вообще.
    4. Иначе → цены `None`.
    """
    key = f"{provider}/{model}"
    local = _load_local_prices()

    entry = local.get("overrides", {}).get(key)
    if entry is not None:
        return {"prompt_per_1m": entry.get("prompt_per_1m"),
                "completion_per_1m": entry.get("completion_per_1m")}

    cache = refresh_cache()
    catalog_id = _resolve_catalog_id(cache, key, model, local.get("catalog_aliases", {}))
    entry = cache.get(catalog_id) if catalog_id else None
    if entry is not None:
        return {"prompt_per_1m": _str_to_per_1m(entry.get("prompt")),
                "completion_per_1m": _str_to_per_1m(entry.get("completion"))}

    note = local.get("provider_notes", {}).get(provider)
    return {"prompt_per_1m": None, "completion_per_1m": None, "note": note}


def _fmt_usd(value: float) -> str:
    """Цена за 1M токенов: 2 знака, но 4 для суб-десятицентовых, чтобы
    дешёвые модели не округлялись в «$0.00». Та же логика в docs/index.html."""
    return f"${value:.4f}" if value < 0.1 else f"${value:.2f}"


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

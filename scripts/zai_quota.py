#!/usr/bin/env python3
"""Мониторинг квоты GLM Coding Plan (Z.AI) — standalone, только stdlib.

Дёргает недокументированные, но рабочие endpoint'ы мониторинга Z.AI (найдены
реверс-инжинирингом из community-плагина opencode-glm-quota, см. issue #163).
Ключ API берёт из opencode auth.json либо из --api-key / env ZAI_API_KEY.

Зависимости: только стандартная библиотека. Никаких импортов из этого репо —
скрипт самодостаточен, чтобы его можно было вынести в отдельный проект.

Запуск:
    python scripts/zai_quota.py                 # квота (лимиты + сброс)
    python scripts/zai_quota.py --json          # сырой JSON ответа
    python scripts/zai_quota.py --models        # + расход по моделям
    python scripts/zai_quota.py --key sk-...    # свой ключ вместо auth.json
    ZAI_API_KEY=sk-... python scripts/zai_quota.py
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from urllib.request import HTTPRedirectHandler, Request

# Платформа: ZAI (api.z.ai, глобальная) или ZHIPU (open.bigmodel.cn, CN).
PLATFORMS = {
    "zai": {
        "name": "Z.AI (api.z.ai)",
        "quota": "https://api.z.ai/api/monitor/usage/quota/limit",
        "models": "https://api.z.ai/api/monitor/usage/model-usage",
        "tools": "https://api.z.ai/api/monitor/usage/tool-usage",
    },
    "zhipu": {
        "name": "Zhipu (open.bigmodel.cn)",
        "quota": "https://open.bigmodel.cn/api/monitor/usage/quota/limit",
        "models": "https://open.bigmodel.cn/api/monitor/usage/model-usage",
        "tools": "https://open.bigmodel.cn/api/monitor/usage/tool-usage",
    },
}

DEFAULT_PLATFORM = "zai"
DEFAULT_AUTH_PATH = Path.home() / ".local" / "share" / "opencode" / "auth.json"
# Пиковое окно Z.AI: 14:00-18:00 по времени штаб-квартиры (UTC+8).
# В МСК (UTC+3) это 09:00-13:00.
PEAK_UTC_OFFSET = 8
PEAK_HOURS_UTC = (14, 18)  # 14:00-18:00 UTC+8

# Расшифровка полей limit.unit/number — Z.AI публично их НЕ документирует.
# Значения ниже — фактические константы из community-плагина opencode-glm-quota
# (src/utils/token-limits.ts), который парсит эти поля для своих подписей.
# Для TOKENS_LIMIT: (unit=3, number=5) → 5-часовое окно, (unit=6, number=1) →
# недельное. Для TIME_LIMIT явной подписи у автора нет; по nextResetTime это
# недельное окно MCP-инструментов. Любая другая комбинация — выводится как
# сырые коды (см. issue #163, «уточнить семантику unit/number»).
TOKENS_LIMIT_5H = (3, 5)     # 5-часовое окно модельных токенов
TOKENS_LIMIT_WEEKLY = (6, 1)  # недельный потолок модельных токенов
# TIME_LIMIT у автора не размечен; по наблюдаемому nextResetTime (~7 дней) это
# недельное окно. unit/number оставляем сырыми — нет подтверждённой расшифровки.

# Платформа → provider-записи в opencode auth.json и env-переменная резерва.
# cycle-2/cycle-3 codex: ключ выбирается СТРОГО по platform, чтобы credential
# одной платформы никогда не уходил на origin другой. Кандидаты — только явные
# Coding-Plan провайдеры; generic OAuth-провайдеры (openai/glm, поле access) НЕ
# кандидаты — это отдельная credential, её отправлять в Authorization нельзя.
# Env-резерв тоже platform-aware: ZAI_API_KEY для zai, ZHIPU_API_KEY для zhipu.
PLATFORM_PROVIDERS = {
    "zai": {"providers": ("zai-coding-plan", "zai"),
            "env": "ZAI_API_KEY"},                     # api.z.ai
    "zhipu": {"providers": ("zhipu-coding-plan", "zhipu"),
              "env": "ZHIPU_API_KEY"},                 # open.bigmodel.cn
}


class _NoAuthRedirectHandler(HTTPRedirectHandler):
    """Редирект-обработчик, НЕ форвардящий Authorization при смене origin.

    cycle-1 codex (critical): дефолтный urllib копирует Authorization на
    редирект даже при смене host/понижении HTTPS→HTTP. Для reverse-engineered
    endpoint'ов без стабильного контракта это утечка ключа Coding Plan на чужой
    хост. Здесь — снимаем Authorization (и Cookie/Proxy-Authorization) перед
    любым редиректом: fail-closed (лучше потерять auth на легитимном same-origin
    редиректе, чем утекать ключом; эти endpoint'ы на редиректах не рассчитаны).
    """

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        new = HTTPRedirectHandler.redirect_request(
            self, req, fp, code, msg, headers, newurl)
        if isinstance(new, Request):
            new.add_unredirected_header("Authorization", "")
            new.add_unredirected_header("Cookie", "")
        return new


# Один opener на процесс — потоков нет, переиспользуем безопасно.
_SAFE_OPENER = urllib.request.build_opener(_NoAuthRedirectHandler)


def resolve_api_key(*, auth_path: Path, api_key: str | None,
                    platform: str = DEFAULT_PLATFORM) -> str:
    """Ключ API: --api-key → platform-specific env → opencode auth.json.

    auth.json устроен как {<provider>: {type, key/access}}. Provider и env-резерв
    выбираются СТРОГО по platform (cycle-2/3 codex): иначе credential одной
    платформы уходит на origin другой. Generic OAuth-провайдеры (openai/glm,
    поле access) НЕ кандидаты — их token нельзя отправлять в Authorization.
    """
    if api_key:
        return api_key
    spec = PLATFORM_PROVIDERS.get(platform)
    if spec is None:
        raise SystemExit(f"Неизвестная платформа '{platform}'.")
    env_var = spec["env"]
    env_key = os.environ.get(env_var)
    if env_key:
        return env_key
    if not auth_path.exists():
        raise SystemExit(
            f"Не найден ключ API. Положите его в {auth_path} (opencode auth),\n"
            f"передайте через --api-key или задайте env {env_var}."
        )
    try:
        auth = json.loads(auth_path.read_text())
    except json.JSONDecodeError as exc:
        raise SystemExit(
            f"{auth_path} повреждён (не JSON): {exc.msg}. "
            f"Используйте --api-key или {env_var}.") from exc
    providers = spec["providers"]
    for provider in providers:
        entry = auth.get(provider)
        # Только поле 'key' (API-credential Coding Plan). Поле 'access' — это
        # OAuth-токен generic-провайдера, его нельзя отправлять как Coding Plan
        # ключ (cycle-3 codex C3).
        if isinstance(entry, dict):
            value = entry.get("key")
            if isinstance(value, str) and value:
                return value
    raise SystemExit(
        f"В {auth_path} нет ключа Coding Plan для платформы '{platform}' "
        f"(искали: {', '.join(providers)}). "
        f"Используйте --api-key или {env_var}."
    )


def fetch_json(url: str, api_key: str, *, timeout: float = 15.0) -> dict | None:
    """GET с auth. Z.AI принимает голый ключ в Authorization (без Bearer).

    Возвращает None, если ответ пустой (некоторые endpoint'ы — model-usage /
    tool-usage — отдают HTTP 200 с пустым телом, когда нет данных в окне или
    требуются query-параметры; см. issue #163). None обрабатывается вызывающим.
    """
    req = urllib.request.Request(
        url,
        headers={
            "Authorization": api_key,
            "Content-Type": "application/json",
        },
    )
    try:
        with _SAFE_OPENER.open(req, timeout=timeout) as resp:
            body = resp.read().decode()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode()[:300]
        raise SystemExit(f"HTTP {exc.code} от {url}:\n{body}") from exc
    except urllib.error.URLError as exc:
        raise SystemExit(f"сетевая ошибка: {exc.reason}") from exc
    if not body.strip():
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ответ {url} — не JSON: {exc.msg}") from exc


def fmt_ts(ms: int | None, tz_offset: int) -> str | None:
    """Unix-миллисекунды → 'YYYY-MM-DD HH:MM (+TZ)'. tz_offset в часах."""
    if not ms:
        return None
    tz = dt.timezone(dt.timedelta(hours=tz_offset))
    moment = dt.datetime.fromtimestamp(ms / 1000, tz=tz)
    return moment.strftime("%Y-%m-%d %H:%M")


def fmt_countdown(ms: int | None) -> str | None:
    """Unix-миллисекунды сброса → 'через Xч Yм' (относительно сейчас)."""
    if not ms:
        return None
    now = dt.datetime.now(dt.timezone.utc).timestamp()
    remaining_s = max(0, ms / 1000 - now)
    hours = int(remaining_s // 3600)
    minutes = int((remaining_s % 3600) // 60)
    if hours >= 24:
        return f"через {hours // 24}д {hours % 24}ч"
    return f"через {hours}ч {minutes}м"


def limit_window_label(limit: dict) -> str:
    """Человекочитаемое окно лимита по полям type/unit/number.

    TOKENS_LIMIT размечено по константам из opencode-glm-quota. Остальное —
    по факту nextResetTime (если он есть), иначе сырые коды (расшифровки нет).
    """
    ltype = limit.get("type")
    unit = limit.get("unit")
    number = limit.get("number")
    raw = f"unit={unit}/number={number}" if unit is not None else ""
    if ltype == "TOKENS_LIMIT":
        if (unit, number) == TOKENS_LIMIT_5H:
            return "5-часовое окно"
        if (unit, number) == TOKENS_LIMIT_WEEKLY:
            return "недельное окно"
    # Для TIME_LIMIT и неизвестных комбинаций — пробуем вывести из nextResetTime.
    reset_ms = limit.get("nextResetTime")
    if reset_ms:
        now = dt.datetime.now(dt.timezone.utc).timestamp()
        span_h = (reset_ms / 1000 - now) / 3600
        if span_h <= 6:
            return f"5-часовое окно ({raw})" if raw else "5-часовое окно"
        if span_h >= 24 * 5:
            return f"недельное окно ({raw})" if raw else "недельное окно"
    return raw or "?"


def is_peak() -> bool:
    """Сейчас пиковое окно Z.AI (14:00-18:00 по времени штаба, UTC+8)?"""
    hq_offset = dt.timezone(dt.timedelta(hours=PEAK_UTC_OFFSET))
    now_at_hq = dt.datetime.now(hq_offset)
    start, end = PEAK_HOURS_UTC
    return start <= now_at_hq.hour < end


def progress_bar(pct: int, width: int = 20) -> str:
    """ASCII-прогрессбар: [████████░░░░░░░░░░░░] 40%."""
    filled = round(width * pct / 100)
    bar = "█" * filled + "░" * (width - filled)
    return f"[{bar}] {pct}%"


def print_quota(data: dict | None, *, tz_offset: int) -> None:
    """Человекочитаемый вывод лимитов."""
    if data is None:
        print("(endpoint квоты вернул пустой ответ — формат изменился? см. --json)")
        return
    payload = data.get("data", data)
    level = payload.get("level")
    if level:
        print(f"Тариф: {level.upper()}")
    if is_peak():
        print("⚠  Сейчас ПИКОВОЕ окно Z.AI (14:00-18:00 UTC+8 / 09:00-13:00 МСК) "
              "— лимиты жёстче.")
    print()

    limits = payload.get("limits") or []
    if not limits:
        print("(лимиты не вернулись)")
        return

    for lim in limits:
        ltype = lim.get("type", "?")
        pct = int(lim.get("percentage", 0))
        reset_ms = lim.get("nextResetTime")

        if ltype == "TOKENS_LIMIT":
            title = "Модельные токены (главный пул — режет прогоны)"
        elif ltype == "TIME_LIMIT":
            title = "MCP-инструменты (web-search / web-reader / zread)"
        else:
            title = ltype

        print(f"▍ {title}")
        print(f"  {progress_bar(pct)}")
        extra = []
        if "remaining" in lim and "currentValue" in lim:
            extra.append(f"{lim['remaining']}/{lim['remaining'] + lim['currentValue']}")
        if "usage" in lim:
            extra.append(f"usage={lim['usage']}")
        if extra:
            print(f"  ({', '.join(extra)})")
        print(f"  окно: {limit_window_label(lim)}")
        reset_at = fmt_ts(reset_ms, tz_offset)
        countdown = fmt_countdown(reset_ms)
        if reset_at:
            print(f"  сброс: {reset_at} ({countdown})")

        details = lim.get("usageDetails") or []
        for d in details:
            if d.get("usage"):
                print(f"    · {d.get('modelCode', '?')}: {d['usage']}")
        print()


def print_models(data: dict | None) -> None:
    """Расход токенов по моделям (опционально, --models)."""
    if data is None:
        print("(endpoint model-usage вернул пустой ответ — возможно нужны "
              "query-параметры или данных в окне нет; см. issue #163)")
        return
    payload = data.get("data", data)
    items = payload.get("list") or payload.get("models") or payload.get("items") or []
    if not items:
        print("(расход по моделям пуст или формат ответа изменился)")
        print("сырой ответ — с --json")
        return
    print("Расход по моделям:")
    for item in items:
        name = item.get("modelCode") or item.get("model") or item.get("name", "?")
        usage = item.get("usage") or item.get("tokens") or item.get("totalTokens", 0)
        print(f"  · {name}: {usage}")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Мониторинг квоты GLM Coding Plan (Z.AI). Standalone, только stdlib.")
    ap.add_argument("--key", help="API-ключ Coding Plan (иначе env ZAI_API_KEY / auth.json)")
    ap.add_argument("--auth-path", type=Path, default=DEFAULT_AUTH_PATH,
                    help=f"путь к opencode auth.json (default: {DEFAULT_AUTH_PATH})")
    ap.add_argument("--platform", choices=PLATFORMS, default=DEFAULT_PLATFORM,
                    help="платформа: zai (api.z.ai) или zhipu (open.bigmodel.cn)")
    ap.add_argument("--tz", type=int, default=3,
                    help="часовой пояс для отображения времени сброса (default: 3 = МСК)")
    ap.add_argument("--models", action="store_true",
                    help="показать также расход токенов по моделям")
    ap.add_argument("--json", action="store_true",
                    help="вывести сырой JSON ответа (без форматирования)")
    args = ap.parse_args()

    api_key = resolve_api_key(auth_path=args.auth_path, api_key=args.key,
                              platform=args.platform)
    endpoints = PLATFORMS[args.platform]

    quota = fetch_json(endpoints["quota"], api_key)
    models = fetch_json(endpoints["models"], api_key) if args.models else None

    if args.json:
        # Единый объект: квота всегда, модели опционально. Раньше ранний
        # return в --json-ветке терял модели при --models --json (R3).
        payload = {"quota": quota if quota is not None else {"error": "пустой ответ"}}
        if args.models:
            payload["models"] = (models if models is not None
                                 else {"error": "пустой ответ"})
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"=== {endpoints['name']} — квота Coding Plan ===\n")
    print_quota(quota, tz_offset=args.tz)

    if args.models:
        print("=== Расход по моделям ===\n")
        print_models(models)

    return 0


if __name__ == "__main__":
    sys.exit(main())

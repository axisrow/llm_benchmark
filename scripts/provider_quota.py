#!/usr/bin/env python3
"""MVP: квоты всех подключённых провайдеров opencode.

Читает ~/.local/share/opencode/auth.json, для каждого провайдера пробует
дёрнуть quota-endpoint. MVP-набросок: zai + openrouter реализованы live,
остальные — заглушки «недоступно» (endpoint'ы не задокументированы, см.
репорт разведки). Только stdlib.

Запуск:
    python scripts/provider_quota.py
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path

AUTH_PATH = Path.home() / ".local" / "share" / "opencode" / "auth.json"


def _fetch_json(url: str, headers: dict, *, timeout: float = 15.0) -> dict | None:
    """GET → JSON; None при пустом ответе, SystemExit при ошибке."""
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode()
    except urllib.error.HTTPError as exc:
        return {"_error": f"HTTP {exc.code}: {exc.read().decode()[:120]}"}
    except (urllib.error.URLError, OSError) as exc:
        return {"_error": f"сеть: {exc}"}
    if not body.strip():
        return None
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return {"_error": "не JSON"}


def _mask(key: str | None) -> str:
    if not key:
        return "(нет)"
    return f"{key[:6]}…{key[-3:]}" if len(key) > 12 else "***"


def _atomic_write(path: Path, data: dict) -> None:
    """Атомарная запись cred-файла (чтобы не потерять при крахе)."""
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def _oauth_refresh(*, token_url: str, client_id: str,
                   refresh_token: str, scope: str | None = None,
                   extra_headers: dict | None = None
                   ) -> tuple[dict, str | None] | tuple[None, str]:
    """OAuth refresh: POST → {access_token, refresh_token?, expires_in}.

    Возвращает (data, None) при успехе или (None, error_kind) при провале.
    error_kind: 'rate_limited' (429 — временно, попробовать позже),
    'invalid_grant' (refresh_token протух/отозван — нужен повторный логин),
    'network' (сбой сети/таймаут), 'http:<code>' (иная HTTP-ошибка).
    refresh_token у OpenAI/Anthropic ОДНОРАЗОВЫЙ (ротируется в ответе) —
    вызывающий обязан записать новый refresh_token обратно в cred-файл.
    """
    body = {"grant_type": "refresh_token",
            "client_id": client_id,
            "refresh_token": refresh_token}
    if scope:
        body["scope"] = scope
    headers = {"Content-Type": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(
        token_url,
        data=json.dumps(body).encode(),
        headers=headers,
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode()), None
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            return None, "rate_limited"
        # invalid_grant / bad refresh token → повторный логин
        try:
            err_body = json.loads(exc.read().decode())
        except (ValueError, OSError):
            err_body = {}
        err_type = (err_body.get("error") or "")
        if isinstance(err_type, dict):
            err_type = err_type.get("type", "")
        if exc.code == 400 or "invalid" in str(err_type).lower():
            return None, "invalid_grant"
        return None, f"http:{exc.code}"
    except (urllib.error.URLError, OSError):
        return None, "network"


def quota_zai(key: str) -> dict:
    """zai-coding-plan: % пулов + сброс (endpoint из zai-quota)."""
    data = _fetch_json(
        "https://api.z.ai/api/monitor/usage/quota/limit",
        {"Authorization": key, "Content-Type": "application/json"})
    if not data or data.get("_error"):
        return data or {"_error": "пустой ответ"}
    payload = data.get("data", {})
    items = []
    for lim in payload.get("limits", []):
        ltype = lim.get("type", "?")
        pct = lim.get("percentage", 0)
        items.append({"label": ltype, "value": f"{pct}%"})
    return {"tariff": payload.get("level", "?"), "items": items}


def quota_openrouter(key: str) -> dict:
    """openrouter: usage $ (всего/день/неделя/месяц), limit, free_tier."""
    data = _fetch_json("https://openrouter.ai/api/v1/key",
                       {"Authorization": f"Bearer {key}"})
    if not data or data.get("_error"):
        return data or {"_error": "пустой ответ"}
    d = data.get("data", {})
    items = []
    if d.get("limit_remaining") is not None:
        items.append({"label": "limit_remaining", "value": f"${d['limit_remaining']}"})
    items.append({"label": "usage (всего)", "value": f"${d.get('usage', 0):.2f}"})
    items.append({"label": "usage день/нед/мес",
                  "value": f"${d.get('usage_daily', 0):.2f} / "
                           f"${d.get('usage_weekly', 0):.2f} / "
                           f"${d.get('usage_monthly', 0):.2f}"})
    return {"tariff": "free_tier" if d.get("is_free_tier") else "paid",
            "items": items}


def quota_openai_chatgpt(_key: str) -> dict:
    """OpenAI ChatGPT (Codex OAuth): /wham/usage — used_percent окон.

    Контракт из akitaonrails/ai-usagebar (src/openai/fetch.rs) и Codex CLI.
    Credentials из ~/.codex/auth.json. При 401 (access истёк) — OAuth refresh
    через auth.openai.com, ротированный refresh_token пишем обратно в файл.
    """
    codex_path = Path.home() / ".codex" / "auth.json"
    if not codex_path.exists():
        return {"_error": "~/.codex/auth.json не найден (codex не залогинен)"}
    codex = json.loads(codex_path.read_text())
    tokens = codex.get("tokens", {})
    refresh_token = tokens.get("refresh_token")
    account_id = tokens.get("account_id")

    def fetch_usage(access: str) -> dict | None:
        headers = {"Authorization": f"Bearer {access}", "User-Agent": "codex-cli"}
        if account_id:
            headers["ChatGPT-Account-Id"] = account_id
        return _fetch_json("https://chatgpt.com/backend-api/wham/usage", headers)

    access = tokens.get("access_token")
    data = fetch_usage(access) if access else None
    # 401/403 → refresh + retry + write-back ротированного refresh_token.
    if (not data or _is_auth_error(data)) and refresh_token:
        refreshed, err = _refresh_and_retry(refresh_kwargs={
            "token_url": "https://auth.openai.com/oauth/token",
            "client_id": "app_EMoamEEZ73f0CkXaXp7hrann",
            "refresh_token": refresh_token, "scope": "openid profile email"})
        if refreshed:
            tokens["access_token"] = refreshed["access_token"]
            if refreshed.get("refresh_token"):
                tokens["refresh_token"] = refreshed["refresh_token"]
            codex["tokens"] = tokens
            _atomic_write(codex_path, codex)
            data = fetch_usage(refreshed["access_token"])
        elif err:
            return {"_error": err}
    if not data:
        return {"_error": "пустой ответ"}
    if data.get("_error"):
        return data
    return _parse_openai_usage(data)


def _is_auth_error(data: dict | None) -> bool:
    """HTTP 401/403 в нашем _error-формате ( '_error': 'HTTP 401: ...' )."""
    if not data:
        return False
    err = str(data.get("_error", ""))
    return "HTTP 401" in err or "HTTP 403" in err


_REFRESH_ERROR_MESSAGES = {
    "rate_limited": "refresh rate-limited провайдером — попробуй позже",
    "invalid_grant": "refresh_token протух/отозван — повторно залогинься в CLI",
    "network": "сбой сети при refresh — попробуй позже",
}


def _refresh_and_retry(*, refresh_kwargs: dict) -> tuple[dict | None, str | None]:
    """OAuth refresh. Возвращает (refreshed_data, error_msg|None).

    refreshed_data = {access_token, refresh_token?, expires_in, ...} или None.
    Caller сам пишет ротированный refresh_token обратно в cred-файл (структура
    у openai/anthropic разная) и дёргает fetch_usage(new_access).
    error_msg — человекочитаемая причина при провале refresh.
    """
    refreshed, err = _oauth_refresh(**refresh_kwargs)
    if not refreshed:
        # err всегда задан, когда refreshed=None; assert успокаивает типизатор.
        assert err is not None
        return None, _REFRESH_ERROR_MESSAGES.get(err, f"refresh не удался ({err})")
    if not refreshed.get("access_token"):
        return None, "refresh вернул ответ без access_token"
    return refreshed, None


def _parse_openai_usage(data: dict) -> dict:
    items = []
    plan = data.get("plan_type")
    rl = data.get("rate_limit") or {}
    for name in ("primary_window", "secondary_window"):
        win = rl.get(name)
        if not win:
            continue
        seconds = win.get("limit_window_seconds", 0)
        # 18000с≈5ч, 604800с=7д — подпись окна
        label = "5ч" if seconds <= 86400 else ("7д" if seconds >= 600000 else f"{seconds//3600}ч")
        used = win.get("used_percent", 0)
        items.append({"label": f"used {label}", "value": f"{used}%"})
    # additional_rate_limits (per-model, напр. GPT-5.3-Codex-Spark)
    for extra in (data.get("additional_rate_limits") or [])[:3]:
        rl2 = extra.get("rate_limit") or {}
        name = extra.get("limit_name", "?")
        items.append({"label": f"{name}", "value": f"{rl2.get('used_percent', 0)}%"})
    return {"tariff": plan or "?", "items": items}


def quota_anthropic_claude(_key: str) -> dict:
    """Anthropic Claude (Claude Code OAuth): /api/oauth/usage.

    Контракт из akitaonrails/ai-usagebar (src/anthropic/fetch.rs). Credentials
    из ~/.claude/.credentials.json → claudeAiOauth.accessToken (на macOS может
    быть в Keychain). При 401 — OAuth refresh через platform.claude.com,
    ротированный refreshToken пишем обратно в файл.
    """
    cred_path = Path.home() / ".claude" / ".credentials.json"
    if not cred_path.exists():
        return {"_error": "~/.claude/.credentials.json не найден "
                          "(возможно в Keychain; claude не залогинен?)"}
    cred = json.loads(cred_path.read_text())
    oauth = cred.get("claudeAiOauth", {})
    refresh_token = oauth.get("refreshToken")

    def fetch_usage(access: str) -> dict | None:
        return _fetch_json("https://api.anthropic.com/api/oauth/usage", {
            "Authorization": f"Bearer {access}",
            "anthropic-beta": "oauth-2025-04-20",
            "User-Agent": "claude-code/2.1.183",
            "Content-Type": "application/json",
        })

    access = oauth.get("accessToken")
    data = fetch_usage(access) if access else None
    if (not data or _is_auth_error(data)) and refresh_token:
        refreshed, err = _refresh_and_retry(refresh_kwargs={
            "token_url": "https://platform.claude.com/v1/oauth/token",
            "client_id": "9d1c250a-e61b-44d9-88ed-5944d1962f5e",
            "refresh_token": refresh_token,
            "extra_headers": {"anthropic-beta": "oauth-2025-04-20",
                              "User-Agent": "claude-code/2.1.183"}})
        if refreshed:
            oauth["accessToken"] = refreshed["access_token"]
            if refreshed.get("refresh_token"):
                oauth["refreshToken"] = refreshed["refresh_token"]
            cred["claudeAiOauth"] = oauth
            _atomic_write(cred_path, cred)
            data = fetch_usage(refreshed["access_token"])
        elif err:
            return {"_error": err}
    if not data:
        return {"_error": "пустой ответ"}
    if data.get("_error"):
        return data
    items = []
    for name in ("five_hour", "seven_day"):
        win = data.get(name)
        if not win:
            continue
        label = "5ч" if name == "five_hour" else "7д"
        util = win.get("utilization")
        items.append({"label": f"used {label}",
                      "value": f"{util}%" if util is not None else "?"})
    return {"tariff": "claude", "items": items}


# Реестр: провайдер → (поле ключа в opencode auth.json, обработчик или None).
# None → «недоступно» с причиной (из разведки). openai/anthropic читают свои
# cred-файлы сами (~/.codex, ~/.claude), поэтому handler игнорирует opencode-ключ.
PROVIDERS = {
    "zai-coding-plan": ("key",    quota_zai,               "Z.AI Coding Plan"),
    "openrouter":      ("key",    quota_openrouter,        "OpenRouter"),
    "openai":          ("access", quota_openai_chatgpt,    "OpenAI ChatGPT (Codex OAuth)"),
    "anthropic":       ("access", quota_anthropic_claude,  "Anthropic Claude (Claude Code OAuth)"),
    "github-copilot":  ("access", None,
                        "только enterprise/org-admin metrics API; individual — web-scrape (не MVP)"),
    "gitlab":          ("access", None,
                        "GraphQL currentQuotaUsage не существует (feature-request)"),
    "opencode":        ("key", None,
                        "Zen balance endpoint — issue opencode#10448 открыт"),
    "google":          ("key", None,
                        "Gemini: no quota API (staff-confirmed); Cloud Monitoring требует OAuth"),
    "ollama-cloud":    ("key", None,
                        "issue ollama#15663 открыт; только cookie-scrape (не MVP)"),
    "nvidia":          ("key", None,
                        "40 RPM rate-limit без $-баланса, нет endpoint'а"),
}


def main() -> int:
    if not AUTH_PATH.exists():
        raise SystemExit(f"auth.json не найден: {AUTH_PATH}")
    auth = json.loads(AUTH_PATH.read_text())

    print(f"=== Квоты подключённых провайдеров ({AUTH_PATH}) ===\n")
    for provider, (field, handler, note) in PROVIDERS.items():
        title = f"{PROVIDERS[provider][2]} ({provider})"
        # openai/anthropic читают свои cred-файлы (~/.codex, ~/.claude), не
        # opencode auth.json — для них opencode-наличие не обязательно.
        reads_own_creds = provider in ("openai", "anthropic")
        entry = auth.get(provider)
        if not reads_own_creds and not isinstance(entry, dict):
            print(f"▍ {provider} — НЕ ПОДКЛЮЧЁН\n")
            continue
        key = entry.get(field) if isinstance(entry, dict) else None
        if not handler:
            print(f"▍ {title}")
            print(f"  ключ: {_mask(key) if key else '(oauth/отдельный файл)'}")
            print(f"  ⚠ квота недоступна — {note}\n")
            continue
        print(f"▍ {title}")
        if not reads_own_creds:
            print(f"  ключ: {_mask(key)}")
        try:
            result = handler(key or "")
        except Exception as exc:
            result = {"_error": f"{type(exc).__name__}: {exc}"}
        if result.get("_error"):
            print(f"  ✗ ошибка: {result['_error']}\n")
            continue
        if result.get("tariff"):
            print(f"  тариф: {result['tariff']}")
        for item in result.get("items", []):
            if isinstance(item, dict):
                print(f"  · {item.get('label', '?')}: {item.get('value', '?')}")
        print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

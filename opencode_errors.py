"""Классификация ошибок провайдера и санитайзинг причин (issue #53).

Выделено из opencode_runtime.py: распознавание лимитов/аккаунт-ошибок провайдера,
скрабинг секретов/PII для публичного отчёта, чтение «хвоста» ошибок из логов
opencode. Листовой модуль — зависит только от stdlib (importирует ничего из
runtime/session/process), поэтому его свободно тянут остальные модули.
"""

import json
import re
import sys
from pathlib import Path

OPENCODE_LOG_DIR = Path.home() / ".local" / "share" / "opencode" / "log"

_PROVIDER_RETRYABLE_LIMIT_ERROR_MARKERS = (
    "http 429",
    "too many requests",
    "rate limit",
    "rate_limit",
    "usage limit",
    "quota",
)

_PROVIDER_PERMANENT_ACCOUNT_ERROR_MARKERS = (
    "requires a subscription",
    "upgrade for access",
    "upgrade for higher limits",
    "insufficient credit",
    "insufficient credits",
    "billing",
    "payment method",
)

_PROVIDER_LIMIT_ERROR_MARKERS = (
    _PROVIDER_RETRYABLE_LIMIT_ERROR_MARKERS
    + _PROVIDER_PERMANENT_ACCOUNT_ERROR_MARKERS
)

# issue #124: POST /message не ответил (ReadTimeout), сессия закрылась по idle, и
# копия НЕ оставила файла модели — ответа провайдера не было вовсе. Единственный
# источник правды для текста причины: ставит его benchmark_report.run_copy (только
# там известен work_dir, т.е. факт наличия результата), а public_reason ниже
# распознаёт как отдельную категорию — иначе причина свелась бы к общему «ошибка
# провайдера» и диагностика первопричины пустого успеха не дошла бы до дашборда.
HUNG_POST_REASON = ("зависший POST /message: ответа провайдера не было, "
                    "сессия закрылась без результата")

# issue #161: OpenCode закончил assistant turn по finish=length. Это не сетевой
# сбой и не «провайдер не ответил»: usage уже получен, но внутренний per-step
# budget OpenCode исчерпан до tool call/финального результата.
OUTPUT_LENGTH_REASON = "лимит ответа OpenCode исчерпан"

# Опциональный watchdog для unattended-прогонов. Категория локальная и безопасна
# для публичного отчёта; числовой порог добавляется к ней в opencode_session.
FIRST_ACTION_TIMEOUT_REASON = "агент не начал действие до first-action timeout"

# issue #158: Python-клиент бенчмарка обращается к локальному opencode serve,
# поэтому транспортная ошибка POST/SSE достоверно означает потерю локального
# канала к serve. Причину внешнего обрыва (интернет, crash serve, firewall) сам
# httpx определить не может; подробность остаётся в приватном run.log.
NETWORK_ERROR_REASON = "локальный обрыв сети: потеряна связь с opencode serve"

# Шаблоны секрето-/PII-подобных фрагментов, которые нельзя выпускать в публичный
# отчёт. Полный текст причины при этом остаётся в приватном run.log.
_SECRET_PATTERNS = (
    re.compile(r"[Bb]earer\s+\S+"),                 # Bearer <token>
    re.compile(r"\b(?:sk|key|pk|tok|ghp|xoxb)[-_][A-Za-z0-9\-_]{6,}"),  # api keys
    re.compile(r"\b[\w.%+-]+@[\w.-]+\.[A-Za-z]{2,}\b"),  # email
    re.compile(r"https?://\S+"),                    # URL (могут нести query/токены)
    re.compile(r"\b[A-Za-z0-9_\-]{20,}\b"),         # длинные токено-подобные строки
)
_LOCAL_REASON_PREFIXES = (
    "сбой ",
    "opencode serve не поднялся",
)


def _is_provider_limit_error(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _PROVIDER_LIMIT_ERROR_MARKERS)


def _is_retryable_limit_error(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in _PROVIDER_RETRYABLE_LIMIT_ERROR_MARKERS)


def _is_account_error(text: str) -> bool:
    lowered = text.lower()
    return any(m in lowered for m in _PROVIDER_PERMANENT_ACCOUNT_ERROR_MARKERS)


def _decode_json_string_field(raw: str, field: str) -> str | None:
    match = re.search(fr'"{re.escape(field)}":"((?:\\.|[^"\\])*)"', raw)
    if not match:
        return None
    encoded = match.group(1)
    try:
        return json.loads(f'"{encoded}"')
    except json.JSONDecodeError:
        return encoded


def _short_error_detail(text: str, limit: int = 180) -> str:
    cleaned = " ".join(text.split())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[:limit - 1] + "…"


def _scrub_secrets(text: str) -> str:
    """Вырезает секрето-/PII-подобные фрагменты, заменяя их на «[скрыто]»."""
    for pattern in _SECRET_PATTERNS:
        text = pattern.sub("[скрыто]", text)
    return text


def public_reason(reason: str | None) -> str | None:
    """Санирует причину исхода для ПУБЛИЧНОГО отчёта (raw_json → дашборд).

    Полная причина (с сырым телом провайдера и tail логов) остаётся в приватном
    run.log. Наружу отдаём безопасный каркас: HTTP-код + распознанная категория, а
    для нераспознанного — короткий хвост со скрабингом секретов/PII. Если категория
    не ясна и текст подозрителен — отдаём только код/«ошибка провайдера», не сырьё.
    """
    if not reason:
        return None
    if reason.startswith(NETWORK_ERROR_REASON):
        # Собственный диагноз бенчмарка, а не тело провайдера. Публикуем только
        # стабильную категорию: текст транспортного исключения остаётся в run.log.
        return NETWORK_ERROR_REASON
    if reason.startswith(_LOCAL_REASON_PREFIXES):
        # Локальная инфраструктурная причина (запуск сервера, future, crash) не
        # является телом провайдера; проверяем её до keyword-классификации, чтобы
        # случайные слова вроде forbidden в пути не стали «ошибкой авторизации».
        return _short_error_detail(_scrub_secrets(reason), limit=120)

    if reason.startswith(HUNG_POST_REASON):
        # issue #124: собственный диагноз бенчмарка, а не тело провайдера —
        # публикуем как есть, чтобы «пустой успех» на дашборде имел причину.
        return HUNG_POST_REASON
    if reason.startswith(OUTPUT_LENGTH_REASON):
        return _short_error_detail(reason.split(" | ", 1)[0], limit=160)
    if reason.startswith(FIRST_ACTION_TIMEOUT_REASON):
        return _short_error_detail(reason.split(" | ", 1)[0], limit=160)

    # Таймаут-причины («нет ответа за 60с …») не содержат тела провайдера — но в
    # хвост мог попасть provider-tail, поэтому всё равно скрабим.
    code_match = re.search(r"HTTP\s+(\d+)", reason)
    code = code_match.group(1) if code_match else None
    prefix = f"HTTP {code}" if code else None

    if code in ("401", "403") or "unauthorized" in reason.lower() \
            or "forbidden" in reason.lower():
        return f"{prefix}: ошибка авторизации" if prefix else "ошибка авторизации"
    if _is_retryable_limit_error(reason):
        return f"{prefix}: превышен лимит/квота" if prefix else "превышен лимит/квота"
    if _is_account_error(reason):
        return f"{prefix}: проблема аккаунта/биллинга" if prefix \
            else "проблема аккаунта/биллинга"
    if reason.startswith("нет ответа"):
        # Чистый таймаут без provider-текста — оставляем как есть; но если к нему
        # приклеен tail (через " | "), берём только безопасную головную часть.
        return _scrub_secrets(reason.split(" | ", 1)[0])

    # Категория не распознана. Если есть HTTP-код, НЕ публикуем тело провайдера:
    # скраббер не является allowlist и не должен решать, какие поля безопасны.
    if prefix:
        return f"{prefix}: ошибка провайдера"
    return "ошибка провайдера"


def _response_body_error(raw: str) -> str | None:
    body = _decode_json_string_field(raw, "responseBody")
    if not body:
        return None
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return _short_error_detail(body)
    if isinstance(payload, dict):
        err = payload.get("error")
        if isinstance(err, str):
            return _short_error_detail(err)
        if isinstance(err, dict):
            msg = err.get("message") or err.get("error") or err.get("name")
            if isinstance(msg, str):
                return _short_error_detail(msg)
        msg = payload.get("message") or payload.get("detail")
        if isinstance(msg, str):
            return _short_error_detail(msg)
    return _short_error_detail(body)


def _log_line_has_agent(raw: str, agent: str) -> bool:
    pattern = rf"(?<!\S)agent={re.escape(agent)}(?=\s|$)"
    return re.search(pattern, raw) is not None


def _opencode_error_tail(session_id: str, lines: int = 8, *,
                         agent: str | None = None) -> str | None:
    try:
        log_files = sorted(OPENCODE_LOG_DIR.glob("*.log"),
                           key=lambda p: p.stat().st_mtime, reverse=True)
    except OSError as exc:
        # Не смогли прочитать каталог provider-логов: причина account/provider
        # ошибки деградирует до обычного timeout. Оставляем след, чтобы это было
        # видно, а не выглядело как «в логах ничего нет».
        print(f"[opencode] не удалось прочитать каталог логов "
              f"{OPENCODE_LOG_DIR}: {exc}", file=sys.stderr)
        return None

    found: list[str] = []
    unread: list[str] = []
    for log_file in log_files:
        try:
            with log_file.open(errors="replace") as fh:
                for raw in fh:
                    raw = raw.rstrip("\n")
                    if not raw.startswith("ERROR") or session_id not in raw:
                        continue
                    if agent is not None and not _log_line_has_agent(raw, agent):
                        continue
                    status = re.search(r'statusCode["\s:=]+(\d+)', raw)
                    err_name = re.search(r'"name":"([^"]+)"', raw)
                    detail = re.search(r'"message":"([^"]{0,160})"', raw)
                    response_error = _response_body_error(raw)
                    parts = []
                    if status:
                        parts.append(f"HTTP {status.group(1)}")
                    if err_name:
                        parts.append(err_name.group(1))
                    detail_text = detail.group(1) if detail else None
                    if "Too Many Requests" in raw and not (
                        detail_text and "Too Many Requests" in detail_text
                    ):
                        parts.append("Too Many Requests")
                    if detail_text:
                        parts.append(detail_text)
                    if response_error and response_error not in parts:
                        parts.append(response_error)
                    summary = " | ".join(parts) if parts else raw[:200]
                    if summary not in found:
                        found.append(summary)
        except OSError as exc:
            # Конкретный лог-файл не открылся (удалён/нет прав) — копим, чтобы не
            # спамить stderr на каждый файл в цикле; сообщим один раз ниже.
            unread.append(f"{log_file}: {exc}")
            continue
        if found:
            break
    # Логируем пропущенные файлы один раз и только если причину так и не нашли:
    # иначе провайдерская причина могла потеряться именно в нечитаемом логе.
    if unread and not found:
        print(f"[opencode] не удалось прочитать логи ({len(unread)}): "
              f"{'; '.join(unread)}", file=sys.stderr)
    if not found:
        return None
    return "\n".join(found[-lines:])

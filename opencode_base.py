"""Базовые примитивы runtime (issue #53): тип результата сессии, Writer,
соединение с opencode и константы-настройки.

ЛИСТОВОЙ модуль — импортирует только stdlib/usage/opencode_ai и НИЧЕГО из
runtime/session/process. Поэтому его свободно тянут opencode_session,
opencode_process и opencode_runtime без циклических импортов (в отличие от CORE
внутри фасада, который при прямом `import opencode_session` дал бы цикл).
"""

from collections.abc import Callable
from dataclasses import dataclass

from opencode_ai import Opencode
from usage import Usage

# POST /message - streaming request. It needs a short finite read-timeout even
# for long benchmark runs, otherwise the worker can sit inside http.post until
# the full run timeout and never notice SSE/log provider errors.
POST_MESSAGE_READ_TIMEOUT = 30.0
PROVIDER_LIMIT_LOG_POLL_INTERVAL = 2.0
# Дать SSE-reader потоку секунду на инициализацию перед отправкой сообщения.
SSE_READER_STARTUP_DELAY = 0.3

# Сервер/прокси может gracefully закрыть стрим GET /event (≈120с) задолго до конца
# бюджета прогона — БЕЗ финального session.idle/session.error. Тогда reader обязан
# переподключиться, а не молча выйти (иначе основной цикл досидит до deadline и
# выдаст ложный таймаут). Реконнект ограничен deadline прогона и счётчиком-страховкой.
SSE_RECONNECT_DELAY = 0.5      # пауза между переподключениями к /event
SSE_MAX_RECONNECTS = 1000      # страховка от busy-loop (реальный лимит — deadline)
SSE_EVENT_READ_TIMEOUT = 60.0  # read-timeout на сам GET /event (вместо None)
# Idle-check на пути реконнекта дёргает зависший сервер: длинный таймаут × до
# SSE_MAX_RECONNECTS попыток = часы простоя reader-потока. Держим коротким.
SSE_IDLE_CHECK_TIMEOUT = 3.0   # таймаут GET /session/<id>/message в idle-check

# Ретрай при лимите провайдера (HTTP 429 / rate limit). Паузы между попытками
# идут «сверх» --timeout прогона: каждая попытка получает свежий полный бюджет.
RATE_LIMIT_MAX_ATTEMPTS = 5          # всего попыток (1 исходная + 4 ретрая)
RATE_LIMIT_BACKOFF_BASE = 5.0        # первая пауза, сек
RATE_LIMIT_BACKOFF_FACTOR = 2.0      # 5 -> 10 -> 20 -> 40
RATE_LIMIT_BACKOFF_CAP = 60.0        # потолок паузы

Writer = Callable[[str], None]


@dataclass(frozen=True)
class SessionProbeResult:
    code: int
    reason: str | None = None
    usage: Usage | None = None
    # True = исход — лимит провайдера, обёртка probe_session может ретраить.
    rate_limited: bool = False
    questions: tuple[dict, ...] = ()
    plan_path: str | None = None
    plan_elapsed: float | None = None
    build_elapsed: float | None = None
    plan_usage: Usage | None = None
    build_usage: Usage | None = None
    plan_completed: bool = False


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def client_for_port(port: int) -> Opencode:
    return Opencode(base_url=base_url(port))

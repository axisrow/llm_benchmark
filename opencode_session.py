"""SSE-чтение событий сессии opencode и прогон агента (issue #53).

Выделено из opencode_runtime.py: подъём SSE-reader потока, отправка задачи,
ожидание исхода (idle/error/limit/timeout) и ретрай при лимите провайдера.
Импортирует базовые примитивы из opencode_base (ЛИСТ — без цикла с runtime) и
классификацию ошибок из opencode_errors. opencode_runtime ре-экспортирует
публичные имена (probe_session и др.) — потребители не меняются.
"""

import json
import re
import sys
import threading
import time
from collections.abc import Callable

import httpx
import httpx_sse

from opencode_base import (
    POST_MESSAGE_READ_TIMEOUT,
    PROVIDER_LIMIT_LOG_POLL_INTERVAL,
    RATE_LIMIT_BACKOFF_BASE,
    RATE_LIMIT_BACKOFF_CAP,
    RATE_LIMIT_BACKOFF_FACTOR,
    RATE_LIMIT_MAX_ATTEMPTS,
    SSE_EVENT_READ_TIMEOUT,
    SSE_IDLE_CHECK_TIMEOUT,
    SSE_MAX_RECONNECTS,
    SSE_READER_STARTUP_DELAY,
    SSE_RECONNECT_DELAY,
    SessionProbeResult,
    Writer,
    base_url,
)
from opencode_errors import (
    NETWORK_ERROR_REASON,
    _is_provider_limit_error,
    _is_retryable_limit_error,
    _opencode_error_tail,
    public_reason,
)
from planning_questions import QuestionProtocolError, capture_question_request
from usage import (
    Usage,
    extract_session_usage,
    extract_usage_from_message,
    field,
    merge_usages,
)


_PLAN_PATH_RE = re.compile(r"^Plan at (.+?) is complete\.", re.DOTALL)


def _extract_session_id(payload: dict) -> str | None:
    props = payload.get("properties", payload)
    if not isinstance(props, dict):
        return None
    info = props.get("info")
    if isinstance(info, dict):
        sid = info.get("sessionID") or info.get("id")
        if isinstance(sid, str) and sid.startswith("ses_"):
            return sid
    sid = props.get("sessionID")
    if isinstance(sid, str):
        return sid
    return None


def _format_event(payload: dict) -> str | None:
    etype = payload.get("type", "")
    props = payload.get("properties", {})

    if etype == "message.part.updated":
        part = props.get("part", {})
        ptype = part.get("type")
        if ptype == "text":
            return part.get("text", "")
        if ptype == "tool":
            tool = part.get("tool") or part.get("name") or "?"
            state = (part.get("state") or {}).get("status", "")
            return f"\n[tool: {tool} {state}]"
        return None

    if etype == "tool.execute.before":
        return f"\n[tool start: {props.get('tool', '?')}]"
    if etype == "tool.execute.after":
        return f"\n[tool done: {props.get('tool', '?')}]"
    if etype == "session.error":
        return f"\n[SESSION ERROR] {json.dumps(props, ensure_ascii=False)[:300]}"
    return None


def _rate_limit_backoff(attempt: int) -> float:
    """Пауза перед повтором: attempt 1 -> 5с, 2 -> 10, 3 -> 20, 4 -> 40 (потолок 60)."""
    delay = RATE_LIMIT_BACKOFF_BASE * (RATE_LIMIT_BACKOFF_FACTOR ** (attempt - 1))
    return min(delay, RATE_LIMIT_BACKOFF_CAP)


def _message_post_timeout(deadline: float | None, now: float) -> float:
    if deadline is None:
        return POST_MESSAGE_READ_TIMEOUT
    remaining = deadline - now
    if remaining <= 0:
        return 0
    return min(POST_MESSAGE_READ_TIMEOUT, remaining)


def _network_error_reason(operation: str, exc: BaseException) -> str:
    """Приватная transport-деталь поверх стабильной публичной категории."""
    return (f"{NETWORK_ERROR_REASON}: {operation}: "
            f"{type(exc).__name__}: {exc}")


def _error_text(props: dict) -> str:
    err = props.get("error") or {}
    if isinstance(err, str):
        return err
    if not isinstance(err, dict):
        return "?"
    data = err.get("data") or {}
    msg = data.get("message") or err.get("message") or err.get("name") or "?"
    code = data.get("statusCode")
    return f"{msg}" + (f" (HTTP {code})" if code else "")


def _fetch_session_usage(http: httpx.Client, session_id: str, write: Writer) -> Usage | None:
    # Возвращаем None при любой проблеме (успешный прогон не падает из-за usage),
    # но дублируем причину на stderr: при недоступном run.log она иначе пропала бы,
    # а успешный run молча получил бы N/A по токенам.
    def note(msg: str) -> None:
        write(f"\n[{msg}]\n")
        print(f"[usage] {msg}", file=sys.stderr)

    try:
        resp = http.get(f"/session/{session_id}/message", timeout=10.0)
    except Exception as exc:
        note(f"usage: не удалось прочитать сообщения: {exc}")
        return None
    if resp.status_code >= 400:
        note(f"usage: GET /message вернул HTTP {resp.status_code}")
        return None
    try:
        return extract_session_usage(resp.json())
    except Exception as exc:
        note(f"usage: не удалось разобрать usage: {exc}")
        return None


def _fetch_session_phase_usages(
    http: httpx.Client,
    session_id: str,
    write: Writer,
) -> tuple[Usage | None, Usage | None]:
    """Best-effort usage split by native OpenCode assistant agent."""
    try:
        resp = http.get(f"/session/{session_id}/message", timeout=10.0)
        resp.raise_for_status()
        messages = resp.json()
    except Exception as exc:
        _safe_write(write, f"\n[usage: не удалось разделить plan/build: {exc}]\n")
        return None, None
    if not isinstance(messages, list):
        return None, None
    grouped: dict[str, list[Usage]] = {"plan": [], "build": []}
    for message in messages:
        info = field(message, "info") or message
        if field(info, "role") != "assistant":
            continue
        agent = field(info, "agent") or field(info, "mode")
        if agent not in grouped:
            continue
        usage = extract_usage_from_message(message)
        if usage is not None:
            grouped[str(agent)].append(usage)
    return merge_usages(grouped["plan"]), merge_usages(grouped["build"])


def _safe_write(write: Writer, msg: str) -> None:
    """write может бросить, если лог уже закрыт (поток-reader живёт дольше)."""
    try:
        write(msg)
    except (OSError, ValueError):
        # Молчим осознанно: единственный потребитель этого сообщения — уже
        # закрытый run.log (OSError/ValueError на закрытом файле); гнать ошибку
        # некуда и она не диагностична. Узкий except: AttributeError/MemoryError
        # и прочие баги должны всплыть, а не молча проглотиться.
        pass


def _reply_to_question(base: str, payload: dict, responder: str,
                       attempt_idx: int, started: float) -> list[dict]:
    """Capture and synchronously answer one question.asked event.

    Две принципиально разные ситуации при POST /question/<id>/reply:

    * HTTPStatusError (4xx/5xx) — сервер ОТВЕТИЛ отказом. Это известный
      детерминированный исход; GET /question reconciliation тут не нужен (мы
      точно знаем, что ответ не принят). Сразу error, копия завершается code=2.
      Раньше raise_for_status падал в общий except и ошибочно шёл в GET, где
      запроса нет в pending — и копия получала ложный reply_status='replied'.

    * TransportError/timeout — неизвестно, принял ли сервер POST. Тогда
      осмотрительно сверяемся с GET /question: запроса уже нет в pending —
      сервер успел принять и обработать → replied (без retry); запрос ещё в
      pending (POST потерялся) → ОДИН retry POST; retry упал → error.
    """
    properties = payload.get("properties") or {}
    captured, answers = capture_question_request(
        properties, responder, attempt_idx=attempt_idx,
        elapsed=max(0.0, time.monotonic() - started),
    )
    request_id = properties["id"]
    try:
        with httpx.Client(base_url=base, timeout=30.0) as http:
            response = http.post(
                f"/question/{request_id}/reply", json={"answers": answers})
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        message = public_reason(str(exc)) or f"HTTP {exc.response.status_code}"
        for item in captured:
            item["reply_status"] = "error"
            item["reply_error"] = message
        raise QuestionProtocolError(
            f"question reply failed: {message}", captured) from exc
    except Exception as exc:
        # Transport/timeout: неизвестно, принял ли сервер POST. Сверяемся с
        # состоянием очереди вопросов, прежде чем решать — отвечено или ретрай.
        # HTTPStatusError уже обработан выше отдельной веткой (4xx/5xx — это
        # известный отказ, reconciliation не нужен) и сюда не попадает.
        try:
            with httpx.Client(base_url=base, timeout=30.0) as http:
                pending = http.get("/question")
                pending.raise_for_status()
                pending_ids = {
                    str(item.get("id")) for item in pending.json()
                    if isinstance(item, dict)
                }
        except Exception as reconcile_exc:
            # Не удалось узнать состояние очереди (HTTP-отказ или transport на
            # GET) — не можем определить, принял ли сервер ответ. Безопасно error.
            message = (public_reason(_retry_reason(reconcile_exc))
                       or reconcile_exc.__class__.__name__)
            for item in captured:
                item["reply_status"] = "error"
                item["reply_error"] = message
            raise QuestionProtocolError(
                f"question reply failed: {message}", captured) from exc
        if request_id not in pending_ids:
            # Сервер принял и обработал ответ (в очереди его уже нет).
            for item in captured:
                item["reply_status"] = "replied"
            return captured
        # POST потерялся по transport, но вопрос ещё ждёт — один retry.
        try:
            with httpx.Client(base_url=base, timeout=30.0) as http:
                response = http.post(
                    f"/question/{request_id}/reply", json={"answers": answers})
                response.raise_for_status()
        except Exception as retry_exc:
            message = (public_reason(_retry_reason(retry_exc))
                       or retry_exc.__class__.__name__)
            for item in captured:
                item["reply_status"] = "error"
                item["reply_error"] = message
            raise QuestionProtocolError(
                f"question reply failed: {message}", captured) from exc
    for item in captured:
        item["reply_status"] = "replied"
    return captured


def _reply_to_plan_exit(base: str, payload: dict) -> None:
    """Approve native plan_exit without recording it as a clarification."""
    properties = payload.get("properties") or {}
    request_id = properties.get("id")
    if not request_id:
        raise QuestionProtocolError("plan_exit question has no id")
    try:
        with httpx.Client(base_url=base, timeout=30.0) as http:
            response = http.post(
                f"/question/{request_id}/reply",
                json={"answers": [["Yes"]]},
            )
            response.raise_for_status()
    except Exception as exc:
        message = public_reason(_retry_reason(exc)) or exc.__class__.__name__
        raise QuestionProtocolError(
            f"plan_exit reply failed: {message}",
        ) from exc


def _abort_on_plan_exit(base: str, session_id: str, _payload: dict) -> None:
    """Keep --questions-only capture-only even if the planner calls plan_exit."""
    try:
        with httpx.Client(base_url=base, timeout=30.0) as http:
            response = http.post(f"/session/{session_id}/abort")
            response.raise_for_status()
    except Exception as exc:
        message = public_reason(_retry_reason(exc)) or exc.__class__.__name__
        raise QuestionProtocolError(
            f"session abort failed: {message}",
        ) from exc


def _plan_path_from_request(payload: dict) -> str | None:
    properties = payload.get("properties") or {}
    questions = properties.get("questions") or []
    if not questions or not isinstance(questions[0], dict):
        return None
    match = _PLAN_PATH_RE.match(str(questions[0].get("question") or ""))
    return match.group(1) if match else None


def _is_plan_exit_request(payload: dict, result: dict) -> bool:
    properties = payload.get("properties") or {}
    tool_ref = properties.get("tool") or {}
    call_id = tool_ref.get("callID") if isinstance(tool_ref, dict) else None
    if call_id and result.get("tool_calls", {}).get(str(call_id)) == "plan_exit":
        return True

    # Compatibility fallback for OpenCode builds that omit tool-call mapping
    # from SSE. Keep it deliberately strict so a user-authored Yes/No question
    # is never mistaken for a control-plane transition.
    questions = properties.get("questions") or []
    if len(questions) != 1 or not isinstance(questions[0], dict):
        return False
    question = questions[0]
    labels = [
        str(option.get("label") or "")
        for option in question.get("options") or []
        if isinstance(option, dict)
    ]
    return (
        question.get("header") == "Build Agent"
        and labels == ["Yes", "No"]
        and _plan_path_from_request(payload) is not None
        and "switch to the build agent" in str(question.get("question") or "")
    )


def _capture_questions_and_abort(base: str, session_id: str, payload: dict,
                                 responder: str, attempt_idx: int,
                                 started: float) -> list[dict]:
    """Capture one question request without answering, then stop the session."""
    properties = payload.get("properties") or {}
    captured, _answers = capture_question_request(
        properties, responder, attempt_idx=attempt_idx,
        elapsed=max(0.0, time.monotonic() - started),
    )
    for item in captured:
        item["answer"] = []
        item["responder"] = "none"
        item["fallback_used"] = False
        item["reply_status"] = "captured"
        item["reply_error"] = None
    try:
        with httpx.Client(base_url=base, timeout=30.0) as http:
            response = http.post(f"/session/{session_id}/abort")
            response.raise_for_status()
    except Exception as exc:
        message = public_reason(str(exc)) or exc.__class__.__name__
        for item in captured:
            item["reply_status"] = "error"
            item["reply_error"] = message
        raise QuestionProtocolError(
            f"session abort failed: {message}", captured) from exc
    return captured


def _retry_reason(exc: BaseException) -> str:
    """Текст причины retry-ошибки для санитайзинга (HTTPStatusError → по коду)."""
    if isinstance(exc, httpx.HTTPStatusError):
        return f"HTTP {exc.response.status_code}"
    return str(exc)


def _session_looks_idle(base: str, session_id: str, write: Writer,
                        timeout: float = 10.0) -> bool:
    """True, если последнее assistant-сообщение сессии завершено (time.completed).

    Используется когда SSE-стрим закрылся штатно, чтобы не пропустить
    session.idle, случившийся в окне между закрытием и переподключением.
    Консервативно: при любой неоднозначности возвращает False (→ реконнект),
    чтобы никогда не выдать ещё работающую сессию за ложный успех.

    `timeout` — таймаут синхронного GET. На пути реконнекта вызывающий передаёт
    короткий SSE_IDLE_CHECK_TIMEOUT: иначе зависший (не упавший) сервер блокирует
    reader-поток на весь таймаут × число реконнектов.
    """
    try:
        with httpx.Client(base_url=base, timeout=timeout) as http:
            resp = http.get(f"/session/{session_id}/message")
        if resp.status_code >= 400:
            return False
        messages = resp.json()
    except Exception as exc:
        # Консервативно False (→ реконнект), но оставляем след в обоих каналах:
        # иначе ошибка доступа к сессии неотличима от штатного «ещё работает».
        _safe_write(write, f"\n[idle-check: не удалось проверить сессию: {exc}]\n")
        print(f"[idle-check] не удалось проверить сессию: {exc}", file=sys.stderr)
        return False
    if not isinstance(messages, list) or not messages:
        return False
    for entry in reversed(messages):
        info = field(entry, "info")
        if info is None:
            info = entry
        if field(info, "role") != "assistant":
            continue
        # Завершено с ошибкой — не «idle-успех»: потерянный session.error нельзя
        # выдать за code 0; основной цикл поднимет причину через provider-tail.
        if field(info, "error"):
            return False
        time_info = field(info, "time") or {}
        # сессия закончила работу: последнее assistant-сообщение завершено.
        return bool(field(time_info, "completed"))
    return False


def _sse_reader(base: str, session_id: str, done: threading.Event,
                stop: threading.Event, result: dict, write: Writer,
                deadline: float | None = None,
                question_handler: Callable[[dict], list[dict]] | None = None,
                stop_after_question: bool = False,
                plan_exit_handler: Callable[[dict], None] | None = None) -> None:
    reconnects = 0
    while not stop.is_set():
        if deadline is not None and time.monotonic() >= deadline:
            # Бюджет исчерпан — пусть основной цикл вынесет честный таймаут.
            return
        try:
            sse_timeout = httpx.Timeout(
                connect=10.0, read=SSE_EVENT_READ_TIMEOUT, write=10.0, pool=10.0)
            with httpx.Client(timeout=sse_timeout) as client:
                with httpx_sse.connect_sse(client, "GET", f"{base}/event") as source:
                    for sse in source.iter_sse():
                        if stop.is_set():
                            return
                        try:
                            payload = json.loads(sse.data)
                        except (json.JSONDecodeError, TypeError):
                            # Служебные/keepalive SSE-кадры без JSON — пропускаем
                            # осознанно; настоящие ошибки сессии приходят
                            # отдельным session.error и логируются ниже.
                            continue
                        sid = _extract_session_id(payload)
                        if sid and sid != session_id:
                            continue
                        etype = payload.get("type", "")
                        if etype == "message.part.updated":
                            part = (payload.get("properties") or {}).get("part") or {}
                            if part.get("type") == "tool" and part.get("callID"):
                                result.setdefault("tool_calls", {})[
                                    str(part["callID"])
                                ] = str(part.get("tool") or part.get("name") or "")
                        if etype == "question.asked" and question_handler is not None:
                            request_id = str(
                                (payload.get("properties") or {}).get("id") or "")
                            if (_is_plan_exit_request(payload, result)
                                    and plan_exit_handler is not None):
                                seen_control = result.setdefault(
                                    "control_question_request_ids", set())
                                if request_id in seen_control:
                                    continue
                                if request_id:
                                    seen_control.add(request_id)
                                try:
                                    plan_exit_handler(payload)
                                    result["plan_path"] = _plan_path_from_request(
                                        payload)
                                    started = result.get("started")
                                    if isinstance(started, (int, float)):
                                        result["plan_elapsed"] = max(
                                            0.0, time.monotonic() - started)
                                    result["plan_completed"] = True
                                    if stop_after_question:
                                        result["questions_only_complete"] = True
                                        done.set()
                                        return
                                except QuestionProtocolError as exc:
                                    result["error"] = str(exc)
                                    done.set()
                                    return
                                continue
                            seen = result.setdefault("question_request_ids", set())
                            if request_id and request_id not in seen:
                                seen.add(request_id)
                                try:
                                    items = question_handler(payload)
                                    round_idx = len(seen)
                                    for item in items:
                                        item["round_idx"] = round_idx
                                    result.setdefault("questions", []).extend(items)
                                    if stop_after_question:
                                        result["questions_only_complete"] = True
                                        done.set()
                                        return
                                except QuestionProtocolError as exc:
                                    round_idx = len(seen)
                                    for item in exc.questions:
                                        item["round_idx"] = round_idx
                                    result.setdefault("questions", []).extend(exc.questions)
                                    result["error"] = str(exc)
                                    done.set()
                                    return
                        msg = _format_event(payload)
                        if msg:
                            write(msg if etype == "message.part.updated" else msg + "\n")
                        if etype == "session.error" and sid == session_id:
                            result["error"] = _error_text(payload.get("properties", {}))
                            done.set()
                            return
                        if etype == "session.idle" and sid == session_id:
                            done.set()
                            return
                    # iter_sse исчерпан ШТАТНО без финального события сессии.
        except Exception as exc:
            # Сетевой обрыв соединения — это ошибка. Реконнектим, пока есть
            # бюджет; если бюджет исчерпан или слишком много обрывов подряд —
            # фиксируем ошибку (битый SSE != молчаливый таймаут).
            if stop.is_set():
                return
            # session.idle мог прийтись на окно обрыва (или тихий период до
            # ReadTimeout) — проверяем статус сессии, как и при graceful-close,
            # иначе завершившийся прогон превратится в ложный таймаут/ошибку.
            if _session_looks_idle(base, session_id, write,
                                   timeout=SSE_IDLE_CHECK_TIMEOUT):
                done.set()
                return
            if stop.is_set():
                return
            reconnects += 1
            # Если до дедлайна не успеем переподключиться — нет смысла ждать,
            # фиксируем ошибку сразу (битый SSE != молчаливый таймаут).
            no_budget_left = (deadline is not None
                              and deadline - time.monotonic() <= SSE_RECONNECT_DELAY)
            if reconnects > SSE_MAX_RECONNECTS or no_budget_left:
                if isinstance(exc, httpx.TransportError):
                    result["error"] = _network_error_reason(
                        "SSE reader error /event", exc)
                else:
                    # Не маскируем неожиданный программный сбой под сеть.
                    result["error"] = f"SSE reader error: {exc}"
                _safe_write(write, f"\n[SSE reader error] {exc}\n")
                done.set()
                return
            _safe_write(write, f"\n[SSE: соединение оборвалось ({exc}), переподключаюсь]\n")
            stop.wait(SSE_RECONNECT_DELAY)
            continue

        # --- штатное закрытие стрима сервером без session.idle/session.error ---
        # Это НЕ ошибка: стрим GET /event — глобальная шина, сервер может его
        # gracefully закрыть, пока сессия ещё работает. Реконнектим, пока есть
        # бюджет; при исчерпании бюджета/лимита реконнектов просто выходим молча,
        # чтобы основной цикл вынес ЧЕСТНЫЙ таймаут (а не подменяем его ошибкой).
        if stop.is_set() or done.is_set():
            return
        _safe_write(write, "\n[SSE: сервер закрыл /event без session.idle, "
                           "проверяю статус сессии и переподключаюсь]\n")
        if _session_looks_idle(base, session_id, write,
                               timeout=SSE_IDLE_CHECK_TIMEOUT):
            done.set()
            return
        if stop.is_set():
            return
        reconnects += 1
        if reconnects > SSE_MAX_RECONNECTS:
            return
        if deadline is not None and time.monotonic() >= deadline:
            return
        stop.wait(SSE_RECONNECT_DELAY)


def probe_session(task: str, model: str, provider: str, agent: str, timeout: float,
                  port: int, write: Writer, planning: bool = False,
                  question_responder: str = "recommended",
                  questions_only: bool = False) -> SessionProbeResult:
    """Гоняет сессию агента, ретраит при лимите провайдера с backoff.

    `timeout` — бюджет wall-clock ВСЕЙ копии, общий на все попытки, включая
    backoff-паузы между ними (issue #139). Абсолютный deadline считается здесь
    один раз и передаётся в каждую попытку; ретрай не стартует, если бюджет уже
    исчерпан. После исчерпания ретраев (или бюджета) — отдельный статус «лимит»
    (code=3), а не обычная «ошибка».
    """
    # Стартовая пауза SSE-reader идёт «сверх» бюджета (см. _probe_session_once):
    # при коротком timeout иначе дедлайн истёк бы ещё до отправки задачи.
    deadline = (None if timeout <= 0
                else time.monotonic() + SSE_READER_STARTUP_DELAY + timeout)
    # Цикл всегда делает ≥1 итерацию (RATE_LIMIT_MAX_ATTEMPTS >= 1), а выйти из
    # него без return можно лишь через rate_limited-результат → `last` тут не None.
    last = None
    all_questions: list[dict] = []
    for attempt in range(1, RATE_LIMIT_MAX_ATTEMPTS + 1):
        once_kwargs = {
            "planning": planning,
            "question_responder": question_responder,
            "attempt_idx": attempt,
            "deadline": deadline,
        }
        if questions_only:
            once_kwargs["questions_only"] = True
        res = _probe_session_once(
            task, model, provider, agent, timeout, port, write, **once_kwargs)
        all_questions.extend(res.questions)
        if not res.rate_limited:
            return SessionProbeResult(
                res.code, res.reason, res.usage, res.rate_limited,
                tuple(all_questions),
                res.plan_path, res.plan_elapsed, res.build_elapsed,
                res.plan_usage, res.build_usage, res.plan_completed,
                res.post_hung,
            )
        last = res
        if attempt < RATE_LIMIT_MAX_ATTEMPTS:
            delay = _rate_limit_backoff(attempt)
            # Бюджет копии общий: если после паузы времени на попытку уже не
            # останется, ретраить незачем — иначе копия шла бы кратно дольше
            # --timeout (issue #139).
            if deadline is not None and time.monotonic() + delay >= deadline:
                write("\n[rate limit] бюджет --timeout исчерпан, "
                      "ретраи прекращены\n")
                break
            write(f"\n[rate limit] попытка {attempt}/{RATE_LIMIT_MAX_ATTEMPTS} "
                  f"упёрлась в лимит провайдера, жду {delay:.0f}с и повторяю...\n")
            time.sleep(delay)
    write("\n--- лимит провайдера: retry исчерпан ---\n")
    return SessionProbeResult(
        3, last.reason, last.usage, questions=tuple(all_questions),
        plan_path=last.plan_path, plan_elapsed=last.plan_elapsed,
        build_elapsed=last.build_elapsed, plan_usage=last.plan_usage,
        build_usage=last.build_usage, plan_completed=last.plan_completed,
    )


def _exit_state(result: dict, done: threading.Event) -> str | None:
    """Причина выйти из poll-loop: 'error' (reader сообщил ошибку) или 'idle'
    (сессия завершилась). None — продолжаем ждать. 'error' имеет приоритет."""
    if result.get("error"):
        return "error"
    if done.is_set():
        return "idle"
    return None


def _wait_for_session(
    done: threading.Event,
    result: dict,
    deadline: float | None,
    provider_limit_tail: Callable[[], str | None],
) -> tuple[str, str | None]:
    """Ждёт исхода сессии. Возвращает (outcome, limit_tail):
      'error'    — reader сообщил ошибку (result['error']);
      'idle'     — сессия завершилась (done);
      'limit'    — в логе opencode найден лимит провайдера (limit_tail задан);
      'deadline' — истёк дедлайн.
    error/idle проверяются и до, и после чтения лога (оно делает I/O, за время
    которого сессия может завершиться) — поэтому _exit_state зовётся дважды.

    NB: 'idle' из done.wait() может гонкой совпасть с выставленным reader'ом
    result['error'] (его тут уже не перепроверяем). Поэтому вызывающий после
    'idle' ОБЯЗАН сначала проверить result.get('error') (error-first)."""
    while True:
        state = _exit_state(result, done)
        if state:
            return state, None
        limit_tail = provider_limit_tail()
        state = _exit_state(result, done)
        if state:
            return state, None
        if limit_tail:
            return "limit", limit_tail

        wait_for = PROVIDER_LIMIT_LOG_POLL_INTERVAL
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "deadline", None
            wait_for = min(wait_for, remaining)
        if done.wait(timeout=wait_for):
            return "idle", None


def _provider_error_tail(session_id: str, agent: str, write: Writer) -> str | None:
    """Хвост ошибок провайдера из лога opencode (с agent= и fallback без него).

    Найденный tail копируется в лог копии. Вынесено из замыкания внутри
    _probe_session_once (#74): захватывало только session_id/agent/write."""
    tail = (_opencode_error_tail(session_id, agent=agent)
            or _opencode_error_tail(session_id))
    if tail:
        write("\n--- ошибки провайдера из лога opencode ---\n"
              f"{tail}\n")
    return tail


def _provider_limit_tail(session_id: str, agent: str, write: Writer) -> str | None:
    """Хвост лога, только если это лимит провайдера (для in-loop детекта в
    _wait_for_session). Вынесено из замыкания (#74)."""
    tail = _opencode_error_tail(session_id, agent=agent)
    if not tail or not _is_provider_limit_error(tail):
        return None
    write("\n--- лимит провайдера из лога opencode ---\n"
          f"{tail}\n")
    return tail


def _with_tail(reason: str, session_id: str, agent: str, write: Writer) -> str:
    """Дополняет reason первой строкой tail-а провайдера, если она привносит сигнал
    (не дублирует уже присутствующий). Вынесено из замыкания (#74)."""
    tail = _provider_error_tail(session_id, agent, write)
    if not tail:
        return reason
    first_line = tail.splitlines()[0]
    sig = max(first_line.split(" | "), key=len).strip()
    if sig and sig in reason:
        return reason
    return f"{reason} | {first_line}"


def _open_session(http: httpx.Client, agent: str, provider: str, model: str,
                  write: Writer) -> str | SessionProbeResult:
    """Создаёт сессию и валидирует ответ. Возвращает session_id либо ранний
    error-result (если сервер вернул не-dict / dict без id)."""
    write(f"Создаю сессию (агент: {agent})...\n")
    resp = http.post("/session", json={})
    try:
        sess = resp.json()
    except Exception:
        sess = None
    # Сервер может вернуть не-dict (строку ошибки, null) или dict без "id":
    # тогда sess["id"] упал бы KeyError/TypeError, а reader-поток ещё не
    # запущен — отдаём честную ошибку вместо краша.
    if not isinstance(sess, dict) or "id" not in sess:
        reason = f"неожиданный ответ POST /session (HTTP {resp.status_code}): {sess!r:.200}"
        write(f"\n--- ошибка ---\n[{reason}]\n")
        return SessionProbeResult(2, reason)
    session_id = sess["id"]
    write(f"Сессия: {session_id}\n")
    write(f"Модель: {provider}/{model}\n")
    write("--- работа ---\n")
    return session_id


def _post_task(http: httpx.Client, session_id: str, agent: str, body: dict,
               deadline: float | None, write: Writer
               ) -> tuple[Usage | None, SessionProbeResult | None, bool]:
    """POST задачи агенту + классификация НЕМЕДЛЕННЫХ ошибок (HTTP≥400 / info.error).

    Возвращает (usage, result, post_hung): result=None — немедленной ошибки нет,
    продолжаем ждать события до дедлайна. POST пропускается, если бюджет уже истёк
    (post_timeout<=0).

    ReadTimeout сам по себе не ошибка (события могут прийти позже), но факт
    «ответа на POST не было» поднимается наружу третьим элементом — post_hung
    (issue #124, угол C). Раньше он оставался только маркером в run.log, и
    сессия, закрывшаяся после этого по idle, отдавала code=0 «готово» — ложный
    успех без единого артефакта. Решение принимает _classify_outcome.

    Достоверно pre-dispatch ConnectError/ConnectTimeout/PoolTimeout сразу дают
    code=2 (#158). Остальные httpx.TransportError неоднозначны: serve мог принять
    POST, поэтому result несёт network fallback, а post_hung=True разрешает
    вызывающему предпочесть уже выставленный SSE-исход (PR #159 cycle 1)."""
    usage: Usage | None = None
    post_timeout = _message_post_timeout(deadline, time.monotonic())
    if post_timeout <= 0:
        # Бюджет истёк ещё до отправки — POST не делался. Это не «зависший»
        # POST: копию и так закроет дедлайн в _wait_for_session (code=1).
        return usage, None, False
    post_start = time.monotonic()
    try:
        resp = http.post(
            f"/session/{session_id}/message",
            json=body,
            timeout=post_timeout,
        )
        try:
            payload = resp.json() or {}
        except Exception:
            # Битое тело ответа не теряет причину: при HTTP>=400 reason
            # ниже берётся из status_code/resp.text, не из payload.
            payload = {}
        usage = extract_usage_from_message(payload)
        if resp.status_code >= 400:
            write(f"\n--- ошибка ---\n[HTTP {resp.status_code}] {resp.text[:400]}\n")
            reason = f"HTTP {resp.status_code}: {resp.text[:200].strip()}"
            tailed = _with_tail(reason, session_id, agent, write)
            is_limit = (resp.status_code == 429
                        or _is_retryable_limit_error(tailed))
            return usage, SessionProbeResult(2, tailed, usage,
                                             rate_limited=is_limit), False
        info = payload.get("info", {}) if isinstance(payload, dict) else {}
        if isinstance(info, dict) and info.get("error"):
            reason = _with_tail(_error_text(info), session_id, agent, write)
            is_limit = _is_retryable_limit_error(reason)
            write(f"\n--- ошибка ---\n[{reason}]\n")
            return usage, SessionProbeResult(2, reason, usage,
                                             rate_limited=is_limit), False
    except httpx.ReadTimeout:
        waited = time.monotonic() - post_start
        write(f"\n[POST /message не ответил за {waited:.1f}с — "
              "продолжаем ждать события до дедлайна]\n")
        return usage, None, True
    except (httpx.ConnectError, httpx.ConnectTimeout,
            httpx.PoolTimeout) as exc:
        # Эти ошибки происходят до получения ответа: достоверного признака, что
        # serve принял POST и начал работу, нет — немедленный network code=2.
        reason = _network_error_reason("POST /message", exc)
        write(f"\n--- ошибка ---\n[{reason}]\n")
        return usage, SessionProbeResult(2, reason, usage), False
    except httpx.TransportError as exc:
        # ReadError/RemoteProtocolError и прочие response-side сбои неоднозначны:
        # serve мог принять POST и уже прислать итог по независимому SSE-каналу.
        # Оставляем network code=2 как fallback, но сигнал разрешает вызывающему
        # предпочесть уже выставленный idle/error и применить прежние правила.
        reason = _network_error_reason("POST /message", exc)
        write(f"\n[POST /message: ответ оборвался после возможной отправки; "
              f"проверяем SSE ({type(exc).__name__}: {exc})]\n")
        return usage, SessionProbeResult(2, reason, usage), True
    return usage, None, False


def _classify_outcome(outcome: str, limit_tail: str | None, result: dict,
                      usage: Usage | None, no_answer_reason: str,
                      http: httpx.Client, session_id: str, agent: str,
                      write: Writer, post_hung: bool = False) -> SessionProbeResult:
    """Маппинг исхода _wait_for_session → SessionProbeResult.

    Порядок веток сохранён: limit → error-first (ошибка reader'а приоритетнее
    idle/таймаута даже при гонке) → idle → deadline (таймаут, с апгрейдом в лимит,
    если в tail без agent= нашёлся ретраябельный лимит). no_answer_reason — готовая
    формулировка таймаута (оркестратор знает deadline/timeout, классификатор — нет).

    post_hung (issue #124) — POST /message не ответил (ReadTimeout). Ветку idle
    он НЕ переклассифицирует: POST и SSE-reader — независимые каналы, и сессия
    могла честно доработать (события принёс reader). Признак лишь пробрасывается
    в SessionProbeResult; «пустой успех» отсекает run_copy, у которого есть
    work_dir и, значит, факт наличия файла модели. Остальные ветки
    (limit/error/deadline) уже отдают ошибку и признака не несут."""
    if outcome == "limit":
        first_line = limit_tail.splitlines()[0]
        is_limit = _is_retryable_limit_error(first_line)
        label = "provider limit" if is_limit else "provider error"
        reason = f"{label} | {first_line}"
        write(f"\n--- ошибка ---\n[{reason}]\n")
        return SessionProbeResult(2, reason, usage, rate_limited=is_limit)

    # error-first: ошибка reader'а имеет приоритет над idle/таймаутом, даже
    # если сессия успела «завершиться» одновременно (как в прежнем post-loop).
    if result.get("error"):
        reason = result["error"]
        write(f"\n--- ошибка ---\n[{reason}]\n")
        tailed = _with_tail(reason, session_id, agent, write)
        return SessionProbeResult(
            2, tailed, usage,
            rate_limited=_is_retryable_limit_error(tailed),
        )
    if outcome == "idle":
        full_usage = _fetch_session_usage(http, session_id, write)
        if full_usage is not None:
            usage = full_usage
        write("\n--- готово ---\n")
        # issue #124: post_hung поднимается как СИГНАЛ, а не приговор. POST
        # /message и SSE-reader — независимые каналы: ReadTimeout на POST не
        # доказывает, что работы не было (события читает отдельный поток, и
        # сессия могла дойти до idle, оставив файл модели). Классификатор здесь
        # артефактов не видит — итог решает run_copy по work_dir.
        return SessionProbeResult(0, None, usage, post_hung=post_hung)

    # Сюда доходит единственный оставшийся исход — 'deadline' (бюджет
    # timeout истёк без ответа): обычный таймаут (или лимит из tail ниже).
    write("\n--- таймаут ---\n")
    tail = _provider_error_tail(session_id, agent, write)
    reason = no_answer_reason
    if tail:
        first_line = tail.splitlines()[0]
        reason = f"{reason} | {first_line}"
        # Реальный лимит провайдера (HTTP 429 и т.п.) мог быть записан в
        # лог opencode БЕЗ токена agent= и проскочить мимо in-loop детекта
        # (_provider_limit_tail зовёт _opencode_error_tail только с agent=).
        # _provider_error_tail() выше делает no-agent fallback и находит
        # такой tail. Если это ретраябельный лимит — помечаем rate_limited,
        # чтобы probe_session ретраил с backoff и отдал code=3 «лимит», а
        # не выдавал лимит за обычный таймаут (как остальные error-ветки).
        if _is_retryable_limit_error(first_line):
            return SessionProbeResult(2, reason, usage, rate_limited=True)
    return SessionProbeResult(1, reason, usage)


def _probe_session_once(task: str, model: str, provider: str, agent: str,
                        timeout: float, port: int, write: Writer, *,
                        planning: bool = False,
                        question_responder: str = "recommended",
                        questions_only: bool = False,
                        attempt_idx: int = 1,
                        deadline: float | None = None) -> SessionProbeResult:
    """Один прогон сессии: создать → запустить SSE-reader → отправить задачу →
    дождаться исхода → классифицировать. Тонкий оркестратор; фазы — функции выше.
    Ретраи при лимите провайдера — в probe_session.

    `deadline` — абсолютный дедлайн ВСЕЙ копии, общий на все попытки (issue
    #139); считает его probe_session. None = дедлайна нет: либо timeout <= 0
    (без лимита), либо прямой вызов без ретраев — тогда бюджет строим здесь из
    своего timeout, чтобы одиночная попытка не осталась вовсе без дедлайна.
    """
    if deadline is None and timeout > 0:
        deadline = time.monotonic() + SSE_READER_STARTUP_DELAY + timeout
    base = base_url(port).rstrip("/")

    with httpx.Client(base_url=base, timeout=30.0) as http:
        opened = _open_session(http, agent, provider, model, write)
        if isinstance(opened, SessionProbeResult):
            return opened
        session_id = opened

        done = threading.Event()
        stop = threading.Event()
        started = time.monotonic()
        result: dict = {"started": started}
        if questions_only:
            def capture_handler(payload: dict) -> list[dict]:
                return _capture_questions_and_abort(
                    base, session_id, payload, question_responder, attempt_idx,
                    started)

            def questions_only_plan_exit_handler(payload: dict) -> None:
                _abort_on_plan_exit(base, session_id, payload)

            question_handler = capture_handler
            plan_exit_handler = questions_only_plan_exit_handler
        elif planning:
            def reply_handler(payload: dict) -> list[dict]:
                return _reply_to_question(
                    base, payload, question_responder, attempt_idx, started)

            def approve_plan_exit_handler(payload: dict) -> None:
                _reply_to_plan_exit(base, payload)

            question_handler = reply_handler
            plan_exit_handler = approve_plan_exit_handler
        else:
            question_handler = None
            plan_exit_handler = None
        reader = threading.Thread(
            target=_sse_reader,
            args=(base, session_id, done, stop, result, write, deadline,
                  question_handler, questions_only, plan_exit_handler),
            daemon=True,
        )
        reader.start()
        time.sleep(SSE_READER_STARTUP_DELAY)

        body = {
            "agent": agent,
            "model": {"providerID": provider, "modelID": model},
            # The benchmark task is the measured input. Control-plane modes
            # (planning/questions-only) must never prefix, suffix, normalize,
            # or otherwise rewrite it.
            "parts": [{"type": "text", "text": task}],
        }

        try:
            usage, early, post_hung = _post_task(
                http, session_id, agent, body, deadline, write)
            if result.get("questions_only_complete"):
                return SessionProbeResult(
                    0, None, usage,
                    questions=tuple(result.get("questions", ())),
                )
            if early is not None:
                # Response-side transport-сбой мог случиться ПОСЛЕ принятия POST.
                # Если SSE уже успел сообщить idle/error, его исход важнее network
                # fallback: ниже сработают штатные error-first/rate-limit/idle и
                # artifact/post_hung правила. Pre-dispatch ошибки (post_hung=False)
                # и неоднозначный сбой без готового SSE-state остаются code=2.
                sse_state = _exit_state(result, done) if post_hung else None
                if sse_state is None:
                    return SessionProbeResult(
                        early.code, early.reason, early.usage,
                        early.rate_limited,
                        tuple(result.get("questions", ())),
                    )
            outcome, limit_tail = _wait_for_session(
                done, result, deadline,
                lambda: _provider_limit_tail(session_id, agent, write))
            if result.get("questions_only_complete"):
                return SessionProbeResult(
                    0, None, usage,
                    questions=tuple(result.get("questions", ())),
                )
            no_answer_reason = ("нет ответа" if deadline is None
                                else f"нет ответа за {timeout:.0f}с")
            classified = _classify_outcome(
                outcome, limit_tail, result, usage, no_answer_reason,
                http, session_id, agent, write,
                # issue #124: questions-only намеренно обрывает сессию после
                # сбора вопросов — POST там не отвечает штатно, и признак пустого
                # успеха поднимать не за что. run_copy questions_only проверяет
                # тоже; гасим и здесь, чтобы сигнал не уезжал наружу вообще.
                post_hung=post_hung and not questions_only,
            )
            if (planning and not questions_only and classified.code == 0
                    and not result.get("plan_completed")):
                classified = SessionProbeResult(
                    2,
                    "planning завершился без plan_exit; build не был запущен",
                    classified.usage,
                )
            plan_usage = build_usage = None
            if planning and not questions_only:
                plan_usage, build_usage = _fetch_session_phase_usages(
                    http, session_id, write)
            total_elapsed = max(0.0, time.monotonic() - started)
            plan_elapsed = result.get("plan_elapsed")
            build_elapsed = (
                max(0.0, total_elapsed - plan_elapsed)
                if isinstance(plan_elapsed, (int, float)) else None
            )
            return SessionProbeResult(
                classified.code, classified.reason, classified.usage,
                classified.rate_limited, tuple(result.get("questions", ())),
                result.get("plan_path"), plan_elapsed, build_elapsed,
                plan_usage, build_usage, bool(result.get("plan_completed")),
                classified.post_hung,
            )
        finally:
            stop.set()
            reader.join(timeout=1.0)

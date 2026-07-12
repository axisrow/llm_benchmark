"""Normalization and deterministic replies for OpenCode question requests."""

from __future__ import annotations

from typing import Any


class QuestionProtocolError(ValueError):
    """The question request cannot be answered deterministically."""

    def __init__(self, message: str, questions: list[dict[str, Any]] | None = None):
        super().__init__(message)
        self.questions = questions or []


def capture_question_request(
    request: dict[str, Any],
    responder: str,
    *,
    attempt_idx: int,
    elapsed: float,
) -> tuple[list[dict[str, Any]], list[list[str]]]:
    """Normalize a request and select answers in OpenCode wire format.

    Невалидный вопрос (нет options, нет label) нельзя ответить детерминированно,
    но его запись всё равно попадает в результат — с reply_status='error' и
    санитизированным reply_error (фиксированная категория, без сырого тела). Так
    error-вопрос доходит до runs[].questions/agent_questions, а копия завершается
    с code=2 (вызывающий ловит QuestionProtocolError). Если второй вопрос в одном
    request-е невалиден, первый (уже нормализованный) не теряется.
    """
    request_id = str(request.get("id") or "")
    if responder not in {"first", "recommended"}:
        raise ValueError(f"unknown question responder: {responder}")
    session_id = str(request.get("sessionID") or "")
    questions = request.get("questions")
    if not request_id or not isinstance(questions, list):
        # Сам запрос нельзя связать с ответом (нет id/questions) — записей нет,
        # но исключение по контракту несёт captured (здесь пустой).
        raise QuestionProtocolError("question request has no id/questions")
    captured: list[dict[str, Any]] = []
    answers: list[list[str]] = []
    errors: list[str] = []
    for index, question in enumerate(questions, 1):
        record = _capture_one_question(
            question, index, request_id, session_id, responder,
            attempt_idx=attempt_idx, elapsed=elapsed,
        )
        captured.append(record)
        if record["reply_status"] == "error":
            errors.append(record["reply_error"] or "invalid question")
            continue
        answers.append(record["answer"])
    if errors:
        # Несём ВСЕ записи (валидные + error) — валидные не теряются, если
        # ошибся второй вопрос в request-е.
        raise QuestionProtocolError("; ".join(errors), captured)
    return captured, answers


def _capture_one_question(
    question: Any, index: int, request_id: str, session_id: str,
    responder: str, *, attempt_idx: int, elapsed: float,
) -> dict[str, Any]:
    """Нормализует один вопрос. Невалидному ставит reply_status='error'."""
    options = question.get("options") if isinstance(question, dict) else None
    base: dict[str, Any] = {
        "attempt_idx": attempt_idx,
        "session_id": session_id,
        "request_id": request_id,
        # Перезаписывается в _sse_reader на номер раунда вопросов в сессии
        # (дедуп по request_id); здесь 0 — лишь заглушка до перехвата.
        "round_idx": 0,
        "question_idx": index,
        "header": (question.get("header") if isinstance(question, dict) else None),
        "question": str(question.get("question") or "")
        if isinstance(question, dict) else "",
        "options": (options if isinstance(options, list) else []),
        "multiple": bool(question.get("multiple"))
        if isinstance(question, dict) else False,
        "custom": bool(question.get("custom", True))
        if isinstance(question, dict) else True,
        "answer": [],
        "responder": responder,
        "fallback_used": False,
        "reply_status": "pending",
        "reply_error": None,
        "elapsed": elapsed,
    }
    if not isinstance(options, list) or not options:
        base["reply_status"] = "error"
        base["reply_error"] = "question has no options"
        return base
    labels = [str(option.get("label") or "") for option in options]
    if not all(labels):
        base["reply_status"] = "error"
        base["reply_error"] = "question option has no label"
        return base
    matches = [label for label in labels if "recommended" in label.lower()]
    fallback = responder == "recommended" and not matches
    if responder == "first" or fallback:
        selected = labels[:1]
    elif question.get("multiple"):
        selected = matches
    else:
        selected = matches[:1]
    base["answer"] = selected
    base["fallback_used"] = fallback
    return base

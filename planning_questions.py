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
    """Normalize a request and select answers in OpenCode wire format."""
    request_id = str(request.get("id") or "")
    if responder not in {"first", "recommended"}:
        raise ValueError(f"unknown question responder: {responder}")
    session_id = str(request.get("sessionID") or "")
    questions = request.get("questions")
    if not request_id or not isinstance(questions, list):
        raise QuestionProtocolError("question request has no id/questions")
    captured: list[dict[str, Any]] = []
    answers: list[list[str]] = []
    for index, question in enumerate(questions, 1):
        options = question.get("options") if isinstance(question, dict) else None
        if not isinstance(options, list) or not options:
            raise QuestionProtocolError("question has no options")
        labels = [str(option.get("label") or "") for option in options]
        if not all(labels):
            raise QuestionProtocolError("question option has no label")
        matches = [label for label in labels if "recommended" in label.lower()]
        fallback = responder == "recommended" and not matches
        if responder == "first" or fallback:
            selected = labels[:1]
        elif question.get("multiple"):
            selected = matches
        else:
            selected = matches[:1]
        answers.append(selected)
        captured.append({
            "attempt_idx": attempt_idx,
            "session_id": session_id,
            "request_id": request_id,
            "round_idx": 0,
            "question_idx": index,
            "header": question.get("header"),
            "question": str(question.get("question") or ""),
            "options": options,
            "multiple": bool(question.get("multiple")),
            "custom": bool(question.get("custom", True)),
            "answer": selected,
            "responder": responder,
            "fallback_used": fallback,
            "reply_status": "pending",
            "reply_error": None,
            "elapsed": elapsed,
        })
    return captured, answers

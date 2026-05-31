"""Token usage extraction and local cost estimation for benchmark reports."""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, replace


def field(obj: object, name: str) -> object | None:
    """Read a field from either a dict-like payload or an SDK object."""
    if isinstance(obj, Mapping):
        return obj.get(name)
    return getattr(obj, name, None)


def as_token(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return int(number)


def as_money(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


@dataclass(frozen=True)
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    reasoning_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    estimated_prompt_cost_usd: float | None = None
    estimated_completion_cost_usd: float | None = None
    estimated_cost_usd: float | None = None
    opencode_cost_usd: float | None = None

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens + self.reasoning_tokens

    def to_report_dict(self) -> dict[str, int | float | None]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "total_tokens": self.total_tokens,
            "estimated_prompt_cost_usd": self.estimated_prompt_cost_usd,
            "estimated_completion_cost_usd": self.estimated_completion_cost_usd,
            "estimated_cost_usd": self.estimated_cost_usd,
            "opencode_cost_usd": self.opencode_cost_usd,
        }


def usage_from_tokens(tokens: object, cost: object = None) -> Usage | None:
    """Normalize OpenCode tokens.* into the benchmark Usage shape."""
    if tokens is None:
        return None
    input_tokens = as_token(field(tokens, "input"))
    output_tokens = as_token(field(tokens, "output"))
    reasoning_tokens = as_token(field(tokens, "reasoning"))
    if input_tokens is None and output_tokens is None and reasoning_tokens is None:
        return None

    cache = field(tokens, "cache") or {}
    return Usage(
        input_tokens=input_tokens or 0,
        output_tokens=output_tokens or 0,
        reasoning_tokens=reasoning_tokens or 0,
        cache_read_tokens=as_token(field(cache, "read")) or 0,
        cache_write_tokens=as_token(field(cache, "write")) or 0,
        opencode_cost_usd=as_money(cost),
    )


def merge_usages(usages: Iterable[Usage]) -> Usage | None:
    present = list(usages)
    if not present:
        return None
    costs = [u.opencode_cost_usd for u in present if u.opencode_cost_usd is not None]
    return Usage(
        input_tokens=sum(u.input_tokens for u in present),
        output_tokens=sum(u.output_tokens for u in present),
        reasoning_tokens=sum(u.reasoning_tokens for u in present),
        cache_read_tokens=sum(u.cache_read_tokens for u in present),
        cache_write_tokens=sum(u.cache_write_tokens for u in present),
        opencode_cost_usd=sum(costs) if costs else None,
    )


def extract_usage_from_message(payload: object) -> Usage | None:
    """Extract usage from `{info, parts}` or a direct AssistantMessage payload."""
    info = field(payload, "info") or payload
    usage = usage_from_tokens(field(info, "tokens"), field(info, "cost"))
    if usage is not None:
        return usage

    parts = field(payload, "parts") or []
    part_usages: list[Usage] = []
    for part in parts:
        if field(part, "type") != "step-finish":
            continue
        part_usage = usage_from_tokens(field(part, "tokens"), field(part, "cost"))
        if part_usage is not None:
            part_usages.append(part_usage)
    return merge_usages(part_usages)


def extract_session_usage(messages: object) -> Usage | None:
    """Sum usage across assistant messages returned for a session."""
    if not isinstance(messages, list):
        return extract_usage_from_message(messages)

    usages: list[Usage] = []
    for item in messages:
        info = field(item, "info") or item
        if field(info, "role") != "assistant":
            continue
        usage = extract_usage_from_message(item)
        if usage is not None:
            usages.append(usage)
    return merge_usages(usages)


def estimate_usage_cost(usage: Usage | None, pricing: Mapping[str, object] | None) -> Usage | None:
    """Add local prompt/completion USD estimate. Missing price or usage stays unknown."""
    if usage is None:
        return None

    prompt_price = as_money((pricing or {}).get("prompt_per_1m"))
    completion_price = as_money((pricing or {}).get("completion_per_1m"))
    if prompt_price is None or completion_price is None:
        return replace(
            usage,
            estimated_prompt_cost_usd=None,
            estimated_completion_cost_usd=None,
            estimated_cost_usd=None,
        )

    prompt_cost = usage.input_tokens * prompt_price / 1_000_000
    completion_cost = usage.output_tokens * completion_price / 1_000_000
    return replace(
        usage,
        estimated_prompt_cost_usd=prompt_cost,
        estimated_completion_cost_usd=completion_cost,
        estimated_cost_usd=prompt_cost + completion_cost,
    )


def summarize_usages(usages: Iterable[Usage | None]) -> dict[str, int | float | None]:
    present = [u for u in usages if u is not None]
    costs = [u.estimated_cost_usd for u in present if u.estimated_cost_usd is not None]
    merged = merge_usages(present)
    return {
        "input_tokens": merged.input_tokens if merged else None,
        "output_tokens": merged.output_tokens if merged else None,
        "reasoning_tokens": merged.reasoning_tokens if merged else None,
        "total_tokens": merged.total_tokens if merged else None,
        "estimated_cost_usd": sum(costs) if costs else None,
        "runs_with_usage": len(present),
        "runs_with_estimated_cost": len(costs),
    }


def format_tokens(value: object) -> str:
    tokens = as_token(value)
    return f"{tokens:,}" if tokens is not None else "N/A"


def format_usd_cost(value: object) -> str:
    cost = as_money(value)
    if cost is None:
        return "N/A"
    if cost == 0:
        return "$0"
    return f"${cost:.6f}" if abs(cost) < 0.01 else f"${cost:.4f}"

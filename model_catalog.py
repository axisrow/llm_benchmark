"""Каталог моделей opencode без запуска `opencode serve`."""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from typing import Any

from db import model_key, split_model_ref

OPENCODE_MODELS_TIMEOUT = 60.0


class ModelCatalogError(RuntimeError):
    """Не удалось получить или разобрать каталог моделей opencode."""


@dataclass(frozen=True)
class ModelCatalogEntry:
    provider: str
    model: str
    name: str | None = None
    metadata: dict[str, Any] | None = None

    @property
    def key(self) -> str:
        return model_key(self.provider, self.model)

    @property
    def cost(self) -> dict[str, Any] | None:
        metadata = self.metadata or {}
        cost = metadata.get("cost")
        return cost if isinstance(cost, dict) else None


def _entry_from_key(key: str, metadata: dict[str, Any] | None = None) -> ModelCatalogEntry:
    try:
        provider, model = split_model_ref(key)
    except ValueError as exc:
        raise ModelCatalogError(str(exc)) from exc
    name = None
    if metadata is not None and isinstance(metadata.get("name"), str):
        name = metadata["name"]
    return ModelCatalogEntry(
        provider=provider,
        model=model,
        name=name,
        metadata=metadata,
    )


def _parse_json_block(lines: list[str], start: int) -> tuple[dict[str, Any], int]:
    block: list[str] = []
    for index in range(start, len(lines)):
        block.append(lines[index])
        try:
            value = json.loads("\n".join(block))
        except json.JSONDecodeError:
            continue
        if not isinstance(value, dict):
            raise ModelCatalogError("verbose metadata must be a JSON object")
        return value, index + 1
    raise ModelCatalogError("unterminated verbose metadata JSON block")


def _is_refresh_banner(line: str) -> bool:
    return line.strip().startswith("Models cache refreshed")


def parse_opencode_models_output(output: str) -> list[ModelCatalogEntry]:
    """Парсит stdout `opencode models`.

    Поддерживаются оба формата:
    - обычный: по одному `provider/model` на строку;
    - verbose: строка `provider/model`, затем pretty-printed JSON metadata.
    """
    entries: list[ModelCatalogEntry] = []
    lines = output.splitlines()
    index = 0
    while index < len(lines):
        key = lines[index].strip()
        index += 1
        if not key:
            continue
        if _is_refresh_banner(key):
            continue
        if key.startswith("{"):
            raise ModelCatalogError("metadata block without model key")

        metadata = None
        if index < len(lines) and lines[index].lstrip().startswith("{"):
            metadata, index = _parse_json_block(lines, index)

        entries.append(_entry_from_key(key, metadata))
    return entries


def _run_opencode_models(provider: str | None, refresh: bool,
                         verbose: bool) -> str:
    cmd = ["opencode", "models"]
    if provider:
        cmd.append(provider)
    cmd.append("--pure")
    if refresh:
        cmd.append("--refresh")
    if verbose:
        cmd.append("--verbose")

    try:
        completed = subprocess.run(
            cmd,
            capture_output=True,
            check=True,
            text=True,
            timeout=OPENCODE_MODELS_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise ModelCatalogError("opencode CLI not found") from exc
    except subprocess.TimeoutExpired as exc:
        raise ModelCatalogError(
            f"opencode models timed out after {OPENCODE_MODELS_TIMEOUT:.0f}s",
        ) from exc
    except subprocess.CalledProcessError as exc:
        message = (exc.stderr or exc.stdout or "").strip()
        raise ModelCatalogError(message or "opencode models failed") from exc
    return completed.stdout


def load_opencode_models(provider: str | None = None,
                         refresh: bool = False) -> list[ModelCatalogEntry]:
    """Возвращает effective список моделей opencode без запуска serve.

    Основной контракт — `opencode models --pure --verbose`. Если verbose-формат
    недоступен или сломан, делаем fallback на простой список ключей.
    """
    try:
        return parse_opencode_models_output(
            _run_opencode_models(provider, refresh, verbose=True),
        )
    except ModelCatalogError as verbose_error:
        try:
            return parse_opencode_models_output(
                _run_opencode_models(provider, refresh, verbose=False),
            )
        except ModelCatalogError:
            raise verbose_error

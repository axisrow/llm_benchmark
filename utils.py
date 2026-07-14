"""Общие утилиты, не привязанные к конкретному слою (БД, runtime, CLI)."""

import json
import re


def json_loads_or(text: str, default: object = None) -> object:
    """json.loads с откатом на *default* при ошибке парсинга.

    Ловит JSONDecodeError, TypeError, RecursionError. Не валидирует тип
    результата — если json.loads вернул dict/int/None при ожидаемом list,
    вызывающий должен проверить тип сам или положиться на «or []».
    """
    try:
        return json.loads(text)
    except (json.JSONDecodeError, TypeError, RecursionError):
        return default


def sanitize_name(name: str) -> str:
    """Имя, безопасное для файловой системы (буквы/цифры/._-, прочее → '-')."""
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    cleaned = re.sub(r"\.{2,}", ".", cleaned).strip("-.")
    return cleaned or "x"


def is_canonical_project_name(name: str) -> bool:
    """Можно ли однозначно использовать project name как имя disk-каталога."""
    return sanitize_name(name) == name


def fmt_secs(seconds: float) -> str:
    return f"{seconds:.1f}с"

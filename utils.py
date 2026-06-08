"""Общие утилиты, не привязанные к конкретному слою (БД, runtime, CLI)."""

import json


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

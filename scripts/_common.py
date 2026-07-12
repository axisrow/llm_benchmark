"""Общая преамбула maintenance-скриптов (issue #54, находка #10).

Раньше каждый скрипт independently повторял `argparse(--dry-run)` и держал свой
ручной `connect / try / finally / close`. Здесь — общий `add_dry_run`; за
открытие БД отвечает `db.session()` (контекстный менеджер: connect + init_schema
+ гарантированный close), который скрипты теперь и используют.
"""

import argparse


def add_dry_run(
    parser: argparse.ArgumentParser,
    *,
    help: str = "не вносить изменения, только показать план",
) -> None:
    """Добавляет стандартный флаг `--dry-run` (единый для всех скриптов)."""
    parser.add_argument("--dry-run", action="store_true", help=help)

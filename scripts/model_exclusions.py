#!/usr/bin/env python3
"""Manual denylist and unstable-status management for benchmark models."""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import (  # noqa: E402
    block_model_exclusion,
    connect,
    init_schema,
    list_model_exclusions,
    list_model_unstable,
    mark_model_unstable,
    split_model_ref,
    unblock_model_exclusion,
    unmark_model_unstable,
)


def parse_model_key(value: str) -> tuple[str, str]:
    try:
        return split_model_ref(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def cmd_list(args: argparse.Namespace) -> int:
    conn = connect()
    try:
        init_schema(conn)
        blocked = list_model_exclusions(conn, active_only=not args.all)
        unstable = list_model_unstable(conn, active_only=not args.all)
    finally:
        conn.close()

    rows = ([("blocked", r) for r in blocked]
            + [("unstable", r) for r in unstable])
    if not rows:
        print("(нет записей)")
        return 0

    print("kind\tprovider/model\tactive\treason\tupdated_at")
    for kind, row in rows:
        key = f"{row['provider']}/{row['model']}"
        print(f"{kind}\t{key}\t{row['active']}\t{row['reason']}\t{row['updated_at']}")
    return 0


def cmd_block(args: argparse.Namespace) -> int:
    provider, model = args.model_key
    conn = connect()
    try:
        init_schema(conn)
        with conn:
            row = block_model_exclusion(conn, provider, model, args.reason)
    finally:
        conn.close()

    print(f"blocked {row['provider']}/{row['model']}")
    return 0


def cmd_unblock(args: argparse.Namespace) -> int:
    provider, model = args.model_key
    conn = connect()
    try:
        init_schema(conn)
        with conn:
            row = unblock_model_exclusion(conn, provider, model)
    finally:
        conn.close()

    key = f"{provider}/{model}"
    if row is None:
        print(f"not found: {key}", file=sys.stderr)
        return 1
    print(f"unblocked {key}")
    return 0


def cmd_unstable(args: argparse.Namespace) -> int:
    provider, model = args.model_key
    conn = connect()
    try:
        init_schema(conn)
        with conn:
            row = mark_model_unstable(conn, provider, model, args.reason)
    finally:
        conn.close()

    print(f"marked unstable {row['provider']}/{row['model']}")
    return 0


def cmd_stable(args: argparse.Namespace) -> int:
    provider, model = args.model_key
    conn = connect()
    try:
        init_schema(conn)
        with conn:
            row = unmark_model_unstable(conn, provider, model)
    finally:
        conn.close()

    key = f"{provider}/{model}"
    if row is None:
        print(f"not found: {key}", file=sys.stderr)
        return 1
    print(f"unmarked unstable {key}")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Ручное управление denylist-ом моделей бенчмарка",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_list = sub.add_parser("list", help="Показать active denylist")
    p_list.add_argument("--all", action="store_true",
                        help="Показать активные и деактивированные записи")
    p_list.set_defaults(func=cmd_list)

    p_block = sub.add_parser("block", help="Исключить модель из запусков")
    p_block.add_argument("model_key", type=parse_model_key,
                         help="Модель в формате provider/model")
    p_block.add_argument("--reason", default="", help="Причина исключения")
    p_block.set_defaults(func=cmd_block)

    p_unblock = sub.add_parser("unblock", help="Вернуть модель в запуски")
    p_unblock.add_argument("model_key", type=parse_model_key,
                           help="Модель в формате provider/model")
    p_unblock.set_defaults(func=cmd_unblock)

    p_unstable = sub.add_parser(
        "unstable", help="Пометить модель как нестабильную (видна в рейтинге с бейджем)")
    p_unstable.add_argument("model_key", type=parse_model_key,
                            help="Модель в формате provider/model")
    p_unstable.add_argument("--reason", default="", help="Причина нестабильности")
    p_unstable.set_defaults(func=cmd_unstable)

    p_stable = sub.add_parser("stable", help="Снять метку нестабильности")
    p_stable.add_argument("model_key", type=parse_model_key,
                          help="Модель в формате provider/model")
    p_stable.set_defaults(func=cmd_stable)

    args = parser.parse_args()
    raise SystemExit(args.func(args))


if __name__ == "__main__":
    main()

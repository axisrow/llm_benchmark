import argparse
import sys
from pathlib import Path

from benchmark_report import run_benchmark
from dashboard_server import serve
from opencode_runtime import (
    DEFAULT_AGENT,
    DEFAULT_BASE_PORT,
    DEFAULT_COPIES,
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    install_shutdown_handlers,
)


DEFAULT_TIMEOUT = 450.0


def validate_benchmark_args(parser: argparse.ArgumentParser,
                            args: argparse.Namespace) -> None:
    if args.copies < 1:
        parser.error("--copies должно быть >= 1")
    if args.timeout < 0:
        parser.error("--timeout должно быть >= 0")
    last_port = args.base_port + args.copies - 1
    if args.base_port < 1 or last_port > 65535:
        parser.error("--base-port и --copies должны задавать порты в диапазоне 1..65535")


def main() -> None:
    if len(sys.argv) > 1 and sys.argv[1] == "serve":
        parser = argparse.ArgumentParser(
            prog="bench.py serve",
            description="Локальный тестовый веб-сервер из data/main.db",
        )
        parser.add_argument("--port", type=int, default=8000, help="Порт (default: 8000)")
        args = parser.parse_args(sys.argv[2:])
        # Перехват SIGTERM/SIGINT, чтобы при kill отработал finally в serve
        # (cleanup_index_snapshot удаляет временный docs/data/index.json).
        install_shutdown_handlers()
        serve(args.port)
        return

    parser = argparse.ArgumentParser(
        description="Автономный кодинг-агент (opencode): N параллельных копий задачи",
    )
    parser.add_argument("task", nargs="?", help="Задача для агента")
    parser.add_argument("-f", "--file", type=Path, help="Файл с задачей")
    parser.add_argument("--project", required=True,
                        help="Название проекта (используется как имя рабочей папки)")
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL,
                        help=f"Модель (default: {DEFAULT_MODEL})")
    parser.add_argument("-p", "--provider", default=DEFAULT_PROVIDER,
                        help=f"Провайдер (default: {DEFAULT_PROVIDER})")
    parser.add_argument("-a", "--agent", default=None,
                        help=f"Имя агента (по умолчанию: {DEFAULT_AGENT}, "
                             f"либо bench_planner при --planning on)")
    parser.add_argument("--planning", choices=("on", "off"), default="off",
                        help="Собирать и автоотвечать на уточняющие вопросы")
    parser.add_argument("--question-responder", choices=("recommended", "first"),
                        default="recommended", help="Стратегия автоответа")
    parser.add_argument("-n", "--copies", type=int, default=DEFAULT_COPIES,
                        help=f"Сколько параллельных копий запустить (default: {DEFAULT_COPIES})")
    parser.add_argument("--base-port", type=int, default=DEFAULT_BASE_PORT,
                        help=f"Порт первой копии; остальные +1 (default: {DEFAULT_BASE_PORT})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"Жёсткий таймаут на одну копию в секундах; "
                             f"0 = без лимита (default: {DEFAULT_TIMEOUT:.0f})")
    parser.add_argument("--force-excluded", action="store_true",
                        help="Запустить модель, даже если она в denylist-е")
    args = parser.parse_args()
    if args.agent is None:
        args.agent = "bench_planner" if args.planning == "on" else DEFAULT_AGENT

    validate_benchmark_args(parser, args)
    install_shutdown_handlers()

    try:
        raise SystemExit(run_benchmark(args))
    except ValueError as exc:
        parser.error(str(exc))
    except FileNotFoundError as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()

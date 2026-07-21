import argparse
import sys
from pathlib import Path

from benchmark_report import run_benchmark
from dashboard_server import serve
from opencode_runtime import (
    DEFAULT_AGENT,
    DEFAULT_COPIES,
    DEFAULT_MODEL,
    DEFAULT_PROVIDER,
    WORK_ROOT,
    install_shutdown_handlers,
    reap_orphan_serves,
)


DEFAULT_TIMEOUT = 450.0


def validate_benchmark_args(parser: argparse.ArgumentParser,
                            args: argparse.Namespace) -> None:
    if args.copies < 1:
        parser.error("--copies должно быть >= 1")
    if args.timeout < 0:
        parser.error("--timeout должно быть >= 0")
    output_token_max = getattr(args, "output_token_max", None)
    if output_token_max is not None and output_token_max < 1:
        parser.error("--output-token-max должно быть >= 1")
    if getattr(args, "first_action_timeout", 0.0) < 0:
        parser.error("--first-action-timeout должно быть >= 0")
    if args.base_port is not None:
        last_port = args.base_port + args.copies - 1
        if args.base_port < 1 or last_port > 65535:
            parser.error("--base-port и --copies должны задавать порты в диапазоне 1..65535")


def _reap_on_exit() -> None:
    """Подмести осиротевшие serve прошлых аварийно убитых прогонов (issue #155).

    Зовётся из ``finally`` CLI-границы уже ПОСЛЕ ``stop_servers()``: свои serve
    к этому моменту погашены штатно и сняты из marker'ов, поэтому под нож идут
    только чужие хвосты с мёртвым владельцем. Собственный same-run SIGKILL это
    НЕ лечит (``finally`` при нём не выполняется) — там работает marker+lock,
    который подметёт следующий здоровый прогон. Ошибки глушим: это гигиена на
    выходе, из-за неё код возврата прогона меняться не должен.
    """
    try:
        result = reap_orphan_serves(work_root=WORK_ROOT, apply=True)
    except Exception as exc:  # noqa: BLE001 — выход не должен падать из-за уборки
        print(f"[reap] не удалось подмести осиротевшие serve: {exc}",
              file=sys.stderr)
        return
    if result.reaped:
        print(f"[reap] погашено осиротевших serve: {len(result.reaped)}",
              file=sys.stderr)


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
                        help="Каноническое имя проекта без пробелов "
                             "(например, library_fine)")
    parser.add_argument("-m", "--model", default=DEFAULT_MODEL,
                        help=f"Модель (default: {DEFAULT_MODEL})")
    parser.add_argument("-p", "--provider", default=DEFAULT_PROVIDER,
                        help=f"Провайдер (default: {DEFAULT_PROVIDER})")
    parser.add_argument("-a", "--agent", default=None,
                        help=f"Имя агента (по умолчанию: {DEFAULT_AGENT}, "
                             f"либо plan при --planning on)")
    parser.add_argument("--planning", choices=("on", "off"), default="off",
                        help="Собирать и автоотвечать на уточняющие вопросы")
    parser.add_argument(
        "--question-responder",
        choices=("task-text", "recommended", "first"),
        default="task-text",
        help="Стратегия автоответа",
    )
    parser.add_argument(
        "--questions-only", action="store_true",
        help="Только собрать уточняющие вопросы, не отвечая и не строя план",
    )
    parser.add_argument("-n", "--copies", type=int, default=DEFAULT_COPIES,
                        help=f"Сколько параллельных копий запустить (default: {DEFAULT_COPIES})")
    parser.add_argument("--base-port", type=int, default=None,
                        help="Порт первой копии; остальные +1 (default=авто)")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"Жёсткий таймаут на одну копию в секундах; "
                             f"0 = без лимита (default: {DEFAULT_TIMEOUT:.0f})")
    parser.add_argument(
        "--output-token-max",
        type=int,
        default=None,
        help="Per-step output budget OpenCode; передаётся как "
             "OPENCODE_EXPERIMENTAL_OUTPUT_TOKEN_MAX (default: env/OpenCode)",
    )
    parser.add_argument(
        "--first-action-timeout",
        type=float,
        default=0.0,
        help="Ранний выход, если агент не начал text/tool/question за N секунд; "
             "0 = выключен (default: 0)",
    )
    parser.add_argument("--force-excluded", action="store_true",
                        help="Запустить модель, даже если она в denylist-е")
    parser.add_argument("--no-save", action="store_true", default=False,
                        help="Тестовый прогон: не записывать отчёт в БД "
                             "(проверка модели/провайдера)")
    parser.add_argument("--reap-on-exit", action="store_true", default=False,
                        help="После прогона погасить осиротевшие opencode serve "
                             "прошлых аварийно убитых прогонов (default: off)")
    args = parser.parse_args()
    if args.questions_only and args.planning != "on":
        parser.error("--questions-only требует --planning on")
    if args.agent is None:
        args.agent = "plan" if args.planning == "on" else DEFAULT_AGENT

    validate_benchmark_args(parser, args)
    install_shutdown_handlers()

    try:
        raise SystemExit(run_benchmark(args))
    except ValueError as exc:
        parser.error(str(exc))
    except FileNotFoundError as exc:
        parser.error(str(exc))
    finally:
        if args.reap_on_exit:
            _reap_on_exit()


if __name__ == "__main__":
    main()

"""Погасить осиротевшие `opencode serve` после насильственной смерти bench.py.

Dry-run используется по умолчанию: без ``--apply`` скрипт только перечисляет
подтверждённых orphan-ов и никому не шлёт сигналов. Подтверждённый orphan —
процесс, чьё владение доказано marker'ом `.bench-active.json` и чей владелец
мёртв (advisory-lock на marker свободен). Всё остальное — включая вручную
запущенный serve и serve живого параллельного bench.py — не трогается
(fail-closed). Подробности критерия — в докстринге ``opencode_reaper``.
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencode_reaper import ReapResult, ServeCandidate, reap_orphan_serves  # noqa: E402
from opencode_runtime import WORK_ROOT  # noqa: E402


def _describe(candidate: ServeCandidate) -> str:
    port = f":{candidate.port}" if candidate.port is not None else "порт неизвестен"
    return f"  pid {candidate.pid} ({port}) — {candidate.reason or 'без пояснения'}"


def _print_result(result: ReapResult, *, apply: bool) -> None:
    if result.candidates:
        header = "Погашено:" if apply else "Кандидаты на гашение (dry-run):"
        print(header)
        shown = result.reaped if apply else result.candidates
        for candidate in shown:
            print(_describe(candidate))
        if apply and len(result.reaped) < len(result.candidates):
            reaped_pids = {candidate.pid for candidate in result.reaped}
            print("Не удалось погасить:")
            for candidate in result.candidates:
                if candidate.pid not in reaped_pids:
                    print(_describe(candidate))
    else:
        print("Подтверждённых осиротевших serve не найдено.")

    for title, bucket in (
        ("Защищено (владелец жив)", result.protected_live),
        ("Неоднозначные (не трогаем)", result.ambiguous),
        ("Zombie (сигнал бессмыслен)", result.zombies),
    ):
        if bucket:
            print(f"{title}:")
            for candidate in bucket:
                print(_describe(candidate))

    if result.errors:
        print("Ошибки:")
        for error in result.errors:
            print(f"  {error}")

    print(result.summary())
    if not apply and result.candidates:
        print("Ничего не изменено. Повторите с --apply, чтобы погасить.")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="reap_orphan_serve.py",
        description="Найти и погасить осиротевшие opencode serve (issue #155)",
    )
    parser.add_argument(
        "--work-root", type=Path, default=WORK_ROOT,
        help=f"Корень рабочих папок с marker'ами (default: {WORK_ROOT})",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Действительно послать сигналы (по умолчанию — dry-run)",
    )
    args = parser.parse_args(argv)

    result = reap_orphan_serves(work_root=args.work_root, apply=args.apply)
    _print_result(result, apply=args.apply)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

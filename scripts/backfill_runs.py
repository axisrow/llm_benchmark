"""Дозапись пробелов: довести каждую модель до N успешных прогонов в каждом проекте.

Рейтинг (index_builder, issue #121) суммирует успешные run-ы (code=0) по ВСЕМ
отчётам ячейки (project, provider, model). Поэтому backfill только ДОЗАПИСЫВАЕТ
недостающее и НИЧЕГО НЕ УДАЛЯЕТ: 2 прогона сегодня + 3 позже = 5. Удаление
отчётов — исключительно ручное решение через scripts/delete_reports.py.

Этот оркестратор для каждой недобитой ячейки (project, provider, model):
  1) считает недобор need = target − cell_ok (успешные по всем отчётам ячейки),
  2) запускает bench.py с -n need (новый отчёт дозаписывается к старым),
  3) пересчитывает cell_ok из БАЗЫ,
  4) при недоборе повторяет (до --max-attempts).

denylist НЕ трогается автоматически: недобитые denylist-ячейки гоняются через
--force-excluded (по умолчанию), исход печатается в финальном вердикте, а решение
unblock/block принимает человек (см. scripts/model_exclusions.py). Важно: index
скрывает активный denylist, поэтому догнанную модель надо ещё и unblock-нуть, иначе
в рейтинг она не попадёт.

Исход считается из БАЗЫ (пересчитываем cell_ok), а не из stdout/exit-кода bench.py.

Запуск:
    python scripts/backfill_runs.py --dry-run            # матрица недобора, без запуска
    python scripts/backfill_runs.py                      # догнать всё (включая denylist)
    python scripts/backfill_runs.py --only nvidia/moonshotai/kimi-k2.6
    python scripts/backfill_runs.py --projects stock_downloader
    python scripts/backfill_runs.py --respect-denylist   # пропустить denylist-ячейки
"""

import argparse
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # корень — import db
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts — _common

import db
from _common import add_dry_run

ROOT = Path(__file__).resolve().parent.parent
PROJECTS = ("fast_sort", "hello_world", "stock_downloader")
DEFAULT_TARGET = 5
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_TIMEOUT = 450.0


def unique_pairs(conn) -> list[tuple[str, str]]:
    """Все уникальные (provider, model), встречающиеся в reports."""
    return [(r["provider"], r["model"]) for r in conn.execute(
        "SELECT DISTINCT provider, model FROM reports ORDER BY provider, model")]


def cell_ok(conn, provider: str, model: str, project: str) -> int:
    """Сколько успешных прогонов (code=0) по ВСЕМ отчётам ячейки (issue #121)."""
    return conn.execute(
        "SELECT COUNT(*) FROM runs JOIN reports ON reports.id = runs.report_id "
        "WHERE reports.provider=? AND reports.model=? AND reports.project=? "
        "AND runs.code=0",
        (provider, model, project)).fetchone()[0]


def is_denylisted(conn, provider: str, model: str) -> bool:
    """Пара в активном denylist?"""
    return db.get_model_exclusion(conn, provider, model, active_only=True) is not None


def build_matrix(conn, projects=PROJECTS, target: int = DEFAULT_TARGET) -> list[dict]:
    """Полная матрица (provider, model, project) с cell_ok / need / denylisted."""
    cells = []
    for provider, model in unique_pairs(conn):
        denied = is_denylisted(conn, provider, model)
        for project in projects:
            ok = cell_ok(conn, provider, model, project)
            cells.append({
                "provider": provider,
                "model": model,
                "project": project,
                "cell_ok": ok,
                "need": max(0, target - ok),
                "denylisted": denied,
            })
    return cells


def _parse_only(only: str | None) -> set[tuple[str, str]] | None:
    """`provider/model[,provider/model...]` -> множество пар, или None."""
    if not only:
        return None
    pairs = set()
    for token in only.split(","):
        token = token.strip()
        if not token:
            continue
        try:
            pairs.add(db.split_model_ref(token))
        except ValueError as exc:
            raise SystemExit(f"Некорректный токен {token!r}: {exc}") from exc
    return pairs or None


def select_targets(conn, *, projects=PROJECTS, target: int = DEFAULT_TARGET,
                   only: str | None = None, skip: str | None = None,
                   respect_denylist: bool = False) -> list[dict]:
    """Ячейки с недобором (need>0), с учётом фильтров --only/--skip/--projects/denylist."""
    only_pairs = _parse_only(only)
    skip_pairs = _parse_only(skip) or set()
    out = []
    for cell in build_matrix(conn, projects, target):
        if cell["need"] <= 0:
            continue
        pair = (cell["provider"], cell["model"])
        if only_pairs is not None and pair not in only_pairs:
            continue
        if pair in skip_pairs:
            continue
        if respect_denylist and cell["denylisted"]:
            continue
        out.append(cell)
    return out


def default_runner(cell: dict, *, n: int, timeout: float, base_port: int | None,
                   agent: str | None, force_excluded: bool) -> int:
    """Запускает bench.py для одной ячейки. Возвращает exit-код процесса.

    Исход считаем из БАЗЫ после прогона, не отсюда — exit-код только для лога.
    """
    cmd = [
        sys.executable, "bench.py",
        "--project", cell["project"],
        "-p", cell["provider"],
        "-m", cell["model"],
        "-n", str(n),
        "--timeout", str(timeout),
    ]
    if base_port is not None:
        cmd += ["--base-port", str(base_port)]
    if agent:
        cmd += ["-a", agent]
    if force_excluded:
        cmd.append("--force-excluded")
    print(f"    $ {' '.join(cmd[1:])}", flush=True)
    proc = subprocess.run(cmd, cwd=str(ROOT))
    return proc.returncode


def backfill_cell(conn, cell, *, target: int, max_attempts: int, timeout: float,
                  base_port: int | None, agent: str | None, force_excluded: bool,
                  runner=default_runner) -> dict:
    """Дозаписывает одну ячейку до target успешных. Возвращает outcome-структуру.

    Ничего не удаляет (issue #121): каждый заход гонит bench.py только на
    недостающее число копий, новый отчёт ложится РЯДОМ со старыми, успех
    пересчитывается суммой по всем отчётам ячейки."""
    provider, model, project = cell["provider"], cell["model"], cell["project"]
    label = f"{provider}/{model} @ {project}"
    last_code = None
    attempts = 0
    # последнее известное число успешных; стартуем из уже посчитанного в
    # build_matrix/select_targets cell_ok, дальше обновляем после каждого прогона
    # (не перечитываем БАЗУ ради того, что только что посчитали).
    final_ok = cell["cell_ok"]
    for attempts in range(1, max_attempts + 1):
        need = target - final_ok
        if need <= 0:
            break
        print(f"  [{label}] попытка {attempts}/{max_attempts}: дозапись "
              f"{need} недостающих (есть {final_ok}/{target})", flush=True)
        last_code = runner(cell, n=need, timeout=timeout, base_port=base_port,
                           agent=agent, force_excluded=force_excluded)
        final_ok = cell_ok(conn, provider, model, project)
        print(f"  [{label}] -> успешных по ячейке: {final_ok}/{target} "
              f"(exit={last_code})", flush=True)
        if final_ok >= target:
            break

    # фейлы для вердикта — по ВСЕМ runs ячейки (история дозаписывается)
    fail_codes = [r[0] for r in conn.execute(
        "SELECT runs.code FROM runs JOIN reports ON reports.id = runs.report_id "
        "WHERE reports.provider=? AND reports.model=? AND reports.project=? "
        "AND runs.code<>0 AND runs.code IS NOT NULL ORDER BY reports.started_at, runs.idx",
        (provider, model, project))]
    return {
        "provider": provider,
        "model": model,
        "project": project,
        "denylisted": cell["denylisted"],
        "target": target,
        "final_ok": final_ok,
        "success": final_ok >= target,
        "attempts": attempts,
        "fail_codes": fail_codes,
        # code=3 = rate-limit провайдера: «не модель виновата, а лимит/квота».
        "rate_limited": 3 in fail_codes,
        "last_exit": last_code,
    }


def _print_matrix(targets: list[dict]) -> None:
    print(f"Ячеек с недобором: {len(targets)}")
    print(f"{'provider':<16} {'model':<40} {'project':<18} "
          f"{'ok':>3} {'need':>4} {'deny':>5}")
    for c in targets:
        print(f"{c['provider']:<16} {c['model']:<40} {c['project']:<18} "
              f"{c['cell_ok']:>3} {c['need']:>4} "
              f"{'DENY' if c['denylisted'] else '':>5}")


def _print_verdict(outcomes: list[dict]) -> None:
    print("\n=== ВЕРДИКТ ===")
    ok_cells = [o for o in outcomes if o["success"]]
    bad_cells = [o for o in outcomes if not o["success"]]
    print(f"Догнано: {len(ok_cells)}; не догнано: {len(bad_cells)}")

    deny_ok = [o for o in ok_cells if o["denylisted"]]
    if deny_ok:
        print("\nDENYLIST-ячейки, которые ОТРАБОТАЛИ (кандидаты на unblock):")
        for o in deny_ok:
            print(f"  python scripts/model_exclusions.py unblock "
                  f"{o['provider']}/{o['model']}   # {o['project']}: "
                  f"{o['final_ok']}/{o['target']} успешных")

    if bad_cells:
        print("\nНЕ ДОГНАЛИ (оставить/уточнить блокировку):")
        for o in bad_cells:
            tags = []
            if o["denylisted"]:
                tags.append("DENY")
            if o["rate_limited"]:
                tags.append("RATE-LIMIT провайдера")
            tag = (" [" + ", ".join(tags) + "]") if tags else ""
            print(f"  {o['provider']}/{o['model']} @ {o['project']}{tag}: "
                  f"{o['final_ok']}/{o['target']} успешных, "
                  f"коды фейлов={o['fail_codes']}, попыток={o['attempts']}, "
                  f"exit={o['last_exit']}")
        rl = [o for o in bad_cells if o["rate_limited"]]
        if rl:
            print(f"\n  ⚠ {len(rl)} ячеек упёрлись в лимит провайдера (code=3) — "
                  f"это не «модель не тянет», а квота. Повторить позже.")


def run(conn, *, projects=PROJECTS, target: int = DEFAULT_TARGET,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS, timeout: float = DEFAULT_TIMEOUT,
        base_port: int | None = None, only: str | None = None,
        skip: str | None = None, agent: str | None = None,
        force_excluded: bool = True, respect_denylist: bool = False,
        dry_run: bool = False, runner=default_runner) -> int:
    """Оркестрация допрогона. Возвращает 0 если все цели достигнуты, иначе 1."""
    targets = select_targets(conn, projects=projects, target=target, only=only,
                             skip=skip, respect_denylist=respect_denylist)
    if not targets:
        print("Недобора нет — все ячейки покрыты.")
        return 0

    _print_matrix(targets)
    if dry_run:
        print("\n[dry-run] ничего не запущено.")
        return 0

    outcomes = []
    for cell in targets:
        outcomes.append(backfill_cell(
            conn, cell, target=target, max_attempts=max_attempts, timeout=timeout,
            base_port=base_port, agent=agent, force_excluded=force_excluded,
            runner=runner))

    _print_verdict(outcomes)
    return 0 if all(o["success"] for o in outcomes) else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_dry_run(parser, help="показать матрицу недобора, ничего не запускать")
    parser.add_argument("--target", type=int, default=DEFAULT_TARGET,
                        help=f"успешных прогонов на ячейку (default: {DEFAULT_TARGET})")
    parser.add_argument("--projects", nargs="+", default=list(PROJECTS),
                        help="какие проекты покрывать")
    parser.add_argument("--only", default=None,
                        help="только эти пары: provider/model[,provider/model...]")
    parser.add_argument("--skip", default=None,
                        help="исключить эти пары: provider/model[,provider/model...]")
    parser.add_argument("--max-attempts", type=int, default=DEFAULT_MAX_ATTEMPTS,
                        help=f"попыток на ячейку (default: {DEFAULT_MAX_ATTEMPTS})")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT,
                        help=f"таймаут копии для bench.py (default: {DEFAULT_TIMEOUT:.0f})")
    parser.add_argument("--base-port", type=int, default=None,
                        help="базовый порт для bench.py (default=авто)")
    parser.add_argument("-a", "--agent", default=None, help="имя агента для bench.py")
    parser.add_argument("--respect-denylist", action="store_true",
                        help="пропускать denylist-ячейки (по умолчанию гоним их через "
                             "--force-excluded)")
    args = parser.parse_args()

    with db.session() as conn:
        return run(
            conn, projects=tuple(args.projects), target=args.target,
            max_attempts=args.max_attempts, timeout=args.timeout,
            base_port=args.base_port, only=args.only, skip=args.skip,
            agent=args.agent, force_excluded=not args.respect_denylist,
            respect_denylist=args.respect_denylist, dry_run=args.dry_run)


if __name__ == "__main__":
    raise SystemExit(main())

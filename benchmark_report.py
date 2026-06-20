"""Benchmark orchestration and report persistence."""

import datetime as dt
import json
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final

from artifacts import collect_report_artifacts, cleanup_collected_artifacts
from db import (
    PROJECT_ROOT,
    connect,
    get_model_exclusion,
    safe_json_loads,
    session,
    upsert_report,
)
from opencode_runtime import (
    Usage,
    cleanup_leaked_artifacts,
    ensure_server_running,
    fmt_secs,
    locked_writer,
    prepare_work_dirs,
    probe_session,
    public_reason,
    rel_to_root,
    status_printer,
    summary_counts,
    summary_line,
    verdict,
)
from pricing import empty_pricing, format_price_display, get_pricing
from usage import (
    estimate_usage_cost,
    format_tokens,
    format_usd_cost,
    summarize_usages,
)


class _ProjectLoadError:
    pass


PROJECT_LOAD_ERROR: Final = _ProjectLoadError()
# Sentinel: raw_json проекта не распарсился — отличаем от валидного non-dict.
_RAW_JSON_INVALID: Final = object()


def load_project(project: str) -> dict | None | _ProjectLoadError:
    # Возврат None означает «проекта нет в библиотеке», и вызывающий запускает
    # ad-hoc. Поэтому ошибку БД нельзя глушить молча — иначе сбой базы выглядит
    # как «проект не найден». Логируем её ОТДЕЛЬНО (см. также #21).
    try:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT raw_json FROM projects_library WHERE name = ?",
                (project,),
            ).fetchone()
        finally:
            conn.close()
    except Exception as exc:
        print(f"warning: не удалось прочитать проект {project!r} из базы "
              f"({exc.__class__.__name__}: {exc}); продолжаю как ad-hoc",
              file=sys.stderr)
        return PROJECT_LOAD_ERROR
    if row is None:
        return None
    # safe_json_loads ловит JSONDecodeError/TypeError/RecursionError (прежний
    # ручной except RecursionError не ловил); sentinel отличает «не распарсилось»
    # (→ ошибка БД, PROJECT_LOAD_ERROR) от валидного non-dict (→ «нет», ad-hoc).
    entry = safe_json_loads(row["raw_json"], default=_RAW_JSON_INVALID)
    if entry is _RAW_JSON_INVALID:
        print(f"warning: повреждён raw_json проекта {project!r} в базе; "
              f"продолжаю как ad-hoc", file=sys.stderr)
        return PROJECT_LOAD_ERROR
    return entry if isinstance(entry, dict) else None


def ensure_model_is_allowed(provider: str, model: str,
                            force_excluded: bool = False) -> None:
    """Fail fast when the selected provider/model is in the project denylist."""
    if force_excluded:
        return

    with session() as conn:
        row = get_model_exclusion(conn, provider, model)

    if row is None:
        return

    reason = f": {row['reason']}" if row["reason"] else ""
    raise ValueError(
        f"Модель {provider}/{model} исключена из бенчмарка{reason}. "
        "Для разовой перепроверки используй --force-excluded."
    )


def save_report(report: dict, run_root: Path, artifacts: list[object] | None = None) -> None:
    rel_path = rel_to_root(run_root).as_posix() + "/report.json"
    raw_json = json.dumps(report, ensure_ascii=False, indent=2)

    with session() as conn, conn:
        upsert_report(conn, report, rel_path, raw_json, artifacts=artifacts)


def run_copy(index: int, work_dir: Path, port: int, task: str, model: str,
             provider: str, agent: str, timeout: float) -> dict:
    start = time.monotonic()
    label = f"copy {index}"
    status = status_printer(label)
    status(f"старт → {rel_to_root(work_dir)} (:{port})")

    def result(code: int, usage: Usage | None = None,
               reason: str | None = None) -> dict:
        return {
            "index": index,
            "port": port,
            "dir": str(work_dir),
            "code": code,
            "elapsed": time.monotonic() - start,
            "usage": usage,
            # Причина исхода (HTTP 429, auth/billing, timeout); для ok обычно None.
            "reason": reason,
        }

    log_path = work_dir / "run.log"
    with log_path.open("w", encoding="utf-8") as log:
        write = locked_writer(log)

        def write_status(msg: str) -> None:
            status(msg)
            write(f"[status] {msg}\n")

        try:
            server_ready = ensure_server_running(work_dir, port, write_status)
        except Exception as exc:
            write("\n--- сбой запуска сервера ---\n")
            write("".join(traceback.format_exception(exc)))
            res = result(
                2, reason=f"сбой запуска сервера: {exc.__class__.__name__}: {exc}")
            write_status(f"ошибка: {exc.__class__.__name__}: {exc} "
                         f"за {fmt_secs(res['elapsed'])}")
            return res

        if not server_ready:
            write("[не удалось поднять opencode serve]\n")
            res = result(2, reason="opencode serve не поднялся")
            write_status(f"ошибка: сервер не поднялся за {fmt_secs(res['elapsed'])}")
            return res

        try:
            session_result = probe_session(
                task=task,
                model=model,
                provider=provider,
                agent=agent,
                timeout=timeout,
                port=port,
                write=write,
            )
            rc = session_result.code
            usage = session_result.usage
            reason = session_result.reason
        except Exception as exc:
            write("\n--- сбой копии ---\n")
            write("".join(traceback.format_exception(exc)))
            res = result(2, reason=f"сбой копии: {exc.__class__.__name__}: {exc}")
            write_status(f"ошибка: {exc.__class__.__name__}: {exc} "
                         f"за {fmt_secs(res['elapsed'])}")
            return res

    res = result(rc, usage, reason=reason)
    status(f"{verdict(rc)} за {fmt_secs(res['elapsed'])} (лог: {rel_to_root(log_path)})")
    return res


def print_usage_report(results: list[dict], usage_summary: dict) -> None:
    print("--- отчёт по токенам ---")
    print(f"{'копия':<6} {'input':>12} {'output':>12} {'reasoning':>10} "
          f"{'total':>12} {'стоимость':>12}")
    for result in results:
        usage_obj = result.get("usage")
        usage = usage_obj.to_report_dict() if usage_obj else {}
        print(
            f"{result['index']:<6} "
            f"{format_tokens(usage.get('input_tokens')):>12} "
            f"{format_tokens(usage.get('output_tokens')):>12} "
            f"{format_tokens(usage.get('reasoning_tokens')):>10} "
            f"{format_tokens(usage.get('total_tokens')):>12} "
            f"{format_usd_cost(usage.get('estimated_cost_usd')):>12}"
        )
    print(f"токены всего:       {format_tokens(usage_summary.get('total_tokens'))}")
    print(f"стоимость всего:    {format_usd_cost(usage_summary.get('estimated_cost_usd'))}")


def run_benchmark(args) -> int:
    entry = load_project(args.project)
    if entry is PROJECT_LOAD_ERROR:
        entry = {}
    elif entry is None:
        print(
            f"warning: проект {args.project!r} не найден в библиотеке; "
            "запускаю ad-hoc без description/what_it_tests",
            file=sys.stderr,
        )
        entry = {}
    task = (
        args.file.read_text(encoding="utf-8") if args.file
        else args.task or entry.get("prompt")
    )
    if not task or not task.strip():
        raise ValueError(
            f"Нет задания: проект {args.project!r} не найден в базе "
            "и задача не указана в командной строке/--file"
        )
    description = entry.get("description")
    what_it_tests = entry.get("what_it_tests")
    ensure_model_is_allowed(
        args.provider,
        args.model,
        getattr(args, "force_excluded", False),
    )

    dirs = prepare_work_dirs(args.project, args.provider, args.model, args.copies)
    run_root = dirs[0].parent
    run_root_rel = rel_to_root(run_root)
    started_at = dt.datetime.now()
    print(f"Запускаю {args.copies} копий: {args.provider}/{args.model}")
    print(f"Папка прогона: {run_root_rel}")
    print(f"Задание: {task.strip()[:80]}")
    print("--- старт ---")

    run_start = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.copies + 1) as pool:
        pricing_future = pool.submit(get_pricing, args.provider, args.model)
        futures = [
            (
                pool.submit(
                    run_copy,
                    i + 1,
                    work_dir,
                    args.base_port + i,
                    task,
                    args.model,
                    args.provider,
                    args.agent,
                    args.timeout,
                ),
                i,
                work_dir,
            )
            for i, work_dir in enumerate(dirs)
        ]
        results = []
        for future, i, work_dir in futures:
            try:
                results.append(future.result())
            except Exception as exc:
                index, port = i + 1, args.base_port + i
                log_path = work_dir / "run.log"
                try:
                    with log_path.open("a", encoding="utf-8") as log:
                        log.write("\n--- сбой future ---\n")
                        log.write("".join(traceback.format_exception(exc)))
                except OSError:
                    pass
                print(f"[copy {index}] ошибка future: {exc.__class__.__name__}: {exc}",
                      flush=True)
                results.append({
                    "index": index,
                    "port": port,
                    "dir": str(work_dir),
                    "code": 2,
                    "elapsed": time.monotonic() - run_start,
                    "usage": None,
                    "reason": f"сбой future: {exc.__class__.__name__}: {exc}",
                })
        run_elapsed = time.monotonic() - run_start

    try:
        pricing = pricing_future.result()
    except Exception as exc:
        print(f"цена: не удалось получить ({exc})")
        pricing = empty_pricing()

    results.sort(key=lambda r: r["index"])
    for result in results:
        result["usage"] = estimate_usage_cost(result.get("usage"), pricing)
    usage_summary = summarize_usages([result.get("usage") for result in results])

    codes = [result["code"] for result in results]
    elapsed = [result["elapsed"] for result in results]
    summary = summary_counts(codes)
    artifact_collection = collect_report_artifacts(results)

    print("--- отчёт по времени ---")
    print(f"{'копия':<6} {'статус':<8} {'время':>8}")
    for result in results:
        print(f"{result['index']:<6} {verdict(result['code']):<8} "
              f"{fmt_secs(result['elapsed']):>8}")
    print(f"всего (wall-clock): {fmt_secs(run_elapsed)}")
    if elapsed:
        print(f"быстрее всех:       {fmt_secs(min(elapsed))}")
        print(f"медленнее всех:     {fmt_secs(max(elapsed))}")
        print(f"в среднем:          {fmt_secs(sum(elapsed) / len(elapsed))}")
    print_usage_report(results, usage_summary)
    if pricing.get("prompt_per_1m") is not None or pricing.get("note"):
        print(f"цена:               {format_price_display(pricing)}")
    print("--- сводка ---")
    print(summary_line(summary, total=args.copies))

    report = {
        "project": args.project,
        "model": args.model,
        "provider": args.provider,
        "prompt": task,
        "description": description,
        "what_it_tests": what_it_tests,
        "copies": args.copies,
        "started_at": started_at.isoformat(),
        "run_elapsed": run_elapsed,
        "summary": summary,
        "pricing": pricing,
        "usage_summary": usage_summary,
        "artifact_summary": artifact_collection.summary(),
        "runs": [
            {
                "index": result["index"],
                "port": result["port"],
                "dir": result["dir"],
                "status": verdict(result["code"]),
                "code": result["code"],
                "elapsed": result["elapsed"],
                "usage": (
                    result["usage"].to_report_dict()
                    if isinstance(result.get("usage"), Usage) else None
                ),
                # В публичный отчёт (raw_json → дашборд → GitHub Pages) идёт только
                # САНИРОВАННАЯ причина: HTTP-код + категория, без сырого тела
                # провайдера/секретов. Полный текст остаётся в приватном run.log.
                # Опциональна: старые отчёты без reason открываются как прежде.
                "reason": public_reason(result.get("reason")),
            }
            for result in results
        ],
    }
    save_report(report, run_root, artifact_collection.artifacts)
    try:
        cleanup_collected_artifacts(artifact_collection)
    except Exception as exc:
        print(f"артефакты сохранены, но очистка диска не удалась: {exc}")

    # Проверяем утечки артефактов за пределы work_dirs
    leaked = cleanup_leaked_artifacts(PROJECT_ROOT, dirs)
    if leaked:
        print("ВНИМАНИЕ: обнаружены утечки артефактов за пределы work_dir:")
        for p in leaked:
            print(f"  - {p}")

    print("Отчёт сохранён в базу: data/main.db")

    return max(codes) if codes else 0

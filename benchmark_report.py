"""Benchmark orchestration and report persistence."""

import datetime as dt
import json
import sys
import time
import traceback
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from artifacts import collect_report_artifacts, cleanup_collected_artifacts
from db import (
    connect,
    get_model_exclusion,
    init_schema,
    upsert_report,
)
from opencode_runtime import (
    Usage,
    ensure_server_running,
    fmt_secs,
    prepare_work_dirs,
    probe_session,
    rel_to_root,
    status_printer,
    verdict,
)
from pricing import empty_pricing, format_price_display, get_pricing
from usage import (
    estimate_usage_cost,
    format_tokens,
    format_usd_cost,
    summarize_usages,
)


def load_project(project: str) -> dict | None:
    try:
        conn = connect()
        try:
            row = conn.execute(
                "SELECT raw_json FROM projects_library WHERE name = ?",
                (project,),
            ).fetchone()
        finally:
            conn.close()
    except Exception:
        return None
    if row is None:
        return None
    try:
        entry = json.loads(row["raw_json"])
    except (TypeError, json.JSONDecodeError):
        return None
    return entry if isinstance(entry, dict) else None


def ensure_model_is_allowed(provider: str, model: str,
                            force_excluded: bool = False) -> None:
    """Fail fast when the selected provider/model is in the project denylist."""
    if force_excluded:
        return

    conn = connect()
    try:
        init_schema(conn)
        row = get_model_exclusion(conn, provider, model)
    finally:
        conn.close()

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

    conn = connect()
    try:
        init_schema(conn)
        with conn:
            upsert_report(conn, report, rel_path, raw_json, artifacts=artifacts)
    finally:
        conn.close()


def run_copy(index: int, work_dir: Path, port: int, task: str, model: str,
             provider: str, agent: str, timeout: float) -> dict:
    start = time.monotonic()
    label = f"copy {index}"
    status = status_printer(label)
    status(f"старт → {rel_to_root(work_dir)} (:{port})")

    def result(code: int, usage: Usage | None = None) -> dict:
        return {
            "index": index,
            "port": port,
            "dir": str(work_dir),
            "code": code,
            "elapsed": time.monotonic() - start,
            "usage": usage,
        }

    log_path = work_dir / "run.log"
    with log_path.open("w", encoding="utf-8") as log:
        log_lock = threading.Lock()

        def write(msg: str) -> None:
            with log_lock:
                log.write(msg)
                log.flush()

        def write_status(msg: str) -> None:
            status(msg)
            write(f"[status] {msg}\n")

        try:
            server_ready = ensure_server_running(work_dir, port, write_status)
        except Exception as exc:
            write("\n--- сбой запуска сервера ---\n")
            write("".join(traceback.format_exception(exc)))
            res = result(2)
            write_status(f"ошибка: {exc.__class__.__name__}: {exc} "
                         f"за {fmt_secs(res['elapsed'])}")
            return res

        if not server_ready:
            write("[не удалось поднять opencode serve]\n")
            res = result(2)
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
        except Exception as exc:
            write("\n--- сбой копии ---\n")
            write("".join(traceback.format_exception(exc)))
            res = result(2)
            status(f"ошибка: {exc.__class__.__name__}: {exc} "
                   f"за {fmt_secs(res['elapsed'])}")
            return res

    res = result(rc, usage)
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
    if entry is None:
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
    ok = codes.count(0)
    timeouts = codes.count(1)
    errors = sum(1 for code in codes if code >= 2)
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
    print(f"{ok} готово / {timeouts} таймаут / {errors} ошибка (из {args.copies})")

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
        "summary": {"ok": ok, "timeout": timeouts, "error": errors},
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
            }
            for result in results
        ],
    }
    save_report(report, run_root, artifact_collection.artifacts)
    try:
        cleanup_collected_artifacts(artifact_collection)
    except Exception as exc:
        print(f"артефакты сохранены, но очистка диска не удалась: {exc}")
    print("Отчёт сохранён в базу: data/main.db")

    return max(codes) if codes else 0

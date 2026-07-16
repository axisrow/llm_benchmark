"""Benchmark orchestration and report persistence."""

import datetime as dt
import json
import sys
import time
import traceback
from collections.abc import Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Final

from artifacts import (
    ARTIFACT_KIND_AGENT_FILE,
    collect_report_artifacts,
    cleanup_collected_artifacts,
    RunArtifact,
)
from db import (
    PROJECT_ROOT,
    connect,
    get_model_exclusion,
    safe_json_loads,
    session,
    upsert_report,
)
from library_fine_grading import (
    PROJECT_NAME as LIBRARY_FINE_PROJECT,
    RunFineGradeResult,
    grade_copy_artifacts,
    summarize_fine,
)
from lint_metrics import (
    RunLintResult,
    lint_copy_artifacts,
    summarize_lint,
    summarize_linters,
)
from opencode_runtime import (
    Usage,
    cleanup_leaked_artifacts,
    ensure_server_running,
    find_free_port_range,
    fmt_secs,
    locked_writer,
    prepare_work_dirs,
    probe_session,
    public_reason,
    rel_to_root,
    status_printer,
    stop_server,
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
from utils import is_canonical_project_name, sanitize_name


class _ProjectLoadError:
    pass


PROJECT_LOAD_ERROR: Final = _ProjectLoadError()
# Sentinel: raw_json проекта не распарсился — отличаем от валидного non-dict.
_RAW_JSON_INVALID: Final = object()
MAX_PLAN_BYTES: Final = 2 * 1024 * 1024
GLOBAL_PLAN_ROOT = Path.home() / ".local" / "share" / "opencode" / "plans"


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
        # safe_json_loads глотает причину; на редком пути порчи повторяем разбор
        # ровно ради внятного диагноза (класс+сообщение) в warning.
        detail = "не удалось распарсить"
        try:
            json.loads(row["raw_json"])
        except Exception as exc:
            detail = f"{exc.__class__.__name__}: {exc}"
        print(f"warning: повреждён raw_json проекта {project!r} в базе "
              f"({detail}); продолжаю как ad-hoc", file=sys.stderr)
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


def summarize_planning_questions(results: list[dict]) -> dict:
    """Сводка по уточняющим вопросам агента для planning-отчёта.

    Подытоги НЕ обязаны сходиться с ``questions``: вопросы режима
    ``--questions-only`` (``reply_status='captured'``, ответ не отправлялся)
    попадают в общий ``questions``/``runs_with_questions``, но ни в один из
    подитогов ниже — они по смыслу не подходят ни под recommended/fallback
    (ответа не было), ни под reply_errors (ошибки тоже нет).
    """
    questions = [q for r in results for q in r.get("questions") or []]
    return {
        "questions": len(questions),
        "runs_with_questions": sum(1 for r in results if r.get("questions")),
        "recommended_matches": sum(
            1 for q in questions
            if q.get("responder") == "recommended" and not q.get("fallback_used")
        ),
        "fallbacks_to_first": sum(1 for q in questions if q.get("fallback_used")),
        "reply_errors": sum(1 for q in questions if q.get("reply_status") == "error"),
    }


def _read_plan_snapshot(work_dir: Path, plan_ref: str | None) -> tuple[str | None,
                                                                        str | None]:
    """Resolve a native OpenCode plan relative to its git worktree and read it."""
    if not plan_ref:
        return None, None
    work_dir = work_dir.resolve()
    worktree = next(
        (path for path in (work_dir, *work_dir.parents)
         if (path / ".git").exists()),
        None,
    )
    if worktree is None:
        return plan_ref, None
    candidate = Path(plan_ref)
    global_plan_root = GLOBAL_PLAN_ROOT.resolve()
    if not candidate.is_absolute():
        # Если OpenCode считает worktree корнем файловой системы, path.relative
        # возвращает `Users/...` без ведущего `/`. Восстанавливаем такой путь
        # только когда после resolve он действительно указывает внутрь
        # доверенного каталога штатных plan-файлов.
        root_relative = (Path("/") / candidate).resolve()
        candidate = (
            root_relative
            if root_relative.is_relative_to(global_plan_root)
            else worktree / candidate
        )
    try:
        resolved = candidate.resolve(strict=True)
        in_worktree = resolved.is_relative_to(worktree)
        in_global_plans = resolved.is_relative_to(global_plan_root)
        if not in_worktree and not in_global_plans:
            return plan_ref, None
        if resolved.is_symlink() or not resolved.is_file():
            return plan_ref, None
        if resolved.stat().st_size > MAX_PLAN_BYTES:
            return plan_ref, None
        content = resolved.read_text(encoding="utf-8")
    except (OSError, UnicodeError, ValueError):
        return plan_ref, None
    if in_global_plans:
        display_path = (
            "~/.local/share/opencode/plans/"
            + resolved.relative_to(global_plan_root).as_posix()
        )
    else:
        try:
            display_path = resolved.relative_to(PROJECT_ROOT).as_posix()
        except ValueError:
            display_path = str(resolved)
    return display_path, content


def run_copy(index: int, work_dir: Path, port: int, task: str, model: str,
             provider: str, agent: str, timeout: float, planning: bool = False,
             question_responder: str = "recommended",
             questions_only: bool = False) -> dict:
    start = time.monotonic()
    label = f"copy {index}"
    status = status_printer(label)
    status(f"старт → {rel_to_root(work_dir)} (:{port})")

    def result(code: int, usage: Usage | None = None,
               reason: str | None = None,
               questions: tuple[dict, ...] = ()) -> dict:
        return {
            "index": index,
            "port": port,
            "dir": str(work_dir),
            "code": code,
            "elapsed": time.monotonic() - start,
            "usage": usage,
            # Причина исхода (HTTP 429, auth/billing, timeout); для ok обычно None.
            "reason": reason,
            "questions": list(questions),
        }

    log_path = work_dir / "run.log"
    with log_path.open("w", encoding="utf-8") as log:
        write = locked_writer(log)

        def write_status(msg: str) -> None:
            status(msg)
            write(f"[status] {msg}\n")

        # finally накрывает и запуск сервера: ensure_server_running регистрирует
        # процесс ДО проверки готовности, поэтому «serve поднялся, но не ответил
        # за SERVER_CHECK_TIMEOUT» оставляет живой процесс — его тоже надо гасить
        # точечно, а не ждать atexit в конце всего прогона (issue #139).
        try:
            try:
                server_ready = ensure_server_running(
                    work_dir, port, write_status, planning=planning)
            except Exception as exc:
                write("\n--- сбой запуска сервера ---\n")
                write("".join(traceback.format_exception(exc)))
                res = result(
                    2,
                    reason=f"сбой запуска сервера: {exc.__class__.__name__}: {exc}")
                write_status(f"ошибка: {exc.__class__.__name__}: {exc} "
                             f"за {fmt_secs(res['elapsed'])}")
                return res

            if not server_ready:
                write("[не удалось поднять opencode serve]\n")
                res = result(2, reason="opencode serve не поднялся")
                write_status(
                    f"ошибка: сервер не поднялся за {fmt_secs(res['elapsed'])}")
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
                    planning=planning,
                    question_responder=question_responder,
                    questions_only=questions_only,
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
        finally:
            # Копия отработала (успех/таймаут/ошибка/сбой запуска) — её serve
            # больше не нужен. Без этого он висел бы до конца всего прогона, пока
            # пул ждёт самую медленную копию. Гасим точечно по порту: чужие serve
            # не трогаем, atexit-путь остаётся рабочим. Неизвестный порт (serve
            # не успел зарегистрироваться) — no-op.
            try:
                stop_server(port)
            except Exception as exc:
                write(f"\n[warn] не удалось погасить serve :{port}: "
                      f"{exc.__class__.__name__}: {exc}\n")

    res = result(rc, usage, reason=reason, questions=session_result.questions)
    if planning:
        plan_path, plan_content = _read_plan_snapshot(
            work_dir, session_result.plan_path)
        res.update({
            "plan_path": plan_path,
            "plan_content": plan_content,
            "plan_elapsed": session_result.plan_elapsed,
            "build_elapsed": session_result.build_elapsed,
            "plan_usage": session_result.plan_usage,
            "build_usage": session_result.build_usage,
            "plan_completed": session_result.plan_completed,
        })
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


def _resolve_task(args) -> tuple[str, str | None, str | None]:
    """Грузит проект, выбирает задание (файл/CLI/библиотека) и валидирует.

    Возвращает (task, description, what_it_tests). Бросает ValueError, если задания
    нет; ensure_model_is_allowed бросает при denylist-паре (если не --force-excluded).
    """
    if not is_canonical_project_name(args.project):
        suggested = sanitize_name(args.project)
        raise ValueError(
            f"Неканоническое имя проекта {args.project!r}; "
            f"используйте {suggested!r}"
        )

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
    ensure_model_is_allowed(
        args.provider, args.model, getattr(args, "force_excluded", False),
    )
    return task, entry.get("description"), entry.get("what_it_tests")


def _announce_run(args, task: str) -> tuple[list[Path], Path, dt.datetime]:
    """Создаёт work_dirs копий и печатает шапку прогона.

    Возвращает (dirs, run_root, started_at)."""
    dirs = prepare_work_dirs(args.project, args.provider, args.model, args.copies)
    run_root = dirs[0].parent
    # started_at снимаем ДО печати шапки — как в исходном run_benchmark (отметка
    # старта прогона, а не конца баннера); сохраняет байт-в-байт поведение.
    started_at = dt.datetime.now()
    print(f"Запускаю {args.copies} копий: {args.provider}/{args.model}")
    print(f"Папка прогона: {rel_to_root(run_root)}")
    print(f"Задание: {task.strip()[:80]}")
    print("--- старт ---")
    return dirs, run_root, started_at


def _run_copies(args, dirs: list[Path], task: str) -> tuple[list[dict], float, dict]:
    """Параллельно гоняет N копий + фоновую задачу цены в одном пуле.

    Возвращает (results, run_elapsed, pricing). Сбой future одной копии не валит
    прогон — превращается в строку результата с code=2. Цена-future доезжает к
    выходу из пула (shutdown ждёт все futures), её результат берём после.
    """
    run_start = time.monotonic()
    # Найти базовый порт один раз, если не задан явно
    base_port = args.base_port
    if base_port is None:
        base_port = find_free_port_range(args.copies)

    with ThreadPoolExecutor(max_workers=args.copies + 1) as pool:
        pricing_future = pool.submit(get_pricing, args.provider, args.model)
        futures = [
            (
                pool.submit(
                    run_copy,
                    i + 1,
                    work_dir,
                    base_port + i,
                    task,
                    args.model,
                    args.provider,
                    args.agent,
                    args.timeout,
                    args.planning == "on",
                    args.question_responder,
                    getattr(args, "questions_only", False),
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
                index, port = i + 1, base_port + i
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
                    "questions": [],
                })
        run_elapsed = time.monotonic() - run_start

    try:
        pricing = pricing_future.result()
    except Exception as exc:
        print(f"цена: не удалось получить ({exc})")
        pricing = empty_pricing()
    return results, run_elapsed, pricing


def _print_report(results: list[dict], run_elapsed: float, usage_summary: dict,
                  pricing: dict, summary: dict, copies: int) -> None:
    """Печатает таблицу времени по копиям, usage/цену и финальную сводку."""
    elapsed = [result["elapsed"] for result in results]
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
    print(summary_line(summary, total=copies))
    # issue #142: no_artifact живёт вне RUN_CODES (summary_line его не знает) —
    # печатаем отдельной строкой и только когда есть о чём сказать.
    if summary.get("no_artifact"):
        print(f"без артефакта:      {summary['no_artifact']} "
              f"(code=0, но модель не сохранила файл)")


def _build_report(args, task: str, description: str | None,
                  what_it_tests: str | None, started_at: dt.datetime,
                  run_elapsed: float, summary: dict, pricing: dict,
                  usage_summary: dict, artifact_collection, results: list[dict]) -> dict:
    """Собирает дословный report-dict (raw_json → дашборд)."""
    planning = args.planning == "on"
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
        # issue #100: сводка Ruff-метрики по копиям (checked/na/unavailable,
        # total_errors, avg_errors). Поле опционально для старых отчётов, но при
        # штатном прогоне есть всегда — даже если Ruff недоступен (unavailable)
        # или во всех копиях нет .py (na). summarize_lint игнорирует неуспешные.
        # Оставлено производным от линтера 'ruff' ради байт-в-байт совместимости
        # индекса/дашборда со старыми отчётами.
        "ruff_summary": summarize_lint(results),
        # issue #101: сводка по КАЖДОМУ линтеру отдельно (ruff/tidy/jq, ...) с
        # раздельными счётчиками — {имя → {checked,na,unavailable,total_errors,
        # avg_errors}}. Инструмент попадает в сводку, только если встретился хотя
        # бы в одной копии. ruff_summary выше остаётся синонимом lint_summary.ruff.
        "lint_summary": summarize_linters(results),
        # issue #126: сводка функциональной оценки «X из 34» по копиям
        # ({checked,na,unavailable,passed,total}). Ключ есть ТОЛЬКО у прогонов
        # проекта library_fine — raw_json остальных проектов байт-в-байт прежний.
        **({"fine_summary": summarize_fine(results)}
           if args.project == LIBRARY_FINE_PROJECT else {}),
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
                # issue #100: Ruff-метрика копии. Только у успешно завершившихся
                # (code==0); у неуспешных ключа НЕТ — они и так исключены из сводки.
                # Опциональна для обратной совместимости со старыми raw_json.
                **({"ruff": {"status": lint.status, "errors": lint.errors}}
                   if (lint := result.get("lint")) is not None else {}),
                # issue #101: результаты ВСЕХ применимых линтеров копии
                # {имя → {status, errors}}. Ключ есть только у успешных копий с
                # хотя бы одним линтуемым файлом; для .py он содержит и 'ruff'
                # (тот же результат, что в runs[].ruff). Опционален для старых
                # raw_json — дашборд читает его при наличии.
                **({"linters": {
                    name: {"status": r.status, "errors": r.errors}
                    for name, r in linters.items()
                }} if (linters := result.get("linters")) else {}),
                # issue #126: функциональная оценка копии library_fine
                # {status, passed, total, autonomous}. Ключ есть только у
                # успешных копий library_fine-прогона; опционален для старых
                # raw_json.
                **({"fine": {"status": fine.status, "passed": fine.passed,
                             "total": fine.total,
                             "autonomous": fine.autonomous,
                             "errors": list(fine.errors)}}
                   if (fine := result.get("fine")) is not None else {}),
                # В planning-отчёте runs[].questions есть ВСЕГДА: пустой массив,
                # если вопросов не было (не отсутствующий ключ). Это и для
                # дашборда предсказуемее, и сводка по пустому прогону честная
                # (questions==0, runs_with_questions==0). При planning=off ключа
                # НЕТ — coding-отчёты остаются байт-в-байт прежними.
                **({
                    "questions": result.get("questions") or [],
                    "plan": {
                        "path": result.get("plan_path"),
                        "content": result.get("plan_content"),
                    },
                    "phases": {
                        "plan": {
                            "status": (
                                "captured" if getattr(args, "questions_only", False)
                                else "completed" if result.get("plan_completed")
                                else verdict(result["code"])
                            ),
                            "elapsed": result.get("plan_elapsed"),
                            "usage": (
                                result["plan_usage"].to_report_dict()
                                if isinstance(result.get("plan_usage"), Usage)
                                else None
                            ),
                        },
                        "build": {
                            "status": (
                                verdict(result["code"])
                                if result.get("plan_completed")
                                and not getattr(args, "questions_only", False)
                                else "not_started"
                            ),
                            "elapsed": result.get("build_elapsed"),
                            "usage": (
                                result["build_usage"].to_report_dict()
                                if isinstance(result.get("build_usage"), Usage)
                                else None
                            ),
                        },
                    },
                } if planning else {}),
            }
            for result in results
        ],
    }
    if planning:
        report["planning"] = {
            "enabled": True,
            "agent": args.agent,
            "responder": args.question_responder,
            **({"questions_only": True}
               if getattr(args, "questions_only", False) else {}),
        }
        report["planning_summary"] = summarize_planning_questions(results)
    return report


def _summarize(results: list[dict], pricing: dict,
               project: str | None = None) -> tuple[dict, dict, object]:
    """Сводит результаты копий: сортировка, цена per-run, агрегаты, артефакты.

    Мутирует `results` на месте (сортировка по index + проставление usage с ценой);
    возвращает (usage_summary, summary, artifact_collection).
    """
    results.sort(key=lambda r: r["index"])
    for result in results:
        result["usage"] = estimate_usage_cost(result.get("usage"), pricing)
        result["plan_usage"] = estimate_usage_cost(
            result.get("plan_usage"), pricing)
        result["build_usage"] = estimate_usage_cost(
            result.get("build_usage"), pricing)
    usage_summary = summarize_usages([result.get("usage") for result in results])
    summary = summary_counts([result["code"] for result in results])
    artifact_collection = collect_report_artifacts(results)
    # issue #142: копии, дошедшие до code==0, но не оставившие ни одного файла
    # модели (в артефактах только run.log, который пишет сам бенчмарк) — не
    # успех: результата нет. Это НЕ новый код исхода (RUN_CODES не трогаем —
    # code==0 честно значит «агент не упал», и check_models/verdict живут на той
    # же таксономии), а отдельный счётчик поверх сводки. Успех для рейтинга
    # считает index_builder — по тем же артефактам, но уже из базы.
    summary["no_artifact"] = _count_copies_without_agent_file(
        results, artifact_collection)
    # issue #100/#101: lint-метрики считаются ПО собранным артефактам ДО cleanup
    # (_finalize зовёт cleanup_collected_artifacts уже после save_report).
    # Линтерам нужны исходники, поэтому метрика физически здесь; логически она
    # независима от cleanup — любой её сбой гасится в 'unavailable' и не валит
    # прогон. Только успешно завершившиеся копии (code==0) получают оценку.
    # lint_copy_artifacts отдаёт dict имя→RunLintResult для КАЖДОГО линтера реестра
    # (checked/na/unavailable): так счётчики na верны по всем инструментам, и для
    # ruff сохраняется поведение #100 (включая na для копии без .py). Неуспешным
    # копиям — пустой dict / lint=None.
    # code по run_idx: gate «только code==0» нужен функциональной оценке ДО
    # исполнения недоверенного JS (см. ниже) — строим один раз из results.
    code_by_idx: dict[int, int] = {
        int(r["index"]): int(r["code"]) for r in results}
    linters_by_idx: dict[int, dict[str, RunLintResult]] = {}
    fine_by_idx: dict[int, RunFineGradeResult] = {}
    for run_idx, group in _group_artifacts_by_idx(artifact_collection.artifacts).items():
        linters_by_idx[run_idx] = lint_copy_artifacts(group)
        # issue #126: функциональная оценка «X из 34» — ТОЛЬКО для проекта
        # library_fine (HTML прочих проектов оценивать по матрице бессмысленно)
        # и ТОЛЬКО успешных копий (code==0). gate ДО вызова: grade_copy_artifacts
        # исполняет недоверенный JS модели в subprocess — не запускать его для
        # фейловых копий, результат которых всё равно отбрасывается (безопасность
        # + wasted work). Линтеры выше безопасны (trusted tools), для них gate
        # не нужен.
        if project == LIBRARY_FINE_PROJECT and code_by_idx.get(run_idx) == 0:
            fine_by_idx[run_idx] = grade_copy_artifacts(group)
    for result in results:
        idx = result.get("index")
        if result.get("code") == 0 and idx in linters_by_idx:
            per_copy = linters_by_idx[idx]
            result["linters"] = per_copy
            # issue #100 совместимость: runs[].ruff и ruff_summary остаются
            # производными от линтера 'ruff' — старый Ruff-путь без изменений.
            result["lint"] = per_copy.get("ruff")
            result["fine"] = fine_by_idx.get(idx)
        else:
            result["linters"] = {}
            result["lint"] = None
            result["fine"] = None
    return usage_summary, summary, artifact_collection


def _count_copies_without_agent_file(results: list[dict],
                                     artifact_collection) -> int:
    """Сколько копий с code==0 не сохранили ни одного agent_file (issue #142).

    Считается по уже собранной коллекции артефактов — тем же данным, что уйдут в
    базу, поэтому счётчик сводки и success_rate индекса не разъезжаются.
    """
    with_agent_file = {
        int(artifact.run_idx)
        for artifact in artifact_collection.artifacts
        if artifact.kind == ARTIFACT_KIND_AGENT_FILE
    }
    return sum(1 for result in results
               if result.get("code") == 0
               and result.get("index") not in with_agent_file)


def _group_artifacts_by_idx(
    artifacts: Sequence[RunArtifact],
) -> dict[int, list[RunArtifact]]:
    """Группирует артефакты по run_idx (одна копия → её .py/.лог-файлы)."""
    grouped: dict[int, list[RunArtifact]] = {}
    for artifact in artifacts:
        grouped.setdefault(int(artifact.run_idx), []).append(artifact)
    return grouped


def _finalize(report: dict, run_root: Path, dirs: list[Path],
              artifact_collection, no_save: bool = False) -> None:
    """Пишет отчёт в базу, чистит диск, проверяет утечки артефактов за work_dirs.

    Файлы копий удаляются только после успешного возврата из save_report: его
    транзакционный context manager уже гарантирует commit либо исключение с
    rollback. Исключение записи не перехватываем — CLI обязан завершиться ошибкой,
    а файлы остаются на диске, потому что cleanup ещё не был вызван.

    issue #140: при no_save=True (тестовый прогон --no-save) запись в базу
    пропускается целиком — ни отчёта, ни runs, ни артефактов, иначе прогон
    исказил бы рейтинг (#121: индекс суммирует по всем отчётам ячейки). Диск при
    этом чистится как обычно: артефакты никуда не сохранены, orphan-хвосты в
    data/result/ оставлять нельзя.
    """
    if not no_save:
        save_report(report, run_root, artifact_collection.artifacts)

    try:
        cleanup_collected_artifacts(artifact_collection)
    except Exception as exc:
        # Запись в базу уже прошла (либо её и не было при --no-save) — сбой
        # удаления файлов не должен валить прогон, но пользователь обязан о нём
        # знать (с путём).
        print(f"warning: очистка диска не удалась "
              f"({exc.__class__.__name__}: {exc}); пути: " +
              ", ".join(str(rel_to_root(d)) for d in dirs), file=sys.stderr)

    # Проверяем утечки артефактов за пределы work_dirs
    try:
        leaked = cleanup_leaked_artifacts(PROJECT_ROOT, dirs)
    except Exception as exc:
        # issue #121 (E1): отчёт уже в базе — сбой проверки утечек не должен
        # валить прогон, но пользователь обязан о нём знать.
        print(f"warning: проверка утечек артефактов не удалась "
              f"({exc.__class__.__name__}: {exc})", file=sys.stderr)
        leaked = []
    if leaked:
        print("ВНИМАНИЕ: обнаружены утечки артефактов за пределы work_dir:")
        for p in leaked:
            print(f"  - {p}")

    if no_save:
        print("Тестовый прогон (--no-save): отчёт не сохранён в базу")
    else:
        print("Отчёт сохранён в базу: data/main.db")


def run_benchmark(args) -> int:
    """Оркестратор прогона: подготовка → запуск копий → сведение → запись.

    Тонкий: каждая фаза — отдельная функция выше. Возвращает max(code) по копиям
    (худший исход) — это итоговый exit-код бенчмарка.
    """
    task, description, what_it_tests = _resolve_task(args)
    dirs, run_root, started_at = _announce_run(args, task)
    results, run_elapsed, pricing = _run_copies(args, dirs, task)
    usage_summary, summary, artifact_collection = _summarize(
        results, pricing, args.project)

    _print_report(results, run_elapsed, usage_summary, pricing, summary, args.copies)
    report = _build_report(args, task, description, what_it_tests, started_at,
                           run_elapsed, summary, pricing, usage_summary,
                           artifact_collection, results)
    # issue #140: --no-save может отсутствовать у вызывающих без этого флага.
    _finalize(report, run_root, dirs, artifact_collection,
              no_save=bool(getattr(args, "no_save", False)))

    return max((result["code"] for result in results), default=0)

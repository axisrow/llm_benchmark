"""Build docs/data/index.json from the benchmark SQLite database."""

import json
import math
import sys
from datetime import datetime

from db import (
    PROJECT_ROOT,
    active_exclusions_map,
    active_unstable_map,
    model_key,
    session,
)
from opencode_runtime import RUN_CODES
from utils import json_loads_or
from pricing import get_pricing


def load_library(conn):
    try:
        rows = conn.execute(
            "SELECT name, description, prompt, what_it_tests "
            "FROM projects_library"
        ).fetchall()
    except Exception as exc:
        # Без следа пустая библиотека неотличима от «таблица недоступна».
        print(f"Не удалось прочитать projects_library из базы: {exc}",
              file=sys.stderr)
        return {}
    library = {}
    for row in rows:
        what_raw = row["what_it_tests"]
        if what_raw:
            what = json_loads_or(what_raw, default=[])
            if not isinstance(what, list):
                print(f"Повреждён what_it_tests проекта {row['name']!r}: "
                      f"ожидается list, получен {type(what).__name__}",
                      file=sys.stderr)
                what = []
        else:
            what = []
        library[row["name"]] = {
            "description": row["description"],
            "prompt": row["prompt"],
            "what_it_tests": what,
        }
    return library


def load_reports(conn):
    # Грузим и декодируем все отчёты один раз; denylist применяется уже в памяти
    # (см. build_index), чтобы не декодировать одни и те же ряды дважды.
    # Берём и id (issue #93): он нужен, чтобы сопоставить in-memory отчёт с его
    # question_reviews и проставить review_key (составной ключ для API). id живёт
    # в служебном ключе "_report_id" — НЕ в raw_json, который обязан остаться
    # байт-в-байт неизменным (фронтенд его не использует).
    rows = conn.execute(
        "SELECT id, rel_path, raw_json FROM reports "
        # Вторичный ключ (provider, model, rel_path) делает порядок ties по
        # равному started_at детерминированным: иначе SQLite отдаёт их в порядке
        # rowid, который плывёт между VACUUM/реимпортом и ломает байт-в-байт
        # воспроизводимость index.json (см. CLAUDE.md).
        "ORDER BY started_at DESC, provider ASC, model ASC, rel_path ASC"
    ).fetchall()

    reports = []
    for row in rows:
        report = json_loads_or(row["raw_json"])
        if report is None:
            print(f"Пропускаю повреждённый ряд reports ({row['rel_path']})",
                  file=sys.stderr)
            continue

        report["_report_id"] = row["id"]
        report["path"] = f"../{row['rel_path']}"
        try:
            started = datetime.fromisoformat(report["started_at"])
            report["started_at_display"] = started.strftime("%Y-%m-%d %H:%M:%S")
        except Exception as exc:
            print(f"Не удалось разобрать started_at в отчёте "
                  f"({row['rel_path']}): {exc}", file=sys.stderr)
            report["started_at_display"] = report.get("started_at", "")

        pricing = report.get("pricing")
        if not pricing or (pricing.get("prompt_per_1m") is None and not pricing.get("note")):
            report["pricing"] = get_pricing(
                report.get("provider", ""),
                report.get("model", ""),
                refresh=False,
            )

        reports.append(report)
    return reports


# Статусы прогона, агрегируемые в любой сводке (на проект / на всю базу).
# Таксономия — из единого источника RUN_CODES (opencode_runtime), не хардкод.
SUMMARY_KEYS = tuple(key for _code, (key, _label) in RUN_CODES.items())


def _empty_summary() -> dict:
    return {key: 0 for key in SUMMARY_KEYS}


def _accumulate_summary(target: dict, report) -> None:
    """Складывает статусы одного отчёта в накопитель `target`."""
    summary = report.get("summary") or {}
    for key in SUMMARY_KEYS:
        target[key] += summary.get(key, 0)


def _model_key(report) -> tuple[str, str]:
    return (report.get("provider", ""), report.get("model", ""))


def _count_runs(reports) -> int:
    return sum(len(report.get("runs") or []) for report in reports)


def _count_models(reports) -> int:
    return len({_model_key(r) for r in reports
                if r.get("provider") and r.get("model")})


def group_by_project(reports, library):
    groups: dict[str, dict] = {}
    for report in reports:
        name = report.get("project", "")
        group = groups.get(name)
        if group is None:
            entry = library.get(name, {})
            group = groups[name] = {
                "name": name,
                "description": entry.get("description") or report.get("description"),
                "prompt": entry.get("prompt") or report.get("prompt"),
                "what_it_tests": entry.get("what_it_tests") or report.get("what_it_tests") or [],
                "reports": [],
            }
        group["reports"].append(report)

    for group in groups.values():
        summary = _empty_summary()
        run_count = 0
        # issue #121: сводка/run_count/метрики — по ВСЕМ отчётам проекта
        # (история дозаписывается, latest-wins больше нет). ruff — историч.
        # отдельное поле (#100); linters — сводка по каждому инструменту (#101),
        # с раздельными счётчиками; fine — функциональная оценка library_fine
        # (#126, есть только у проектов с fine_summary в отчётах).
        ruff = dict.fromkeys(_LINT_KEYS, 0)
        linters: dict[str, dict] = {}
        fine = dict.fromkeys(_FINE_KEYS, 0)
        for report in group["reports"]:
            _accumulate_summary(summary, report)
            run_count += len(report.get("runs") or [])
            _accumulate_counters(ruff, report.get("ruff_summary"), _LINT_KEYS)
            _accumulate_linters(linters, report)
            _accumulate_counters(fine, report.get("fine_summary"), _FINE_KEYS)
        group["summary"] = summary
        group["run_count"] = run_count
        group["report_count"] = len(group["reports"])
        group["model_count"] = _count_models(group["reports"])
        # avg_errors пересчитываем из накопленных total_errors/checked проекта:
        # среднее latest-сводок моделей было бы нерепрезентативно (веса разные).
        ruff["avg_errors"] = (
            round(ruff["total_errors"] / ruff["checked"], 2) if ruff["checked"] else None
        )
        group["ruff_summary"] = ruff
        for tool_summary in linters.values():
            tool_summary["avg_errors"] = (
                round(tool_summary["total_errors"] / tool_summary["checked"], 2)
                if tool_summary["checked"] else None
            )
        group["lint_summary"] = linters
        if any(isinstance(r.get("fine_summary"), dict) for r in group["reports"]):
            group["fine_summary"] = fine

    return sorted(groups.values(),
                  key=lambda g: (-g["model_count"], g["name"]))


_LINT_KEYS = ("checked", "na", "unavailable", "total_errors")
_FINE_KEYS = ("checked", "na", "unavailable", "parse_error",
              "autonomy_errors", "passed", "total")


def _accumulate_counters(target: dict, source, keys: tuple[str, ...]) -> None:
    """Складывает целочисленные счётчики сводки одного отчёта в накопитель.

    Сводки считает benchmark_report при прогоне (#100 ruff_summary, #126
    fine_summary). Старые отчёты без сводки (source не dict) пропускаются —
    проект без метрики просто получит нули, не ломая сборку индекса.
    """
    if not isinstance(source, dict):
        return
    for key in keys:
        target[key] += int(source.get(key, 0) or 0)


def _accumulate_linters(target: dict, report) -> None:
    """Складывает lint_summary одного отчёта в накопитель `target` по инструментам.

    lint_summary отчёта — {имя_линтера → {checked,na,unavailable,total_errors}}
    (её считает benchmark_report при прогоне, #101). Каждый инструмент копится в
    свой под-словарь target[name] с раздельными счётчиками — diagnostics разных
    линтеров не смешиваются. Старые отчёты без lint_summary (до #101) пропускаются
    — проект без метрики просто не получит эти ключи, не ломая сборку индекса.
    """
    lint_summary = report.get("lint_summary") or {}
    for name, tool in lint_summary.items():
        if not isinstance(tool, dict):
            continue
        bucket = target.setdefault(name, dict.fromkeys(_LINT_KEYS, 0))
        _accumulate_counters(bucket, tool, _LINT_KEYS)


def _is_number(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def _avg(values):
    return sum(values) / len(values) if values else None


def _sum_numbers(values):
    numbers = [v for v in values if _is_number(v)]
    return sum(numbers) if numbers else None


def build_dashboard_summary(all_reports, excluded_reports):
    """Сводка по всей базе, включая denylist-модели (фронт читает её готовой)."""
    project_names = {r.get("project", "") for r in all_reports if r.get("project")}

    status_summary = _empty_summary()
    for report in all_reports:
        _accumulate_summary(status_summary, report)

    return {
        "project_count": len(project_names),
        "model_count": _count_models(all_reports),
        "report_count": len(all_reports),
        "run_count": _count_runs(all_reports),
        **status_summary,
        "total_tokens": _sum_numbers(
            (r.get("usage_summary") or {}).get("total_tokens") for r in all_reports
        ),
        "estimated_cost_usd": _sum_numbers(
            (r.get("usage_summary") or {}).get("estimated_cost_usd") for r in all_reports
        ),
        "excluded_report_count": len(excluded_reports),
        "excluded_run_count": _count_runs(excluded_reports),
    }


def _group_by_project_model(reports) -> dict:
    """Группирует ВСЕ отчёты по ячейке (project, provider, model).

    reports приходят из load_reports уже ORDER BY started_at DESC, поэтому первый
    отчёт каждой ячейки — самый свежий (записи без project/provider/model — мимо)."""
    cells: dict[tuple[str, str, str], list] = {}
    for report in reports:
        project = report.get("project", "")
        provider = report.get("provider", "")
        model = report.get("model", "")
        if not project or not provider or not model:
            continue
        cells.setdefault((project, provider, model), []).append(report)
    return cells


def _new_model_item(provider: str, model: str) -> dict:
    """Пустой аккумулятор метрик для (provider, model)."""
    return {
        "provider": provider,
        "model": model,
        "projects": set(),
        "successful_run_count": 0,
        "total_run_count": 0,
        "elapsed": [],
        "tokens": [],
        "costs": [],
        "latest_report": None,
        "unstable_projects": set(),
    }


def _accumulate_runs(item: dict, report) -> None:
    """Добавляет успешные (code==0) прогоны report в метрики item."""
    for run in report.get("runs") or []:
        code = run.get("code")
        if code != 0:
            continue

        item["successful_run_count"] += 1
        elapsed = run.get("elapsed")
        if _is_number(elapsed):
            item["elapsed"].append(elapsed)

        usage = run.get("usage") or {}
        tokens = usage.get("total_tokens")
        if _is_number(tokens):
            item["tokens"].append(tokens)
        cost = usage.get("estimated_cost_usd")
        if _is_number(cost):
            item["costs"].append(cost)


def _aggregate_by_model(cells: dict) -> dict:
    """Сводит ВСЕ отчёты каждой ячейки в аккумулятор по (provider, model).

    issue #121: фейлы модель не исключают — метрики копятся по всем успешным
    (code==0) прогонам всех отчётов; total_run_count считает все записанные runs
    (для success_rate); проект с хотя бы одним фейлом попадает в unstable_projects."""
    by_model: dict[tuple[str, str], dict] = {}
    for (project, provider, model), cell_reports in cells.items():
        key = (provider, model)
        item = by_model.setdefault(key, _new_model_item(provider, model))

        item["projects"].add(project)
        # Ячейки идут в порядке убывания started_at их свежайшего отчёта, внутри
        # ячейки первый отчёт — самый свежий: первый отчёт первой ячейки модели
        # и есть её самый свежий отчёт вообще.
        if item["latest_report"] is None:
            item["latest_report"] = cell_reports[0]

        for report in cell_reports:
            runs = report.get("runs") or []
            item["total_run_count"] += len(runs)
            if any(run.get("code") != 0 for run in runs):
                item["unstable_projects"].add(project)
            _accumulate_runs(item, report)
    return by_model


def _ranking_row(provider: str, model: str, item: dict, unstable_map: dict) -> dict:
    """Собирает одну строку рейтинга из аккумулятора item (без rank — он позже)."""
    is_unstable = (provider, model) in unstable_map
    latest_report = item["latest_report"] or {}
    total = item["total_run_count"]
    return {
        "provider": item["provider"],
        "model": item["model"],
        "key": model_key(item["provider"], item["model"]),
        "projects": sorted(item["projects"]),
        "project_count": len(item["projects"]),
        "successful_run_count": item["successful_run_count"],
        "total_run_count": total,
        # детерминировано (целочисленное деление IEEE + round), сборка индекса
        # остаётся байт-в-байт воспроизводимой
        "success_rate": (round(item["successful_run_count"] / total, 4)
                         if total else None),
        "avg_elapsed": _avg(item["elapsed"]),
        "avg_tokens": _avg(item["tokens"]),
        "avg_cost_usd": _avg(item["costs"]),
        "latest_started_at": latest_report.get("started_at", ""),
        "latest_started_at_display": latest_report.get(
            "started_at_display",
            latest_report.get("started_at", ""),
        ),
        "status": "unstable" if is_unstable else "stable",
        "unstable_projects": sorted(item["unstable_projects"]),
        "unstable_reason": unstable_map.get((provider, model), ""),
    }


def _sort_and_rank(ranking: list) -> list:
    """Сортирует рейтинг (avg_elapsed → tokens → cost → provider → model; None — в
    конец через math.inf) и проставляет 1-based rank. Мутирует и возвращает ranking."""
    def sort_value(value):
        return value if _is_number(value) else math.inf

    ranking.sort(key=lambda row: (
        sort_value(row["avg_elapsed"]),
        sort_value(row["avg_tokens"]),
        sort_value(row["avg_cost_usd"]),
        row["provider"],
        row["model"],
    ))
    for idx, row in enumerate(ranking, start=1):
        row["rank"] = idx
    return ranking


def build_model_ranking(reports, unstable_map=None):
    """Рейтинг моделей: группировка ВСЕХ отчётов по ячейкам → агрегат по
    (provider, model) → фильтр (только нулевой успех) → строки → сортировка+rank.

    issue #121: фейлы модель НЕ исключают — показывается success_rate; скрывается
    лишь модель без единого успешного прогона (нечего показать). unstable_map:
    {(provider, model): reason} — ручная пометка; бейдж чисто визуальный
    (status/unstable_reason), на метрики не влияет."""
    unstable_map = unstable_map or {}
    cells = _group_by_project_model(reports)
    by_model = _aggregate_by_model(cells)

    ranking = [
        _ranking_row(provider, model, item, unstable_map)
        for (provider, model), item in by_model.items()
        if item["successful_run_count"] > 0
    ]

    return _sort_and_rank(ranking)


def load_question_reviews(conn) -> dict:
    """Все оценки одним запросом: {(report_id, run_idx, attempt_idx, request_id,
    question_idx): verdict}. Пустой dict, если оценок нет."""
    rows = conn.execute(
        "SELECT report_id, run_idx, attempt_idx, request_id, question_idx, "
        "verdict FROM question_reviews"
    ).fetchall()
    return {
        (r["report_id"], r["run_idx"], r["attempt_idx"], r["request_id"],
         r["question_idx"]): r["verdict"]
        for r in rows
    }


def _review_key(report_id, run_idx, attempt_idx, request_id, question_idx) -> dict:
    """Составной ключ вопроса для API-вызовов разметки (PUT/DELETE)."""
    return {
        "report_id": report_id,
        "run_idx": run_idx,
        "attempt_idx": attempt_idx,
        "request_id": request_id,
        "question_idx": question_idx,
    }


def _review_summary(total: int, reviewed: int, useful: int, unnecessary: int) -> dict:
    """Агрегаты разметки одного planning-отчёта.

    useful_percent = useful/reviewed*100 либо null при reviewed=0 (неоценённые
    вопросы не ухудшают метрику); coverage_percent = reviewed/total*100 либо 0
    при total=0 (делить на ноль нельзя).
    """
    return {
        "total": total,
        "reviewed": reviewed,
        "useful": useful,
        "unnecessary": unnecessary,
        "useful_percent": round(useful / reviewed * 100, 2) if reviewed else None,
        "coverage_percent": round(reviewed / total * 100, 2) if total else 0.0,
    }


def enrich_reviews(reports: list, reviews_map: dict) -> None:
    """issue #93: добавляет review_key/review_verdict в questions и review_summary
    в planning-отчёты.

    Мутирует ТОЛЬКО in-memory reports (которые строит load_reports из raw_json);
    reports.raw_json в базе не трогается (байт-в-байт). Для каждого вопроса
    planning-отчёта проставляется review_key (нужен фронтенду для PUT/DELETE даже
    на неоценённом вопросе); review_verdict — только оценённому. review_summary
    — только planning-отчётам. Coding-отчёты (без planning) обходятся без всего.
    """
    for report in reports:
        # Служебный _report_id ставится в load_reports всем отчётам; убираем,
        # чтобы он не утёк в готовый index.json (там только публичные поля).
        report_id = report.pop("_report_id", None)
        # Coding-отчёты (нет planning) — кнопок/сводки нет.
        if not report.get("planning"):
            continue
        total = reviewed = useful = unnecessary = 0
        for run in report.get("runs") or []:
            for question in run.get("questions") or []:
                if not isinstance(question, dict):
                    continue
                total += 1
                attempt = question.get("attempt_idx", 1)
                run_idx = run.get("index")
                request_id = question.get("request_id", "")
                question_idx = question.get("question_idx", 0)
                key_tuple = (report_id, run_idx, attempt, request_id, question_idx)
                # review_key нужен всегда (фронтенд шлёт его в PUT/DELETE).
                question["review_key"] = _review_key(
                    report_id, run_idx, attempt, request_id, question_idx)
                verdict = reviews_map.get(key_tuple)
                if verdict is not None:
                    question["review_verdict"] = verdict
                    reviewed += 1
                    if verdict == "useful":
                        useful += 1
                    elif verdict == "unnecessary":
                        unnecessary += 1
        report["review_summary"] = _review_summary(
            total, reviewed, useful, unnecessary)


def build_index() -> int:
    with session() as conn:
        all_reports = load_reports(conn)
        excluded_keys = active_exclusions_map(conn)
        library = load_library(conn)
        unstable_map = active_unstable_map(conn)
        # issue #93: review-разметка одним запросом; enrichment ниже — в памяти,
        # raw_json не мутируется.
        reviews_map = load_question_reviews(conn)

    # enrichment до фильтрации denylist: оценки показываются и у исключённых
    # отчётов (они остаются в excluded_reports, summary по всей базе их видит).
    enrich_reviews(all_reports, reviews_map)

    # Видимый набор и исключённые отчёты — из одной загрузки; фильтрация
    # списком сохраняет порядок started_at DESC, на который опирается рейтинг.
    reports, excluded_reports = [], []
    for report in all_reports:
        if _model_key(report) in excluded_keys:
            excluded_reports.append(report)
        else:
            reports.append(report)

    total_models = _count_models(reports)

    output = {
        "generated_at": datetime.now().isoformat(),
        "total": len(reports),
        "total_models": total_models,
        "dashboard_summary": build_dashboard_summary(all_reports, excluded_reports),
        "model_ranking": build_model_ranking(reports, unstable_map),
        "projects": group_by_project(reports, library),
    }

    index_path = PROJECT_ROOT / "docs" / "data" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return len(reports)

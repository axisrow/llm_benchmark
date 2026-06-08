"""Build docs/data/index.json from the benchmark SQLite database."""

import json
import math
import sys
from datetime import datetime

from db import (
    PROJECT_ROOT,
    active_exclusions_map,
    active_unstable_map,
    connect,
    init_schema,
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
    rows = conn.execute(
        "SELECT rel_path, raw_json FROM reports "
        "ORDER BY started_at DESC"
    ).fetchall()

    reports = []
    for row in rows:
        report = json_loads_or(row["raw_json"])
        if report is None:
            print(f"Пропускаю повреждённый ряд reports ({row['rel_path']})",
                  file=sys.stderr)
            continue

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
                "run_count": 0,
                "summary": _empty_summary(),
                "reports": [],
                "model_keys": set(),
            }
        group["reports"].append(report)
        group["model_keys"].add(_model_key(report))
        group["run_count"] += len(report.get("runs") or [])
        _accumulate_summary(group["summary"], report)

    for group in groups.values():
        group["report_count"] = len(group["reports"])
        group["model_count"] = len(group.pop("model_keys"))

    return sorted(groups.values(),
                  key=lambda g: (-g["model_count"], g["name"]))


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


def _report_is_clean(report) -> bool:
    """latest-отчёт проекта «чист»: нет таймаутов/ошибок/лимитов и все runs code=0."""
    summary = report.get("summary") or {}
    if ((summary.get("timeout") or 0) > 0 or (summary.get("error") or 0) > 0
            or (summary.get("rate_limited") or 0) > 0):
        return False
    for run in report.get("runs") or []:
        code = run.get("code")
        if not isinstance(code, int) or code != 0:
            return False
    return True


def build_model_ranking(reports, unstable_map=None):
    # unstable_map: {(provider, model): reason} — модели, помеченные нестабильными
    # вручную. Их НЕ выкидываем из рейтинга по фейлам: показываем с бейджем, а
    # метрики считаем ТОЛЬКО по чистым проектам; грязные собираем в unstable_projects.
    unstable_map = unstable_map or {}

    # reports приходят из load_reports уже ORDER BY started_at DESC,
    # поэтому первый встреченный report по ключу — самый свежий.
    latest: dict[tuple[str, str, str], dict] = {}
    for report in reports:
        project = report.get("project", "")
        provider = report.get("provider", "")
        model = report.get("model", "")
        if not project or not provider or not model:
            continue
        latest.setdefault((project, provider, model), report)

    by_model: dict[tuple[str, str], dict] = {}
    for (project, provider, model), report in latest.items():
        key = (provider, model)
        is_unstable = key in unstable_map  # выводимо из unstable_map, не храним в item
        item = by_model.setdefault(
            key,
            {
                "provider": provider,
                "model": model,
                "projects": set(),
                "successful_run_count": 0,
                "elapsed": [],
                "tokens": [],
                "costs": [],
                "latest_report": None,
                "has_failures": False,
                "unstable_projects": set(),
            },
        )

        item["projects"].add(project)
        # latest по убыванию started_at — первый report для (provider, model) самый свежий.
        if item["latest_report"] is None:
            item["latest_report"] = report

        project_clean = _report_is_clean(report)
        if not project_clean:
            if is_unstable:
                # для unstable грязный проект НЕ исключает модель — лишь метит проект,
                # и его прогоны не идут в метрики (учитываем только чистые проекты).
                item["unstable_projects"].add(project)
                continue
            # обычная модель с фейлом в latest — прежнее поведение (исключаем целиком).
            item["has_failures"] = True

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

    ranking = []
    for (provider, model), item in by_model.items():
        is_unstable = (provider, model) in unstable_map
        # unstable не исключаем по has_failures; всех — по нулю успешных (нечего показать).
        if (not is_unstable and item["has_failures"]) \
                or item["successful_run_count"] == 0:
            continue

        latest_report = item["latest_report"] or {}
        ranking.append({
            "provider": item["provider"],
            "model": item["model"],
            "key": f"{item['provider']}/{item['model']}",
            "projects": sorted(item["projects"]),
            "project_count": len(item["projects"]),
            "successful_run_count": item["successful_run_count"],
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
        })

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


def build_index() -> int:
    conn = connect()
    try:
        init_schema(conn)
        all_reports = load_reports(conn)
        excluded_keys = active_exclusions_map(conn)
        library = load_library(conn)
        unstable_map = active_unstable_map(conn)
    finally:
        conn.close()

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

"""Build docs/data/index.json from the benchmark SQLite database."""

import json
import math
import sys
from datetime import datetime

from db import PROJECT_ROOT, connect, init_schema
from pricing import get_pricing


def load_library(conn):
    try:
        rows = conn.execute(
            "SELECT name, description, prompt, what_it_tests "
            "FROM projects_library"
        ).fetchall()
    except Exception:
        return {}
    library = {}
    for row in rows:
        try:
            what = json.loads(row["what_it_tests"]) if row["what_it_tests"] else []
        except (TypeError, json.JSONDecodeError):
            what = []
        library[row["name"]] = {
            "description": row["description"],
            "prompt": row["prompt"],
            "what_it_tests": what,
        }
    return library


def load_reports(conn):
    # Активный denylist отсекаем прямо в SELECT по индексированным колонкам
    # reports.provider/model — исключённые отчёты не доходят до декодирования.
    rows = conn.execute(
        "SELECT rel_path, raw_json FROM reports r "
        "WHERE NOT EXISTS ("
        "    SELECT 1 FROM model_exclusions x "
        "    WHERE x.provider = r.provider AND x.model = r.model AND x.active = 1"
        ") "
        "ORDER BY started_at DESC"
    ).fetchall()

    reports = []
    for row in rows:
        try:
            report = json.loads(row["raw_json"])
        except (json.JSONDecodeError, TypeError) as exc:
            print(f"Пропускаю повреждённый ряд reports ({row['rel_path']}): {exc}",
                  file=sys.stderr)
            continue

        report["path"] = f"../{row['rel_path']}"
        try:
            started = datetime.fromisoformat(report["started_at"])
            report["started_at_display"] = started.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
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
                "summary": {"ok": 0, "timeout": 0, "error": 0, "rate_limited": 0},
                "reports": [],
                "model_keys": set(),
            }
        group["reports"].append(report)
        group["model_keys"].add((
            report.get("provider", ""),
            report.get("model", ""),
        ))
        group["run_count"] += len(report.get("runs") or [])
        summary = report.get("summary") or {}
        for key in ("ok", "timeout", "error", "rate_limited"):
            group["summary"][key] += summary.get(key, 0)

    for group in groups.values():
        group["report_count"] = len(group["reports"])
        group["model_count"] = len(group.pop("model_keys"))

    return sorted(groups.values(),
                  key=lambda g: (-g["model_count"], g["name"]))


def _is_number(value):
    return isinstance(value, (int, float)) and math.isfinite(value)


def _avg(values):
    return sum(values) / len(values) if values else None


def build_model_ranking(reports):
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
            },
        )

        item["projects"].add(project)
        # latest по убыванию started_at — первый report для (provider, model) самый свежий.
        if item["latest_report"] is None:
            item["latest_report"] = report

        summary = report.get("summary") or {}
        if ((summary.get("timeout") or 0) > 0 or (summary.get("error") or 0) > 0
                or (summary.get("rate_limited") or 0) > 0):
            item["has_failures"] = True

        for run in report.get("runs") or []:
            code = run.get("code")
            if isinstance(code, int) and code != 0:
                item["has_failures"] = True
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
    for item in by_model.values():
        if item["has_failures"] or item["successful_run_count"] == 0:
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
        reports = load_reports(conn)
        library = load_library(conn)
    finally:
        conn.close()

    total_models = len({
        (report.get("provider", ""), report.get("model", ""))
        for report in reports
        if report.get("provider") and report.get("model")
    })

    output = {
        "generated_at": datetime.now().isoformat(),
        "total": len(reports),
        "total_models": total_models,
        "model_ranking": build_model_ranking(reports),
        "projects": group_by_project(reports, library),
    }

    index_path = PROJECT_ROOT / "docs" / "data" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return len(reports)

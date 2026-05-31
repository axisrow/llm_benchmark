"""Build docs/data/index.json from the benchmark SQLite database."""

import json
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
                "summary": {"ok": 0, "timeout": 0, "error": 0},
                "reports": [],
            }
        group["reports"].append(report)
        group["run_count"] += len(report.get("runs") or [])
        summary = report.get("summary") or {}
        for key in ("ok", "timeout", "error"):
            group["summary"][key] += summary.get(key, 0)

    for group in groups.values():
        group["model_count"] = len(group["reports"])

    return sorted(groups.values(),
                  key=lambda g: (-g["model_count"], g["name"]))


def build_index() -> int:
    conn = connect()
    try:
        init_schema(conn)
        reports = load_reports(conn)
        library = load_library(conn)
    finally:
        conn.close()

    output = {
        "generated_at": datetime.now().isoformat(),
        "total": len(reports),
        "projects": group_by_project(reports, library),
    }

    index_path = PROJECT_ROOT / "docs" / "data" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(output, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    return len(reports)

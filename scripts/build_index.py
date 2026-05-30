#!/usr/bin/env python3
"""Читает SQLite-базу data/main.db и генерирует index.json для дашборда.

Источник правды — база (наполняется scripts/ingest.py из JSON). Вывод
index.json остаётся байт-в-байт совместимым с прежним: отчёты восстанавливаются
из дословного raw_json (точный набор и порядок ключей), обогащение
(path/started_at_display/pricing) дописывается тем же кодом, что и раньше.
"""

import json
import sys
from pathlib import Path
from datetime import datetime

# pricing.py и db.py — корень проекта и папка scripts/ в sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))
from pricing import get_pricing
from db import PROJECT_ROOT, connect, init_schema


def load_library(conn):
    """Библиотека заданий из таблицы projects_library (имя -> запись).
    Пусто, если таблицы/данных нет — группировка тогда возьмёт описание/
    задание из самих отчётов."""
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
    """Восстанавливает отчёты из базы в том же виде, что прежде давал скан
    файлов: raw_json -> dict (точные ключи/порядок) + обогащение
    path/started_at_display/pricing в том же порядке, что и раньше.

    Строки идут ORDER BY started_at DESC — это заменяет прежний
    reports.sort(key=started_at, reverse=True)."""
    rows = conn.execute(
        "SELECT rel_path, raw_json FROM reports ORDER BY started_at DESC"
    ).fetchall()

    reports = []
    for row in rows:
        report = json.loads(row["raw_json"])

        # Путь для доступа из браузера (rel_path хранится POSIX'ом).
        report["path"] = f"../{row['rel_path']}"

        # Человекочитаемая дата из started_at.
        try:
            started = datetime.fromisoformat(report["started_at"])
            report["started_at_display"] = started.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            report["started_at_display"] = report.get("started_at", "")

        # Обогащаем ценами из каталога, если поля нет или цена пустая без
        # причины (старые отчёты + fail-safe записи). Записи с note и реальные
        # цены не трогаем. Условие идентично прежнему build_index.py.
        pricing = report.get("pricing")
        if not pricing or (pricing.get("prompt_per_1m") is None and not pricing.get("note")):
            report["pricing"] = get_pricing(report.get("provider", ""), report.get("model", ""))

        reports.append(report)
    return reports


def group_by_project(reports, library):
    """Группирует прогоны по полю `project`. У каждого проекта — описание,
    задание и что проверяет (из библиотеки, иначе из самого отчёта), счётчик
    моделей, сводная статистика и список его прогонов.

    Сортировка проектов: по числу моделей убыв., затем по имени."""
    groups: dict[str, dict] = {}
    for report in reports:
        name = report.get("project", "")
        group = groups.get(name)
        if group is None:
            entry = library.get(name, {})
            group = groups[name] = {
                "name": name,
                # Библиотека — источник правды; для исторических проектов вне
                # библиотеки берём задание/описание из самого отчёта.
                "description": entry.get("description") or report.get("description"),
                "prompt": entry.get("prompt") or report.get("prompt"),
                "what_it_tests": entry.get("what_it_tests", []),
                "run_count": 0,
                "summary": {"ok": 0, "timeout": 0, "error": 0},
                "reports": [],
            }
        group["reports"].append(report)
        # `or []` / `or {}`: усечённый отчёт (убитый агент) может содержать
        # "runs": null — get(..., default) вернул бы None и уронил len/get.
        group["run_count"] += len(report.get("runs") or [])
        summary = report.get("summary") or {}
        for key in ("ok", "timeout", "error"):
            group["summary"][key] += summary.get(key, 0)

    # model_count выводится из числа отчётов проекта (один отчёт = одна модель).
    for group in groups.values():
        group["model_count"] = len(group["reports"])

    return sorted(groups.values(),
                  key=lambda g: (-g["model_count"], g["name"]))


def build_index():
    conn = connect()
    try:
        init_schema(conn)
        reports = load_reports(conn)
        library = load_library(conn)
    finally:
        conn.close()

    # Отчёты раскладываются по projects[].reports; верхнеуровневый плоский
    # список не нужен (фронт читает только data.projects) — не дублируем.
    output = {
        "generated_at": datetime.now().isoformat(),
        "total": len(reports),
        "projects": group_by_project(reports, library),
    }

    # Пишем индекс
    index_path = PROJECT_ROOT / "docs" / "data" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)

    with open(index_path, "w", encoding="utf-8") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"✓ Индекс создан: {index_path}")
    print(f"  Найдено отчётов: {len(reports)}")

if __name__ == "__main__":
    build_index()

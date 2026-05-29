#!/usr/bin/env python3
"""Сканирует data/result и генерирует index.json для дашборда."""

import json
import os
import sys
from pathlib import Path
from datetime import datetime

# pricing.py живёт в корне проекта — добавляем корень в sys.path.
sys.path.insert(0, str(Path(__file__).parent.parent))
from pricing import get_pricing

def load_library():
    """Библиотека заданий projects.json (название проекта -> запись).
    Пусто, если файла нет или он повреждён — группировка тогда возьмёт
    описание/задание из самих отчётов."""
    path = Path(__file__).parent.parent / "projects.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


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
    project_root = Path(__file__).parent.parent
    result_dir = project_root / "data" / "result"

    if not result_dir.exists():
        print(f"Папка {result_dir} не найдена")
        return {}

    reports = []

    # Сканируем все report.json: data/result/<project>/<provider>_<model>/report.json
    for report_file in sorted(result_dir.glob("*/*/report.json")):
        # Один повреждённый отчёт (напр. обрыв записи при kill) не должен ронять
        # пересборку всего индекса — пропускаем его с предупреждением.
        try:
            with open(report_file) as f:
                report = json.load(f)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Пропускаю повреждённый отчёт {report_file}: {exc}", file=sys.stderr)
            continue

        # Добавляем путь для доступа из браузера
        rel_path = report_file.relative_to(project_root)
        report["path"] = f"../{rel_path}"

        # Парсим дату из started_at
        try:
            started = datetime.fromisoformat(report["started_at"])
            report["started_at_display"] = started.strftime("%Y-%m-%d %H:%M:%S")
        except Exception:
            report["started_at_display"] = report.get("started_at", "")

        # Обогащаем ценами из каталога, если поля нет или цена пустая без причины
        # (старые отчёты + fail-safe записи agent.py при сбое lookup'а — их стоит
        # переобогатить, когда каталог снова доступен). Записи с note (subscription/
        # self-hosted) и реальные цены не трогаем.
        pricing = report.get("pricing")
        if not pricing or (pricing.get("prompt_per_1m") is None and not pricing.get("note")):
            report["pricing"] = get_pricing(report.get("provider", ""), report.get("model", ""))

        reports.append(report)

    # Сортируем по дате (новые первыми)
    reports.sort(key=lambda r: r.get("started_at") or "", reverse=True)

    # Отчёты раскладываются по projects[].reports; верхнеуровневый плоский
    # список не нужен (фронт читает только data.projects) — не дублируем.
    output = {
        "generated_at": datetime.now().isoformat(),
        "total": len(reports),
        "projects": group_by_project(reports, load_library()),
    }

    # Пишем индекс
    index_path = project_root / "docs" / "data" / "index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)

    with open(index_path, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"✓ Индекс создан: {index_path}")
    print(f"  Найдено отчётов: {len(reports)}")

if __name__ == "__main__":
    build_index()

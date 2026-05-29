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

def build_index():
    project_root = Path(__file__).parent.parent
    result_dir = project_root / "data" / "result"

    if not result_dir.exists():
        print(f"Папка {result_dir} не найдена")
        return {}

    reports = []

    # Сканируем все report.json
    for report_file in sorted(result_dir.glob("*/report.json")):
        with open(report_file) as f:
            report = json.load(f)

        # Добавляем путь для доступа из браузера
        rel_path = report_file.relative_to(project_root)
        report["path"] = f"../{rel_path}"

        # Парсим дату из started_at
        try:
            started = datetime.fromisoformat(report["started_at"])
            report["started_at_display"] = started.strftime("%Y-%m-%d %H:%M:%S")
        except:
            report["started_at_display"] = report["started_at"]

        # Обогащаем ценами из каталога, если поле отсутствует (обратная совместимость).
        if "pricing" not in report or report["pricing"] is None:
            report["pricing"] = get_pricing(report.get("provider", ""), report.get("model", ""))

        reports.append(report)

    # Сортируем по дате (новые первыми)
    reports.sort(key=lambda r: r["started_at"], reverse=True)

    output = {
        "generated_at": datetime.now().isoformat(),
        "total": len(reports),
        "reports": reports
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

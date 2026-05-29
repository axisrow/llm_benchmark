#!/usr/bin/env python3
"""Разовый перенос результатов в новую вложенную раскладку.

Было (плоско, проект и модель смешаны в имени):
    data/result/<project>_<model>/report.json
    data/result/<project>_<model>/<stamp>_<i>/

Стало (одна папка на проект, внутри — подпапки по провайдеру и модели):
    data/result/<project>/<provider>_<model>/report.json
    data/result/<project>/<provider>_<model>/<stamp>_<i>/

Провайдер в имени подпапки снимает коллизию: одна модель (напр. glm-4.7) у разных
провайдеров теперь раскладывается по разным подпапкам. Канонические project/
provider/model берутся из самого report.json, а не из имени старой папки.

Идемпотентность: папки, уже лежащие в новой структуре (report.json внутри
<project>/<provider>_<model>/, т.е. на два уровня ниже result/), пропускаются.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

# Раскладку (целевой путь) и санитайзинг берём из agent.py — единый источник
# правды, чтобы миграция и новые прогоны не разъехались.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from agent import work_root_for  # noqa: E402

PROJECT_ROOT = Path(__file__).resolve().parent.parent
RESULT_DIR = PROJECT_ROOT / "data" / "result"


def _move(src: Path, dst: Path) -> None:
    """Переместить src в dst, по возможности через `git mv` (сохранить историю)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    try:
        subprocess.run(
            ["git", "-C", str(PROJECT_ROOT), "mv", str(src), str(dst)],
            check=True, capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Папка не под git (или git недоступен) — обычный перенос.
        shutil.move(str(src), str(dst))


def migrate() -> None:
    if not RESULT_DIR.exists():
        print(f"Папка {RESULT_DIR} не найдена")
        return

    moved = 0
    # Старые плоские папки: report.json лежит ровно на один уровень ниже result/.
    # Уже перенесённые лежат на два уровня (*/*/report.json) и сюда не попадают —
    # отсюда идемпотентность, отдельная проверка не нужна.
    for report_file in sorted(RESULT_DIR.glob("*/report.json")):
        old_dir = report_file.parent
        try:
            report = json.loads(report_file.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Пропускаю {report_file}: {exc}", file=sys.stderr)
            continue

        new_dir = work_root_for(report.get("project", ""),
                                report.get("provider", ""),
                                report.get("model", ""))
        if new_dir.exists():
            print(f"ПРОПУСК {old_dir.name}: цель {new_dir.relative_to(RESULT_DIR)} "
                  "уже существует", file=sys.stderr)
            continue

        _move(old_dir, new_dir)

        # Чиним абсолютные пути runs[].dir в уже распарсенном отчёте (старые
        # указывали на плоскую папку) и пишем его на новое место. Фронт их не
        # читает, но пусть данные не «врут».
        for run in report.get("runs", []):
            copy_name = Path(run.get("dir", "")).name
            if copy_name:
                run["dir"] = str(new_dir / copy_name)
        (new_dir / "report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )

        print(f"  {old_dir.name}  ->  {new_dir.relative_to(RESULT_DIR)}")
        moved += 1

    print(f"✓ Перенесено папок: {moved}")


if __name__ == "__main__":
    migrate()

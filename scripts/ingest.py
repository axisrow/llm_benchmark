#!/usr/bin/env python3
"""Разовый импортёр: заливает старые JSON-исходники в `data/main.db`.

Источники: все `data/result/*/*/report.json`, `projects.json`, `prices.json`,
`free_models.json`, кэш `data/.openrouter_cache.json`. Идемпотентно: повторный
прогон не плодит дубли (отчёты — upsert по (project,provider,model,started_at);
конфиги, кэш и правила — полная перезапись таблиц).

ОДНОРАЗОВЫЙ инструмент миграции JSON → база. После переноса данных в базу и
удаления JSON-файлов больше не нужен: `bench.py` пишет отчёты в базу напрямую,
а конфиги/цены/правила тоже живут в базе.
"""

import json
import sqlite3
import sys
from pathlib import Path

# db.py живёт рядом, в scripts/.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from db import PROJECT_ROOT, connect, init_schema, upsert_report


def ingest_reports(conn: sqlite3.Connection, root: Path) -> int:
    """Загружает все report.json через общий db.upsert_report. Битый файл
    пропускается с предупреждением — один обрыв записи не валит весь ингест."""
    result_dir = root / "data" / "result"
    count = 0
    for report_file in sorted(result_dir.glob("*/*/report.json")):
        try:
            raw = report_file.read_text(encoding="utf-8")
            report = json.loads(raw)
        except (json.JSONDecodeError, OSError) as exc:
            print(f"Пропускаю повреждённый отчёт {report_file}: {exc}",
                  file=sys.stderr)
            continue

        rel_path = report_file.relative_to(root).as_posix()
        upsert_report(conn, report, rel_path, raw)
        count += 1
    return count


def ingest_projects_library(conn: sqlite3.Connection, root: Path) -> None:
    """Загружает projects.json (название проекта -> запись)."""
    path = root / "projects.json"
    try:
        library = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    for name, entry in library.items():
        conn.execute(
            """
            INSERT INTO projects_library
                (name, description, prompt, what_it_tests, raw_json)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (name) DO UPDATE SET
                description   = excluded.description,
                prompt        = excluded.prompt,
                what_it_tests = excluded.what_it_tests,
                raw_json      = excluded.raw_json
            """,
            (name, entry.get("description"), entry.get("prompt"),
             json.dumps(entry.get("what_it_tests", []), ensure_ascii=False),
             json.dumps(entry, ensure_ascii=False)),
        )


def ingest_prices(conn: sqlite3.Connection, root: Path) -> None:
    """Загружает prices.json. Три таблицы перезаписываются целиком, чтобы
    удаления в JSON распространялись в базу."""
    path = root / "prices.json"
    try:
        prices = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    conn.execute("DELETE FROM price_overrides")
    for key, entry in (prices.get("overrides") or {}).items():
        conn.execute(
            "INSERT INTO price_overrides (key, prompt_per_1m, completion_per_1m)"
            " VALUES (?, ?, ?)",
            (key, entry.get("prompt_per_1m"), entry.get("completion_per_1m")),
        )

    conn.execute("DELETE FROM price_aliases")
    for local_key, openrouter_id in (prices.get("catalog_aliases") or {}).items():
        conn.execute(
            "INSERT INTO price_aliases (local_key, openrouter_id) VALUES (?, ?)",
            (local_key, openrouter_id),
        )

    conn.execute("DELETE FROM provider_notes")
    for provider, note in (prices.get("provider_notes") or {}).items():
        conn.execute(
            "INSERT INTO provider_notes (provider, note) VALUES (?, ?)",
            (provider, note),
        )


def ingest_openrouter_cache(conn: sqlite3.Connection, root: Path) -> None:
    """Загружает кэш каталога OpenRouter (если он есть на диске)."""
    path = root / "data" / ".openrouter_cache.json"
    try:
        cache = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    conn.execute("DELETE FROM openrouter_cache")
    for model_id, entry in (cache.get("models") or {}).items():
        conn.execute(
            "INSERT INTO openrouter_cache (model_id, prompt, completion) "
            "VALUES (?, ?, ?)",
            (model_id, entry.get("prompt"), entry.get("completion")),
        )
    conn.execute(
        "INSERT INTO openrouter_cache_meta (id, fetched_at) VALUES (1, ?) "
        "ON CONFLICT (id) DO UPDATE SET fetched_at = excluded.fetched_at",
        (cache.get("fetched_at"),),
    )


def ingest_free_rules(conn: sqlite3.Connection, root: Path) -> None:
    """Загружает free_models.json (правила бесплатности по провайдеру).
    Таблица перезаписывается целиком."""
    path = root / "free_models.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return

    conn.execute("DELETE FROM free_rules")
    for provider, entry in (data.get("providers") or {}).items():
        conn.execute(
            "INSERT INTO free_rules (provider, strategy, models) VALUES (?, ?, ?)",
            (provider, entry.get("strategy", ""),
             json.dumps(entry.get("models", []), ensure_ascii=False)),
        )


def main() -> None:
    conn = connect()
    try:
        init_schema(conn)
        with conn:  # одна транзакция на весь ингест
            reports = ingest_reports(conn, PROJECT_ROOT)
            ingest_projects_library(conn, PROJECT_ROOT)
            ingest_prices(conn, PROJECT_ROOT)
            ingest_openrouter_cache(conn, PROJECT_ROOT)
            ingest_free_rules(conn, PROJECT_ROOT)
        print(f"✓ База наполнена: {PROJECT_ROOT / 'data' / 'main.db'}")
        print(f"  Загружено отчётов: {reports}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()

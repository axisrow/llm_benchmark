"""Схема SQLite-базы и хелпер подключения.

База `data/main.db` — **единственный** источник правды проекта: отчёты прогонов
(пишет `bench.py`), библиотека заданий, цены, правила бесплатности и кэш
OpenRouter. JSON-файлов с данными на диске больше нет. `build_index.py` читает
базу и собирает `docs/data/index.json` для сайта.

База **коммитится в git** — раз JSON исчезли, в CI данные взять больше неоткуда.
`scripts/ingest.py` — разовый импортёр (первичная миграция старых JSON в базу).
"""

import sqlite3
from pathlib import Path

# Корень проекта — на уровень выше папки scripts/.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "data" / "main.db"

# `raw_json` в `reports`/`projects_library` хранит дословный текст исходного
# JSON — это гарантирует, что при пересборке index.json порядок и набор ключей
# каждого отчёта воспроизводятся байт-в-байт (фронтенд не ломается).
SCHEMA = """
CREATE TABLE IF NOT EXISTS reports (
    id              INTEGER PRIMARY KEY,
    project         TEXT NOT NULL,
    provider        TEXT NOT NULL,
    model           TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    run_elapsed     REAL,
    copies          INTEGER,
    summary_ok      INTEGER NOT NULL DEFAULT 0,
    summary_timeout INTEGER NOT NULL DEFAULT 0,
    summary_error   INTEGER NOT NULL DEFAULT 0,
    rel_path        TEXT NOT NULL,
    raw_json        TEXT NOT NULL,
    UNIQUE (project, provider, model, started_at)
);
CREATE INDEX IF NOT EXISTS idx_reports_started ON reports(started_at);

CREATE TABLE IF NOT EXISTS runs (
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    idx       INTEGER NOT NULL,
    port      INTEGER,
    dir       TEXT,
    status    TEXT,
    code      INTEGER,
    elapsed   REAL,
    PRIMARY KEY (report_id, idx)
);

CREATE TABLE IF NOT EXISTS projects_library (
    name          TEXT PRIMARY KEY,
    description   TEXT,
    prompt        TEXT,
    what_it_tests TEXT,
    raw_json      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS price_overrides (
    key               TEXT PRIMARY KEY,
    prompt_per_1m     REAL,
    completion_per_1m REAL
);

CREATE TABLE IF NOT EXISTS price_aliases (
    local_key     TEXT PRIMARY KEY,
    openrouter_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_notes (
    provider TEXT PRIMARY KEY,
    note     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS openrouter_cache (
    model_id   TEXT PRIMARY KEY,
    prompt     TEXT,
    completion TEXT
);

CREATE TABLE IF NOT EXISTS openrouter_cache_meta (
    id         INTEGER PRIMARY KEY CHECK (id = 1),
    fetched_at REAL
);

CREATE TABLE IF NOT EXISTS free_rules (
    provider TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    models   TEXT NOT NULL DEFAULT '[]'
);
"""


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    """Открывает базу, включает внешние ключи и row-доступ по имени.

    WAL-режим разрешает одного писателя + многих читателей одновременно: это
    снимает «database is locked», когда параллельно идут запись отчёта (bench.py),
    обновление кэша цен (pricing.refresh_cache) или прогон check_models.py.
    Файлы `-wal`/`-shm` эфемерны и игнорируются git (см. .gitignore)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    # WAL не спасает от гонки писатель-писатель: при busy_timeout=0 второй
    # писатель падает мгновенно. 5с ожидания хватает на короткие транзакции
    # (запись отчёта, обновление кэша цен) при двух параллельных bench.py.
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """Создаёт таблицы, если их ещё нет (идемпотентно)."""
    conn.executescript(SCHEMA)


def upsert_report(conn: sqlite3.Connection, report: dict, rel_path: str,
                  raw_json: str) -> int:
    """Вставляет/обновляет отчёт (таблицы reports + runs) и возвращает его id.

    Единый путь записи отчёта: зовётся и из `bench.py` (прогон пишет в базу), и
    из `scripts/ingest.py` (разовая миграция). Уникальность по
    (project, provider, model, started_at) делает запись идемпотентной (upsert);
    `runs` для отчёта перезаписываются (delete-then-insert). Не коммитит —
    вызывающий оборачивает в свою транзакцию."""
    summary = report.get("summary") or {}
    report_id = conn.execute(
        """
        INSERT INTO reports
            (project, provider, model, started_at, run_elapsed, copies,
             summary_ok, summary_timeout, summary_error, rel_path, raw_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (project, provider, model, started_at) DO UPDATE SET
            run_elapsed=excluded.run_elapsed, copies=excluded.copies,
            summary_ok=excluded.summary_ok, summary_timeout=excluded.summary_timeout,
            summary_error=excluded.summary_error, rel_path=excluded.rel_path,
            raw_json=excluded.raw_json
        RETURNING id
        """,
        (report.get("project", ""), report.get("provider", ""),
         report.get("model", ""), report.get("started_at", ""),
         report.get("run_elapsed"), report.get("copies"),
         summary.get("ok", 0), summary.get("timeout", 0),
         summary.get("error", 0), rel_path, raw_json),
    ).fetchone()[0]

    conn.execute("DELETE FROM runs WHERE report_id = ?", (report_id,))
    conn.executemany(
        "INSERT INTO runs (report_id, idx, port, dir, status, code, elapsed) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        [(report_id, r.get("index"), r.get("port"), r.get("dir"),
          r.get("status"), r.get("code"), r.get("elapsed"))
         for r in report.get("runs") or []],
    )
    return report_id

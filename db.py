"""Схема SQLite-базы и хелпер подключения.

База `data/main.db` — **единственный** источник правды проекта: отчёты прогонов
(пишет `bench.py`), библиотека заданий, цены, правила бесплатности и кэш
OpenRouter. JSON-файлов с данными на диске больше нет. `build_index.py` читает
базу и собирает `docs/data/index.json` для сайта.

База **коммитится в git** — раз JSON исчезли, в CI данные взять больше неоткуда.
"""

import contextlib
import datetime as dt
import sqlite3
import zlib
from collections.abc import Generator, Iterable
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from artifacts import RunArtifact

# backward compat: re-export старого имени (check_models до PR #40).
from utils import json_loads_or  # noqa: F401

# Корень проекта — папка с этим модулем.
PROJECT_ROOT = Path(__file__).resolve().parent
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
    -- summary_* — денормализованный legacy-срез сводки для SQL-аналитики;
    -- index_builder его НЕ читает (берёт всё из raw_json). Новые ключи
    -- сводки (напр. rate_limited) живут только в raw_json — отдельной
    -- колонки им не заводим, чтобы не мигрировать закоммиченную базу.
    summary_ok      INTEGER NOT NULL DEFAULT 0,
    summary_timeout INTEGER NOT NULL DEFAULT 0,
    summary_error   INTEGER NOT NULL DEFAULT 0,
    rel_path        TEXT NOT NULL,
    raw_json        TEXT NOT NULL,
    UNIQUE (project, provider, model, started_at)
);
CREATE INDEX IF NOT EXISTS idx_reports_started ON reports(started_at);

-- Урезанный индекс прогонов: причина исхода (reason) тут намеренно не хранится,
-- она живёт только в reports.raw_json (см. _RUN_BASE_COLUMNS ниже).
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

CREATE TABLE IF NOT EXISTS agent_questions (
    report_id    INTEGER NOT NULL,
    run_idx      INTEGER NOT NULL,
    attempt_idx  INTEGER NOT NULL,
    session_id   TEXT NOT NULL,
    request_id   TEXT NOT NULL,
    round_idx    INTEGER NOT NULL,
    question_idx INTEGER NOT NULL,
    header       TEXT,
    question     TEXT NOT NULL,
    options_json TEXT NOT NULL,
    multiple     INTEGER NOT NULL,
    custom       INTEGER NOT NULL,
    answer_json  TEXT,
    responder    TEXT NOT NULL,
    fallback_used INTEGER NOT NULL,
    reply_status TEXT NOT NULL,
    reply_error  TEXT,
    elapsed      REAL NOT NULL,
    PRIMARY KEY (report_id, run_idx, attempt_idx, request_id, question_idx),
    FOREIGN KEY (report_id, run_idx) REFERENCES runs(report_id, idx)
        ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS file_blobs (
    sha256           TEXT PRIMARY KEY,
    size_bytes       INTEGER NOT NULL,
    content_encoding TEXT NOT NULL,
    content_blob     BLOB NOT NULL
);

CREATE TABLE IF NOT EXISTS run_artifacts (
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    run_idx   INTEGER NOT NULL,
    path      TEXT NOT NULL,
    kind      TEXT NOT NULL CHECK (kind IN ('log', 'agent_file')),
    sha256    TEXT NOT NULL REFERENCES file_blobs(sha256),
    PRIMARY KEY (report_id, run_idx, path)
);
CREATE INDEX IF NOT EXISTS idx_run_artifacts_blob ON run_artifacts(sha256);

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

CREATE TABLE IF NOT EXISTS model_exclusions (
    provider   TEXT NOT NULL,
    model      TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    active     INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, model)
);

CREATE TABLE IF NOT EXISTS model_unstability (
    provider   TEXT NOT NULL,
    model      TEXT NOT NULL,
    reason     TEXT NOT NULL DEFAULT '',
    active     INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (provider, model)
);
"""

# Таблица runs — намеренно урезанный SQL-индекс для быстрой агрегатной аналитики
# (status/code/elapsed). Человекочитаемая причина исхода (reason: HTTP 429, auth/
# billing, timeout tail) сюда НЕ кладётся, чтобы не мигрировать закоммиченную базу;
# полный источник причин — reports.raw_json (поле runs[*].reason).
_RUN_BASE_COLUMNS = ("report_id", "idx", "port", "dir", "status", "code", "elapsed")
_ARTIFACT_CONTENT_ENCODING = "zlib"
# Общая для model_exclusions и model_unstability. Если схемы разойдутся —
# завести отдельные _EXCLUSION_COLUMNS и _UNSTABLE_COLUMNS.
_EXCLUSION_COLUMNS = ("provider", "model", "reason", "active", "created_at", "updated_at")
_EXCL_COLS_CSV = ", ".join(_EXCLUSION_COLUMNS)


def safe_json_loads(text: str, default: object = None) -> object:
    """json.loads с безопасным откатом: возвращает *default* при ошибке парсинга."""
    return json_loads_or(text, default=default)


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


@contextlib.contextmanager
def session(path: Path = DB_PATH) -> Generator[sqlite3.Connection, None, None]:
    """Контекстный менеджер: открывает базу, инициализирует схему, отдаёт conn.

    Гарантированно закрывает соединение при выходе (нормальном или по исключению).
    Канонический способ открыть базу в production-путях (benchmark_report,
    pricing, check_models, index_builder, scripts/model_exclusions) — заменяет
    ручной идиом connect/init_schema/try-finally-close. Для записи оборачивай
    выданный conn ещё и в `with conn:` (транзакция: commit при успехе, rollback
    при исключении). Пути, которым НЕ нужен init_schema (напр. load_project,
    сознательно не маскирующий ошибку БД под «проект не найден»), используют
    connect() напрямую.
    """
    conn = connect(path)
    try:
        init_schema(conn)
        yield conn
    finally:
        conn.close()


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _clean_model_ref(provider: str, model: str) -> tuple[str, str]:
    provider = provider.strip()
    model = model.strip()
    if not provider or not model:
        raise ValueError("provider and model must be non-empty")
    return provider, model


def split_model_ref(value: str) -> tuple[str, str]:
    """Разбивает `'provider/model'` в `(provider, model)` (split по первому `/`).

    Единый парсер ключа модели для CLI (check_models, model_exclusions). Бросает
    `ValueError` при отсутствии `/` или пустых сегментах; вызывающий оборачивает
    в свой тип ошибки (`SystemExit`/`argparse.ArgumentTypeError`)."""
    if "/" not in value:
        raise ValueError(f"нужен формат provider/model: {value!r}")
    provider, model = value.split("/", 1)
    return _clean_model_ref(provider, model)


def model_key(provider: str, model: str) -> str:
    """Собирает ключ модели `'provider/model'` (обратное к split_model_ref)."""
    return f"{provider}/{model}"


def get_model_exclusion(conn: sqlite3.Connection, provider: str, model: str,
                        active_only: bool = True) -> sqlite3.Row | None:
    """Возвращает denylist-запись модели или None."""
    provider, model = _clean_model_ref(provider, model)
    query = f"""
        SELECT {_EXCL_COLS_CSV}
        FROM model_exclusions
        WHERE provider = ? AND model = ?
    """
    if active_only:
        query += " AND active = 1"
    return conn.execute(query, (provider, model)).fetchone()


def list_model_exclusions(conn: sqlite3.Connection,
                          active_only: bool = True) -> list[sqlite3.Row]:
    """Список моделей denylist-а, по умолчанию только активные."""
    query = f"""
        SELECT {_EXCL_COLS_CSV}
        FROM model_exclusions
    """
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY active DESC, provider, model"
    return conn.execute(query).fetchall()


def active_exclusions_map(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], str]:
    """Активный denylist как `{(provider, model): reason}`."""
    return {
        (row["provider"], row["model"]): row["reason"]
        for row in list_model_exclusions(conn)
    }


def block_model_exclusion(conn: sqlite3.Connection, provider: str, model: str,
                          reason: str = "") -> sqlite3.Row:
    """Добавляет/реактивирует модель в denylist-е без сброса created_at."""
    provider, model = _clean_model_ref(provider, model)
    now = _now_iso()
    return conn.execute(
        f"""
        INSERT INTO model_exclusions
            ({_EXCL_COLS_CSV})
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT (provider, model) DO UPDATE SET
            reason = excluded.reason,
            active = 1,
            updated_at = excluded.updated_at
        RETURNING {_EXCL_COLS_CSV}
        """,
        (provider, model, reason or "", now, now),
    ).fetchone()


def unblock_model_exclusion(conn: sqlite3.Connection, provider: str,
                            model: str) -> sqlite3.Row | None:
    """Деактивирует denylist-запись, не удаляя историю."""
    provider, model = _clean_model_ref(provider, model)
    return conn.execute(
        f"""
        UPDATE model_exclusions
        SET active = 0, updated_at = ?
        WHERE provider = ? AND model = ?
        RETURNING {_EXCL_COLS_CSV}
        """,
        (_now_iso(), provider, model),
    ).fetchone()


# --- Статус «нестабильная» (model_unstability) ------------------------------
# Отдельно от denylist: unstable-модель НЕ скрывается из рейтинга и НЕ блокируется
# на входе прогона — она берёт часть проектов, но фейлит другие (таймаут/лимит).
# Тег ставится вручную на МОДЕЛЬ; какие проекты нестабильны, index_builder
# вычисляет сам из данных (latest-отчёт проекта с фейлами).


def get_model_unstable(conn: sqlite3.Connection, provider: str, model: str,
                       active_only: bool = True) -> sqlite3.Row | None:
    """Возвращает unstable-запись модели или None."""
    provider, model = _clean_model_ref(provider, model)
    query = f"""
        SELECT {_EXCL_COLS_CSV}
        FROM model_unstability
        WHERE provider = ? AND model = ?
    """
    if active_only:
        query += " AND active = 1"
    return conn.execute(query, (provider, model)).fetchone()


def list_model_unstable(conn: sqlite3.Connection,
                        active_only: bool = True) -> list[sqlite3.Row]:
    """Список нестабильных моделей, по умолчанию только активные."""
    query = f"""
        SELECT {_EXCL_COLS_CSV}
        FROM model_unstability
    """
    if active_only:
        query += " WHERE active = 1"
    query += " ORDER BY active DESC, provider, model"
    return conn.execute(query).fetchall()


def active_unstable_map(
    conn: sqlite3.Connection,
) -> dict[tuple[str, str], str]:
    """Активные unstable-метки как `{(provider, model): reason}`."""
    return {
        (row["provider"], row["model"]): row["reason"]
        for row in list_model_unstable(conn)
    }


def mark_model_unstable(conn: sqlite3.Connection, provider: str, model: str,
                        reason: str = "") -> sqlite3.Row:
    """Помечает/реактивирует модель как нестабильную без сброса created_at."""
    provider, model = _clean_model_ref(provider, model)
    now = _now_iso()
    return conn.execute(
        f"""
        INSERT INTO model_unstability
            ({_EXCL_COLS_CSV})
        VALUES (?, ?, ?, 1, ?, ?)
        ON CONFLICT (provider, model) DO UPDATE SET
            reason = excluded.reason,
            active = 1,
            updated_at = excluded.updated_at
        RETURNING {_EXCL_COLS_CSV}
        """,
        (provider, model, reason or "", now, now),
    ).fetchone()


def unmark_model_unstable(conn: sqlite3.Connection, provider: str,
                          model: str) -> sqlite3.Row | None:
    """Снимает метку нестабильности, не удаляя историю."""
    provider, model = _clean_model_ref(provider, model)
    return conn.execute(
        f"""
        UPDATE model_unstability
        SET active = 0, updated_at = ?
        WHERE provider = ? AND model = ?
        RETURNING {_EXCL_COLS_CSV}
        """,
        (_now_iso(), provider, model),
    ).fetchone()


def replace_report_artifacts(conn: sqlite3.Connection, report_id: int,
                             artifacts: Iterable["RunArtifact"], *,
                             partial: bool = False) -> None:
    """Replaces artifact mappings for a report and stores deduped file blobs.

    `artifacts` — `RunArtifact` из artifacts.py (структурный доступ по атрибутам).
    `partial=True` — точечная замена: удаляются маппинги только тех run_idx,
    что встречаются в `artifacts`; остальные копии отчёта не трогаются (нужно
    backfill-у, когда часть рабочих папок уже зачищена с диска). По умолчанию
    замена всего отчёта (путь записи полного отчёта, см. upsert_report).
    """
    artifact_list = list(artifacts)
    if partial:
        indices = sorted({int(a.run_idx) for a in artifact_list})
        if indices:
            placeholders = ", ".join("?" * len(indices))
            conn.execute(
                f"DELETE FROM run_artifacts "
                f"WHERE report_id = ? AND run_idx IN ({placeholders})",
                [report_id, *indices],
            )
    else:
        conn.execute("DELETE FROM run_artifacts WHERE report_id = ?", (report_id,))

    # Пишем blob только для sha256, которых ещё нет в базе: компрессия дорогая,
    # а ON CONFLICT DO NOTHING всё равно отбросил бы дубль уже после сжатия.
    candidate_shas = {str(a.sha256) for a in artifact_list}
    existing_shas = _existing_blob_shas(conn, candidate_shas)
    blob_rows = []
    seen: set[str] = set()
    for artifact in artifact_list:
        sha256 = str(artifact.sha256)
        if sha256 in existing_shas or sha256 in seen:
            continue
        seen.add(sha256)
        blob_rows.append((
            sha256,
            int(artifact.size_bytes),
            _ARTIFACT_CONTENT_ENCODING,
            sqlite3.Binary(zlib.compress(bytes(artifact.content))),
        ))
    conn.executemany(
        """
        INSERT INTO file_blobs (sha256, size_bytes, content_encoding, content_blob)
        VALUES (?, ?, ?, ?)
        ON CONFLICT (sha256) DO NOTHING
        """,
        blob_rows,
    )

    conn.executemany(
        """
        INSERT INTO run_artifacts (report_id, run_idx, path, kind, sha256)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (report_id, int(a.run_idx), str(a.path), str(a.kind), str(a.sha256))
            for a in artifact_list
        ],
    )

    # Перезапись маппингов (и full-, и partial-путь) могла осиротить старые
    # блобы: их sha256 больше не встречается в run_artifacts, а отдельных ссылок
    # у дедуплицированных по sha256 блобов нет. Подметаем в той же транзакции —
    # иначе file_blobs копит мёртвые блобы навсегда (база коммитится в git, ср.
    # delete_report). Безопасно: prune удаляет лишь блобы без единой ссылки,
    # общий с другим отчётом — уцелеет.
    prune_orphan_blobs(conn)


def prune_orphan_blobs(conn: sqlite3.Connection) -> int:
    """Удаляет file_blobs, на которые не ссылается ни один run_artifacts.

    Блобы дедуплицируются по sha256 и живут отдельно от ссылок; после удаления
    отчёта/артефактов на них могут не остаться ссылки. Единственное место,
    владеющее этим инвариантом, — здесь (раньше один и тот же SQL копировался
    в maintenance-скрипты). Возвращает число удалённых блобов.
    """
    cur = conn.execute(
        "DELETE FROM file_blobs "
        "WHERE sha256 NOT IN (SELECT sha256 FROM run_artifacts)"
    )
    return cur.rowcount


def delete_report(conn: sqlite3.Connection, report_id: int) -> int:
    """Удаляет отчёт целиком и подметает осиротевшие блобы.

    `runs`/`run_artifacts` уходят каскадом (ON DELETE CASCADE + foreign_keys=ON),
    после чего вызывается `prune_orphan_blobs`. Возвращает число удалённых строк
    reports (0, если отчёта с таким id не было).
    """
    cur = conn.execute("DELETE FROM reports WHERE id = ?", (report_id,))
    deleted = cur.rowcount
    if deleted:
        prune_orphan_blobs(conn)
    return deleted


def _existing_blob_shas(conn: sqlite3.Connection, shas: set[str]) -> set[str]:
    """Подмножество `shas`, уже лежащее в file_blobs (одним запросом)."""
    if not shas:
        return set()
    sha_list = list(shas)
    placeholders = ", ".join("?" * len(sha_list))
    rows = conn.execute(
        f"SELECT sha256 FROM file_blobs WHERE sha256 IN ({placeholders})",
        sha_list,
    )
    return {row["sha256"] for row in rows}


def list_artifacts(conn: sqlite3.Connection, report_id: int,
                   run_idx: int | None = None) -> list[sqlite3.Row]:
    query = """
        SELECT
            a.report_id, a.run_idx, a.path, a.kind, a.sha256,
            b.size_bytes, b.content_encoding
        FROM run_artifacts AS a
        JOIN file_blobs AS b ON b.sha256 = a.sha256
        WHERE a.report_id = ?
    """
    params: list[object] = [report_id]
    if run_idx is not None:
        query += " AND a.run_idx = ?"
        params.append(run_idx)
    query += " ORDER BY a.run_idx, a.path"
    return conn.execute(query, params).fetchall()


def _decode_blob(encoding: str, blob: object) -> bytes:
    content = bytes(blob)
    if encoding == _ARTIFACT_CONTENT_ENCODING:
        return zlib.decompress(content)
    if encoding == "identity":
        return content
    raise ValueError(f"unknown artifact encoding: {encoding}")


def read_artifact(conn: sqlite3.Connection, report_id: int, run_idx: int,
                  path: str) -> bytes:
    row = conn.execute(
        """
        SELECT b.content_encoding, b.content_blob
        FROM run_artifacts AS a
        JOIN file_blobs AS b ON b.sha256 = a.sha256
        WHERE a.report_id = ? AND a.run_idx = ? AND a.path = ?
        """,
        (report_id, run_idx, path),
    ).fetchone()
    if row is None:
        raise FileNotFoundError(f"artifact not found: {report_id}/{run_idx}/{path}")
    return _decode_blob(row["content_encoding"], row["content_blob"])


def iter_artifact_contents(conn: sqlite3.Connection, report_id: int,
                           run_idx: int | None = None) -> Iterable[tuple[int, str, bytes]]:
    """`(run_idx, path, content)` всех артефактов отчёта одним JOIN-запросом.

    Заменяет связку `list_artifacts` + поштучный `read_artifact` (N+1 запросов
    с повторным JOIN на каждую строку) при пакетном экспорте.
    """
    query = """
        SELECT a.run_idx, a.path, b.content_encoding, b.content_blob
        FROM run_artifacts AS a
        JOIN file_blobs AS b ON b.sha256 = a.sha256
        WHERE a.report_id = ?
    """
    params: list[object] = [report_id]
    if run_idx is not None:
        query += " AND a.run_idx = ?"
        params.append(run_idx)
    query += " ORDER BY a.run_idx, a.path"
    for row in conn.execute(query, params):
        yield (
            row["run_idx"],
            row["path"],
            _decode_blob(row["content_encoding"], row["content_blob"]),
        )


def upsert_report(conn: sqlite3.Connection, report: dict, rel_path: str,
                  raw_json: str, artifacts: Iterable["RunArtifact"] | None = None) -> int:
    """Вставляет/обновляет отчёт (таблицы reports + runs) и возвращает его id.

    Единый путь записи отчёта: зовётся из benchmark runner. Уникальность по
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

    conn.execute("DELETE FROM agent_questions WHERE report_id = ?", (report_id,))
    conn.execute("DELETE FROM runs WHERE report_id = ?", (report_id,))
    columns = _RUN_BASE_COLUMNS
    placeholders = ", ".join("?" * len(columns))
    run_rows = []
    for r in report.get("runs") or []:
        run_rows.append((
            report_id, r.get("index"), r.get("port"), r.get("dir"),
            r.get("status"), r.get("code"), r.get("elapsed"),
        ))
    conn.executemany(
        f"INSERT INTO runs ({', '.join(columns)}) VALUES ({placeholders})",
        run_rows,
    )
    import json
    question_rows = []
    for run in report.get("runs") or []:
        for question in run.get("questions") or []:
            question_rows.append((
                report_id, run.get("index"), question.get("attempt_idx", 1),
                question.get("session_id", ""), question.get("request_id", ""),
                question.get("round_idx", 0), question.get("question_idx", 0),
                question.get("header"), question.get("question", ""),
                json.dumps(question.get("options") or [], ensure_ascii=False),
                int(bool(question.get("multiple"))), int(bool(question.get("custom"))),
                json.dumps(question.get("answer"), ensure_ascii=False),
                question.get("responder", ""), int(bool(question.get("fallback_used"))),
                question.get("reply_status", ""), question.get("reply_error"),
                question.get("elapsed", 0),
            ))
    conn.executemany(
        """INSERT INTO agent_questions
        (report_id, run_idx, attempt_idx, session_id, request_id, round_idx,
         question_idx, header, question, options_json, multiple, custom,
         answer_json, responder, fallback_used, reply_status, reply_error, elapsed)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        question_rows,
    )
    if artifacts is not None:
        replace_report_artifacts(conn, report_id, artifacts)
    return report_id

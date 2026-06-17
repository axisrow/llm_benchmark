"""Перенос отчётов из другой копии базы (например git-версии) в текущую.

Нужен, когда рабочая data/main.db потеряла отчёты, которые есть в закоммиченной
версии. Переносит reports + связанные runs/run_artifacts/file_blobs, переназначая
report_id (он автоинкрементный и в двух базах разный). Идемпотентен по ключу
(project, provider, model, started_at) — уже существующие отчёты пропускает.

Ключи отчётов на перенос читаются из файла (по строке `project|provider|model|started_at`).

Запуск:
    python scripts/restore_reports_from_git.py --source /tmp/head_main.db \
        --keys /tmp/genuine_keys.txt [--dry-run]
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import db

REPORT_COLS = [
    "project", "provider", "model", "started_at", "run_elapsed", "copies",
    "summary_ok", "summary_timeout", "summary_error", "rel_path", "raw_json",
]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", required=True, help="путь к базе-источнику")
    parser.add_argument("--keys", required=True,
                        help="файл с ключами project|provider|model|started_at")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    try:
        keys_text = Path(args.keys).read_text(encoding="utf-8")
    except FileNotFoundError:
        print(f"Файл с ключами не найден: {args.keys}", file=sys.stderr)
        return 2
    except OSError as exc:
        print(f"Не удалось прочитать файл ключей {args.keys}: {exc}", file=sys.stderr)
        return 2

    keys = []
    for line in keys_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("|")
        if len(parts) != 4:
            print(f"Пропускаю некорректную строку: {line!r}")
            continue
        keys.append(tuple(parts))

    conn = db.connect()
    attached = False
    try:
        db.init_schema(conn)
        conn.execute("ATTACH DATABASE ? AS src", (str(Path(args.source).resolve()),))
        attached = True

        added = skipped = missing = failed = 0
        runs_total = arts_total = blobs_total = 0
        # Каждый ключ — отдельная транзакция: ошибка на одном отчёте откатывает
        # только его, а уже перенесённые остаются (повторный запуск не начинает
        # с нуля). Счётчики меняем ТОЛЬКО после успешного выхода из `with conn`
        # (т.е. после durable COMMIT): иначе откат частично вставленного отчёта
        # — или сбой самого COMMIT (SQLITE_BUSY, диск) — оставил бы их завышенными
        # и одновременно посчитанными в `failed`.
        for project, provider, model, started_at in keys:
            outcome = None  # ("missing"|"skipped"|"added", runs, blobs, arts)
            try:
                with conn:
                    src_row = conn.execute(
                        "SELECT id FROM src.reports WHERE project=? AND provider=? "
                        "AND model=? AND started_at=?",
                        (project, provider, model, started_at)).fetchone()
                    if src_row is None:
                        print(f"  НЕТ в источнике: {project}|{provider}/{model}|{started_at}")
                        outcome = ("missing", 0, 0, 0)
                        continue
                    src_id = src_row[0]

                    exists = conn.execute(
                        "SELECT 1 FROM reports WHERE project=? AND provider=? "
                        "AND model=? AND started_at=?",
                        (project, provider, model, started_at)).fetchone()
                    if exists:
                        outcome = ("skipped", 0, 0, 0)
                        continue

                    if args.dry_run:
                        outcome = ("added", 0, 0, 0)
                        continue

                    # 1) report -> новый id
                    cols = ", ".join(REPORT_COLS)
                    vals = conn.execute(
                        f"SELECT {cols} FROM src.reports WHERE id=?", (src_id,)).fetchone()
                    placeholders = ", ".join("?" * len(REPORT_COLS))
                    cur = conn.execute(
                        f"INSERT INTO reports ({cols}) VALUES ({placeholders})", vals)
                    new_id = cur.lastrowid

                    # 2) runs
                    r = conn.execute(
                        "INSERT INTO runs (report_id, idx, port, dir, status, code, elapsed) "
                        "SELECT ?, idx, port, dir, status, code, elapsed "
                        "FROM src.runs WHERE report_id=?", (new_id, src_id))

                    # 3) file_blobs, на которые ссылаются артефакты этого отчёта
                    #    (только отсутствующие — sha256 дедуплицируется глобально)
                    b = conn.execute(
                        "INSERT OR IGNORE INTO file_blobs "
                        "(sha256, size_bytes, content_encoding, content_blob) "
                        "SELECT DISTINCT fb.sha256, fb.size_bytes, fb.content_encoding, fb.content_blob "
                        "FROM src.file_blobs fb "
                        "JOIN src.run_artifacts ra ON ra.sha256=fb.sha256 "
                        "WHERE ra.report_id=?", (src_id,))

                    # 4) run_artifacts
                    a = conn.execute(
                        "INSERT INTO run_artifacts (report_id, run_idx, path, kind, sha256) "
                        "SELECT ?, run_idx, path, kind, sha256 "
                        "FROM src.run_artifacts WHERE report_id=?", (new_id, src_id))

                    outcome = ("added", r.rowcount, b.rowcount, a.rowcount)
            except Exception as exc:
                # Транзакция этого ключа уже откатилась (with conn re-raises после
                # rollback); логируем и идём дальше — не теряя предыдущий прогресс.
                # Сбой COMMIT при выходе из `with conn` тоже попадает сюда, и т.к.
                # счётчики ещё не тронуты — ключ считается только в `failed`.
                print(f"  ОШИБКА на {project}|{provider}/{model}|{started_at}: {exc}",
                      file=sys.stderr)
                failed += 1
                continue

            # Транзакция закоммичена успешно — теперь применяем счётчики.
            kind, r_cnt, b_cnt, a_cnt = outcome
            if kind == "missing":
                missing += 1
            elif kind == "skipped":
                skipped += 1
            else:  # "added"
                runs_total += r_cnt
                blobs_total += b_cnt
                arts_total += a_cnt
                added += 1

        verb = "будет добавлено" if args.dry_run else "добавлено"
        suffix = f"; с ошибкой: {failed}" if failed else ""
        print(f"\nОтчётов {verb}: {added}; пропущено (уже есть): {skipped}; "
              f"нет в источнике: {missing}{suffix}")
        if not args.dry_run:
            print(f"  runs: {runs_total}, run_artifacts: {arts_total}, "
                  f"новых блобов: {blobs_total}")
            total = conn.execute("SELECT count(*) FROM reports").fetchone()[0]
            print(f"  Всего отчётов в базе: {total}")
        # Ненулевой код, если хоть один ключ упал: это инструмент восстановления
        # потерянных отчётов, и exit status — единственный надёжный машинный сигнал
        # для автоматизации. Иначе частичный restore легко принять за полный.
        return 1 if failed else 0
    finally:
        # DETACH только после успешного ATTACH: иначе «no such database: src»
        # из finally маскирует исходную ошибку (например, недоступный файл).
        # Сам DETACH тоже оборачиваем: если он бросит (busy/constraint), его
        # исключение не должно подменить то, что летело из try-блока.
        if attached:
            try:
                conn.execute("DETACH DATABASE src")
            except Exception:
                pass
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())

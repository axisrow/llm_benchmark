#!/usr/bin/env python3
"""Дозаписывает/пересчитывает lint-метрику (runs[].linters, runs[].ruff,
lint_summary, ruff_summary) в raw_json отчётов — БЕЗ перепрогона моделей.

Гоняет ТЕКУЩИЙ реестр LINTERS (ruff/tidy/jq + js/node) по собранным артефактам
каждой code==0 копии. Цели:
  - диагностика: тест линтера на РЕАЛЬНЫХ артефактах базы (урок #130 — гонять
    на реальных данных, не только синтетических тестах);
  - backfill: отчёты, прогнанные ДО добавления нового линтера (напр. js), не
    имеют runs[].linters.<name> — этот скрипт пересчитывает все линтеры и
    вписывает их единообразно (формат байт-в-байт как benchmark_report._build_report).

Совпадает с bench.py: runs[].linters = {имя → {status, errors}}, runs[].ruff —
производный от линтера 'ruff' (#100), lint_summary = summarize_linters,
ruff_summary = summarize_lint. Артефакты читает из БД (file_blobs/run_artifacts);
НЕ трогает runs[].fine, usage, code, status — только lint-поля.

Запуск:
    python scripts/backfill_lint.py                       # dry-run по всем
    python scripts/backfill_lint.py --report-id 276       # точечно
    python scripts/backfill_lint.py --project library_fine
    python scripts/backfill_lint.py --apply               # записать
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from artifacts import RunArtifact  # noqa: E402
from db import DB_PATH  # noqa: E402
from lint_metrics import (  # noqa: E402
    lint_copy_artifacts,
    summarize_lint,
    summarize_linters,
)


def _load_run_artifacts(conn: sqlite3.Connection,
                        report_id: int) -> dict[int, list[RunArtifact]]:
    """Артефакты отчёта, сгруппированные по run_idx (RunArtifact с .content).

    ВСЕ артефакты копии (включая run.log), НЕ фильтруя по kind заранее: фильтр по
    agent_file + суффиксу делает сама ``lint_metrics._artifacts_for`` — тогда копия
    с только run.log (без исходников) честно получает ``na`` по каждому линтеру, как
    в bench.py. Ранняя отсечка ``kind != agent_file`` здесь (ревью Codex cycle 1)
    роняла такую копию из группировки целиком и подменяла её реальные ``na`` на
    отсутствие ключей, искажая исторические знаменатели."""
    from db import list_artifacts, read_artifact
    by_idx: dict[int, list[RunArtifact]] = {}
    for row in list_artifacts(conn, report_id):
        content = read_artifact(conn, report_id, row["run_idx"], row["path"])
        by_idx.setdefault(row["run_idx"], []).append(RunArtifact(
            run_idx=row["run_idx"],
            path=row["path"],
            kind=row["kind"],
            size_bytes=row["size_bytes"],
            sha256=row["sha256"],
            content=content,
            source_path=Path(row["path"]),
        ))
    return by_idx


def backfill_report(conn: sqlite3.Connection, report_id: int
                    ) -> tuple[dict | None, str]:
    """Пересчитывает runs[].linters/runs[].ruff/lint_summary/ruff_summary для
    отчёта. Возвращает (new_report|None, заметка). None — нет артефактов/копий."""
    row = conn.execute(
        "SELECT raw_json FROM reports WHERE id=?", (report_id,)
    ).fetchone()
    if row is None:
        return None, f"report {report_id} не найден"
    report = json.loads(row["raw_json"])

    artifacts_by_idx = _load_run_artifacts(conn, report_id)
    if not artifacts_by_idx:
        return None, f"report {report_id}: нет agent_file-артефактов"

    any_lint = False
    for run in report.get("runs", []):
        idx = run.get("index")
        code = run.get("code")
        # gate code==0 + наличие артефактов копии — как bench.py. Фейловые копии
        # (code!=0, или code==0 без артефактов) НЕ получают lint-ключей вовсе —
        # bench.py их не эмитит (truthy-gate spread в _build_report), и мы должны
        # повторять байт-в-байт: ключи УДАЛЯЕМ, а не пишем {} / null (ревью Claude
        # cycle 1, инвариант raw_json из CLAUDE.md).
        if code == 0 and idx in artifacts_by_idx:
            per_copy = lint_copy_artifacts(artifacts_by_idx[idx])
            linters = {
                name: {"status": r.status, "errors": r.errors}
                for name, r in per_copy.items()
            }
            # _build_report эмитит ruff/linters условным spread (truthy-gate).
            # 'linters' — пустой dict был бы falsy и не эмитился, но у code==0
            # копии всегда есть хотя бы 'na'-результат по каждому линтеру → dict
            # непустой. ruff из per_copy берётся как в _build_report.
            ruff = per_copy.get("ruff")
            run["linters"] = linters
            run["ruff"] = ({"status": ruff.status, "errors": ruff.errors}
                           if ruff is not None else None)
            # Убираем legacy runs[].lint БЕЗУСЛОВНО (ревью Codex cycle 3, усиление
            # C1). _build_report его не эмитит; summarize_lint читает run['lint']
            # ПЕРВЫМ, с fallback на linters.ruff — значит оставшийся от старого
            # backfill lint перекрывал бы свежий пересчёт и ruff_summary разошёлся
            # бы с lint_summary.ruff. 487 копий в БД несут legacy lint. Удаляем
            # здесь (как и в else-ветке ниже), а не только «не добавляем».
            run.pop("lint", None)
            any_lint = True
        else:
            run.pop("linters", None)
            run.pop("ruff", None)
            run.pop("lint", None)

    if not any_lint:
        return None, f"report {report_id}: нет code==0 копий с артефактами"

    report["lint_summary"] = summarize_linters(report["runs"])
    report["ruff_summary"] = summarize_lint(report["runs"])
    return report, ""


def _select_report_ids(conn: sqlite3.Connection, args) -> list[int]:
    if args.report_id is not None:
        return [args.report_id]
    base = "SELECT id FROM reports WHERE 1=1"
    params: list = []
    if args.project:
        base += " AND project=?"
        params.append(args.project)
    base += " ORDER BY id"
    return [r["id"] for r in conn.execute(base, params)]


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Пересчёт lint-метрики (runs[].linters/lint_summary) в raw_json "
                    "отчётов по текущему реестру LINTERS (без перепрогона моделей).")
    ap.add_argument("--report-id", type=int, help="точечно по id отчёта")
    ap.add_argument("--project", help="только отчёты проекта (напр. library_fine)")
    ap.add_argument("--apply", action="store_true",
                    help="записать в БД (по умолчанию — dry-run)")
    ap.add_argument("--db", type=Path, default=DB_PATH)
    args = ap.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row

    ids = _select_report_ids(conn, args)
    if not ids:
        print("Нет отчётов для пересчёта lint-метрики.")
        return 0

    print("mode     report | lint-сводка по отчёту")
    changed = 0
    for rid in ids:
        try:
            report, note = backfill_report(conn, rid)
        except Exception as exc:
            # Изоляция по отчёту (ревью Claude cycle 1, M1): один malformed raw_json
            # или испорченный blob не должен валить пересчёт остальных. upsert здесь
            # нет — каждый отчёт пишется своей транзакцией, так что частичный прогон
            # безопасен; ошибка печатается с id для доследования.
            print(f"  error {rid}: {exc.__class__.__name__}: {exc}")
            continue
        if report is None:
            print(f"  skip {rid}: {note}")
            continue
        ls = report.get("lint_summary", {})
        parts = []
        for name, s in ls.items():
            if not s or not s.get("checked"):
                continue
            parts.append(f"{name}={s['total_errors']}/{s['checked']}copy")
        summary_str = " ".join(parts) if parts else "(нет checked-линтеров)"
        print(f"  report {rid} [{report.get('project')}]: {summary_str}")
        if not args.apply:
            continue
        new_raw = json.dumps(report, ensure_ascii=False, indent=2)
        # ID-scoped UPDATE только raw_json (ревью Codex cycle 1). upsert_report
        # непригоден: его conflict-identity — (project,provider,model,started_at) из
        # raw_json, а не rid; на повреждённой/импортированной БД lint-данные отчёта A
        # перезаписали бы отчёт B (с delete-then-insert runs/questions). Lint-поля
        # живут только в raw_json, нормализованные таблицы пересчёт не трогает —
        # поэтому прямой UPDATE по id и fail-closed: проверяем, что SQL-identity
        # строки совпадает с полями raw_json (иначе чужой отчёт) и rowcount==1.
        sql_row = conn.execute(
            "SELECT project, provider, model, started_at FROM reports WHERE id=?",
            (rid,)).fetchone()
        if sql_row is None:
            print(f"  skip {rid}: отчёт исчез из БД до записи")
            continue
        raw_identity = (report.get("project"), report.get("provider"),
                        report.get("model"), report.get("started_at"))
        if tuple(sql_row) != raw_identity:
            print(f"  skip {rid}: SQL-identity {tuple(sql_row)} расходится с "
                  f"raw_json {raw_identity} — запись отменена (повреждённая БД)")
            continue
        with conn:
            cur = conn.execute(
                "UPDATE reports SET raw_json=? WHERE id=?", (new_raw, rid))
        if cur.rowcount != 1:
            print(f"  skip {rid}: UPDATE затронул {cur.rowcount} строк — отмена")
            continue
        changed += 1

    print()
    if args.apply:
        print(f"ЗАПИСАНО: пересчитано {changed} отчётов(а)")
    else:
        print(f"(dry-run; --apply чтобы записать пересчёт для {len(ids)} отобранных)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

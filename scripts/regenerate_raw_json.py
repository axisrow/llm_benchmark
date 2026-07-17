"""Принудительно регенерирует raw_json отчётов из текущего состояния таблицы runs.

Чинит рассинхрон, возникший когда прогоны удалили из таблицы runs + пересчитали
summary_* колонки, но raw_json не обновили (cleanup_runs.py осознанно raw_json не
трогает). index_builder строит рейтинг ТОЛЬКО из raw_json, поэтому дашборд показывал
старые (с удалёнными ошибками/таймаутами) данные.

Источник usage (токены/стоимость per-run) — ТОЛЬКО raw_json; таблица runs его не
хранит. Поэтому raw_json не пересобирается с нуля: берём существующий raw_json и
оставляем в runs[] только те index, что ЕСТЬ сейчас в таблице runs, сохраняя usage.
summary/usage_summary/copies переагрегируются из оставшихся.

Запись через upsert_report — атомарно raw_json + summary_* + перезапись runs;
artifacts не передаём (таблицы runs/run_artifacts уже выверены вручную).

Byte-for-byte: json.dumps(report, ensure_ascii=False, indent=2) — как в save_report.

Запуск:
    python scripts/regenerate_raw_json.py --dry-run
    python scripts/regenerate_raw_json.py
    python scripts/regenerate_raw_json.py --report-id 64   # точечно
    python scripts/regenerate_raw_json.py --all            # идемпотентно по всем
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # корень — import db
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts — _common

import db
from _common import add_dry_run
from artifacts import ARTIFACT_KIND_AGENT_FILE, ARTIFACT_KIND_LOG
from lint_metrics import summarize_lint, summarize_linters
from opencode_runtime import RUN_CODES
from usage import Usage, summarize_usages

# code -> ключ summary; единая таксономия из opencode_runtime.RUN_CODES.
_CODE_TO_SUMMARY = {code: key for code, (key, _label) in RUN_CODES.items()}


def _recount_artifact_summary(conn, report_id: int,
                              keep_indices: set[int]) -> dict:
    """Пересчёт artifact_summary по выжившим артефактам из БД (без файлового
    сканирования): files/logs/agent_files/bytes по run_idx из keep_indices,
    size_bytes — из file_blobs. errors всегда [] (накапливаются только при
    первичном сборе с диска, в БД их нет)."""
    if not keep_indices:
        return {"files": 0, "logs": 0, "agent_files": 0, "bytes": 0, "errors": []}
    placeholders = ", ".join("?" * len(keep_indices))
    logs = agent_files = total_bytes = 0
    for kind, size in conn.execute(
        f"SELECT a.kind, b.size_bytes FROM run_artifacts a "
        f"JOIN file_blobs b ON b.sha256 = a.sha256 "
        f"WHERE a.report_id = ? AND a.run_idx IN ({placeholders})",
        (report_id, *keep_indices),
    ):
        if kind == ARTIFACT_KIND_LOG:
            logs += 1
        elif kind == ARTIFACT_KIND_AGENT_FILE:
            agent_files += 1
        total_bytes += size
    return {"files": logs + agent_files, "logs": logs,
            "agent_files": agent_files, "bytes": total_bytes, "errors": []}


def _count_no_artifact(conn, report_id: int, kept: list[dict], *,
                       questions_only: bool) -> int:
    """Сколько выживших копий code=0 не сохранили ни одного agent_file (#142).

    Считается из БД, как и artifact_summary. questions-only прогонам файл не
    положен (фазы build нет) — там счётчик всегда 0, ср.
    index_builder._expects_agent_file и benchmark_report._summarize.
    """
    if questions_only:
        return 0
    with_file = {
        row[0] for row in conn.execute(
            "SELECT DISTINCT run_idx FROM run_artifacts "
            "WHERE report_id = ? AND kind = ?",
            (report_id, ARTIFACT_KIND_AGENT_FILE),
        )
    }
    return sum(1 for run in kept
               if run.get("code") == 0 and run.get("index") not in with_file)


def rebuild_report_dict(conn, report_id: int, report: dict,
                        keep_indices: set[int]) -> dict:
    """Новый report dict с отфильтрованными runs[] и пересчётом ВСЕХ сводок.

    keep_indices — index прогонов, которые надо оставить (= idx в таблице runs).
    Порядок ключей верхнего уровня сохраняется (byte-for-byte): только заменяем
    значения, не пересортировываем dict.

    Пересчитываются: summary (из code; no_artifact — SQL по выжившим
    run_artifacts, #142), usage_summary (из usage), copies,
    lint_summary/ruff_summary (из runs[].linters через lint_metrics) и
    artifact_summary (SQL по выжившим run_artifacts). Ключ сводки попадает в
    результат ТОЛЬКО если он был в исходном report — отчёты без метрик не
    обрастают новыми ключами (байт-в-байт для них). run_elapsed — историческое,
    не трогаем.
    """
    old_runs = report.get("runs") or []
    kept = [r for r in old_runs if r.get("index") in keep_indices]

    # summary: пересчёт из code оставшихся, сохраняя набор ключей исходного summary
    # (ok/timeout/error всегда; rate_limited — только если был в исходнике).
    old_summary = report.get("summary") or {}
    counts = {k: 0 for k in _CODE_TO_SUMMARY.values()}
    for r in kept:
        key = _CODE_TO_SUMMARY.get(r.get("code"))
        if key is not None:
            counts[key] += 1
    new_summary = {key: counts[key] for key in ("ok", "timeout", "error")}
    if "rate_limited" in old_summary:
        new_summary["rate_limited"] = counts["rate_limited"]
    # issue #142: no_artifact — не код исхода (его нет в RUN_CODES), поэтому цикл
    # выше его не считает, а старое значение относилось ко ВСЕМ копиям, включая
    # выброшенные. Пересчитываем по выжившим — из run_artifacts, как и
    # artifact_summary. Ключ добавляем только если он был в исходнике: отчёты
    # старого формата новыми полями не обрастают (байт-в-байт).
    if "no_artifact" in old_summary:
        new_summary["no_artifact"] = _count_no_artifact(
            conn, report_id, kept, questions_only=bool(
                (report.get("planning") or {}).get("questions_only")))

    # usage_summary: переагрегация через summarize_usages из usage оставшихся.
    usages = [Usage.from_report_dict(r.get("usage")) for r in kept]
    new_usage_summary = summarize_usages(usages)

    new_report = dict(report)            # сохраняет порядок верхнеуровневых ключей
    new_report["runs"] = kept
    new_report["summary"] = new_summary
    new_report["usage_summary"] = new_usage_summary
    new_report["copies"] = len(kept)
    # issue #121/#126: lint/ruff/artifact_summary пересчитываются по выжившим
    # runs/артефактам — иначе после удаления прогонов они остаются согласованными
    # со старым (до удаления) набором. lint/ruff — из runs[].linters (данные per-run
    # уже в raw_json, повторно линтеры не гоняем); artifact — SQL по run_artifacts.
    if "lint_summary" in report:
        new_report["lint_summary"] = summarize_linters(kept)
    if "ruff_summary" in report:
        new_report["ruff_summary"] = summarize_lint(kept)
    if "artifact_summary" in report:
        new_report["artifact_summary"] = _recount_artifact_summary(
            conn, report_id, keep_indices)
    # run_elapsed — НЕ трогаем (историческое).
    return new_report


def regenerate_one(conn, report_id: int, *, dry_run: bool) -> tuple[dict, bool]:
    """Возвращает (diff, will_change) для одного отчёта."""
    row = conn.execute(
        "SELECT raw_json, rel_path FROM reports WHERE id=?", (report_id,)
    ).fetchone()
    if row is None:
        raise SystemExit(f"Нет отчёта id={report_id}")
    report = json.loads(row["raw_json"])
    table_indices = {
        r[0] for r in conn.execute(
            "SELECT idx FROM runs WHERE report_id=?", (report_id,))
    }
    new_report = rebuild_report_dict(conn, report_id, report, table_indices)

    old_n = len(report.get("runs") or [])
    new_n = len(new_report["runs"])
    diff = {
        "id": report_id,
        "runs": (old_n, new_n),
        "summary_before": report.get("summary"),
        "summary_after": new_report["summary"],
        "copies": (report.get("copies"), new_report["copies"]),
    }
    will_change = (old_n != new_n
                   or report.get("summary") != new_report["summary"]
                   or report.get("copies") != new_report["copies"]
                   or any(report.get(k) != new_report.get(k) for k in
                          ("lint_summary", "ruff_summary", "artifact_summary")))
    if dry_run or not will_change:
        return diff, will_change

    new_raw = json.dumps(new_report, ensure_ascii=False, indent=2)
    db.upsert_report(conn, new_report, row["rel_path"], new_raw)  # artifacts=None
    return diff, True


def run(conn, ids, *, dry_run: bool = False) -> int:
    """Регенерирует raw_json для отчётов ids. Возвращает число изменённых."""
    changed = 0
    for rid in ids:
        diff, will_change = regenerate_one(conn, rid, dry_run=dry_run)
        if will_change:
            changed += 1
            print(f"  id={diff['id']}: runs {diff['runs'][0]}->{diff['runs'][1]}, "
                  f"copies {diff['copies'][0]}->{diff['copies'][1]}, "
                  f"summary {diff['summary_before']} -> {diff['summary_after']}")
    return changed


def _select_ids(conn, args) -> list[int]:
    if args.report_id is not None:
        return [args.report_id]
    if args.all:
        return [r[0] for r in conn.execute("SELECT id FROM reports ORDER BY id")]
    # только рассинхронные: count(runs) != json_array_length(raw_json.runs)
    return [r[0] for r in conn.execute(
        "SELECT id FROM reports r WHERE "
        "(SELECT count(*) FROM runs ru WHERE ru.report_id=r.id) <> "
        "json_array_length(raw_json, '$.runs') ORDER BY id")]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    add_dry_run(parser)
    parser.add_argument("--report-id", type=int, default=None,
                        help="чинить только один отчёт")
    parser.add_argument("--all", action="store_true",
                        help="прогнать по ВСЕМ отчётам (идемпотентно)")
    args = parser.parse_args()

    with db.session() as conn:
        ids = _select_ids(conn, args)
        if not ids:
            print("Рассинхронов нет — чинить нечего.")
            return 0

        print(f"Отчётов к обработке: {len(ids)}")
        if args.dry_run:
            changed = run(conn, ids, dry_run=True)
            print(f"\n[dry-run] изменилось бы отчётов: {changed}. Ничего не записано.")
        else:
            with conn:
                changed = run(conn, ids, dry_run=False)
            print(f"\nОбновлено отчётов: {changed}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Выборочное ручное удаление отчётов из базы (issue #121).

Автоудаление в бенчмарке запрещено: backfill_runs только дозаписывает, а решение
«перезаписать» принимает человек — этим CLI. Dry-run по умолчанию (по образцу
cleanup_result_dir.py): печатает счётчики, ничего не удаляет; --apply удаляет.

Режимы:
    python scripts/delete_reports.py --report-id 42              # отчёт целиком
    python scripts/delete_reports.py --report-id 42 --run-idx 3  # одна копия
    python scripts/delete_reports.py prov/model                  # вся модель
    python scripts/delete_reports.py prov/model --project p      # модель в проекте
    python scripts/delete_reports.py --project p                 # проект целиком

После удаления дозапись — python bench.py / scripts/backfill_runs.py.
Каталог data/result/<project>/ чистится только в режиме «проект целиком» (как в
dashboard_server); хвосты остальных режимов подметёт scripts/cleanup_result_dir.py.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # корень — import db
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts — regenerate_raw_json

import db
from artifacts import delete_project_result_dir, project_has_active_run
from regenerate_raw_json import regenerate_one
from utils import sanitize_name

RESULT_ROOT = db.PROJECT_ROOT / "data" / "result"


def _report_counters(conn, report_ids: list[int]) -> dict[str, int]:
    """Счётчики затрагиваемых строк БЕЗ удаления (печатаются до него)."""
    if not report_ids:
        return {"reports": 0, "runs": 0, "artifacts": 0}
    placeholders = ", ".join("?" * len(report_ids))
    runs = conn.execute(
        f"SELECT count(*) FROM runs WHERE report_id IN ({placeholders})",
        report_ids).fetchone()[0]
    artifacts = conn.execute(
        f"SELECT count(*) FROM run_artifacts WHERE report_id IN ({placeholders})",
        report_ids).fetchone()[0]
    return {"reports": len(report_ids), "runs": runs, "artifacts": artifacts}


def _print_counters(counters: dict) -> None:
    print(f"К удалению: отчётов={counters['reports']}, runs={counters['runs']}, "
          f"артефактов={counters['artifacts']}")


def _dry_run_notice() -> int:
    print("\n[dry-run] изменений не внесено. Передайте --apply для удаления.")
    return 0


def delete_one_report(conn, report_id: int, *, apply: bool) -> int:
    """Режим 1: отчёт целиком по id."""
    row = conn.execute(
        "SELECT project, provider, model, started_at FROM reports WHERE id=?",
        (report_id,)).fetchone()
    if row is None:
        print(f"error: нет отчёта id={report_id}", file=sys.stderr)
        return 1
    print(f"Отчёт id={report_id}: {row['project']} / "
          f"{row['provider']}/{row['model']} @ {row['started_at']}")
    _print_counters(_report_counters(conn, [report_id]))
    if not apply:
        return _dry_run_notice()
    with conn:
        db.delete_report(conn, report_id)
    print("Удалено.")
    return 0


def delete_one_run(conn, report_id: int, run_idx: int, *, apply: bool) -> int:
    """Режим 2: одна копия отчёта; raw_json выжившего пересобирается."""
    with conn:
        report_row = conn.execute(
            "SELECT raw_json FROM reports WHERE id=?", (report_id,)).fetchone()
        table_indices = {
            row[0] for row in conn.execute(
                "SELECT idx FROM runs WHERE report_id=?", (report_id,))
        }
        if run_idx not in table_indices:
            print(f"error: нет копии run_idx={run_idx} в отчёте id={report_id}",
                  file=sys.stderr)
            return 1

        try:
            raw_report = json.loads(report_row["raw_json"])
            raw_indices = {
                run.get("index") for run in (raw_report.get("runs") or [])
            }
        except (AttributeError, json.JSONDecodeError, TypeError):
            raw_indices = None
        if raw_indices != table_indices:
            print(
                f"error: рассинхрон runs/raw_json в отчёте id={report_id}; "
                "сначала выполните "
                f"'python scripts/regenerate_raw_json.py --report-id {report_id}'",
                file=sys.stderr,
            )
            return 1

        artifacts = conn.execute(
            "SELECT count(*) FROM run_artifacts WHERE report_id=? AND run_idx=?",
            (report_id, run_idx)).fetchone()[0]
        survivors = len(table_indices) - 1
        print(f"К удалению: копия run_idx={run_idx} отчёта id={report_id}, "
              f"артефактов={artifacts}")
        if survivors == 0:
            print("Отчёт опустеет и будет удалён целиком.")
        if not apply:
            return _dry_run_notice()

        conn.execute(
            "DELETE FROM run_artifacts WHERE report_id=? AND run_idx=?",
            (report_id, run_idx))
        conn.execute(
            "DELETE FROM runs WHERE report_id=? AND idx=?", (report_id, run_idx))
        if survivors == 0:
            # опустевший отчёт — целиком (каскад и блобы внутри delete_report)
            db.delete_report(conn, report_id)
        else:
            # выжившему пересобираем raw_json из оставшихся runs — переиспользуем
            # regenerate_raw_json (единая таксономия RUN_CODES, байт-в-байт формат)
            regenerate_one(conn, report_id, dry_run=False)
            db.prune_orphan_blobs(conn)
    print("Удалено.")
    return 0


def delete_model(conn, provider: str, model: str, project: str | None,
                 *, apply: bool) -> int:
    """Режим 3: все результаты модели, опционально в одном проекте."""
    query = "SELECT id FROM reports WHERE provider=? AND model=?"
    params: list[object] = [provider, model]
    if project is not None:
        query += " AND project=?"
        params.append(project)
    report_ids = [r["id"] for r in conn.execute(query, params).fetchall()]
    scope = f" в проекте {project!r}" if project else ""
    print(f"Все результаты {provider}/{model}{scope}:")
    _print_counters(_report_counters(conn, report_ids))
    if not report_ids:
        print("Удалять нечего.")
        return 0
    if not apply:
        return _dry_run_notice()
    with conn:
        db.delete_model_reports(conn, provider, model, project)
    print("Удалено.")
    return 0


def delete_whole_project(conn, name: str, *, apply: bool,
                         result_root: Path) -> int:
    """Режим 4: проект целиком (delete_project + файловая чистка каталога)."""
    disk_name = sanitize_name(name)
    if project_has_active_run(result_root, disk_name):
        print(f"error: проект {name} имеет активный прогон; дождись завершения "
              "или останови бенчмарк", file=sys.stderr)
        return 1

    report_ids = [r["id"] for r in conn.execute(
        "SELECT id FROM reports WHERE project=?", (name,)).fetchall()]
    library_row = conn.execute(
        "SELECT 1 FROM projects_library WHERE name=?", (name,)).fetchone()
    if not report_ids and library_row is None:
        print(f"error: проект {name!r} не найден", file=sys.stderr)
        return 1
    print(f"Проект {name!r} целиком (+строка библиотеки, "
          f"каталог {result_root / disk_name}):")
    _print_counters(_report_counters(conn, report_ids))
    if not apply:
        return _dry_run_notice()
    try:
        with conn:
            db.delete_project(conn, name)
    except db.ProjectDirectoryCollisionError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    # Файловая чистка ТОЛЬКО после успешного commit (как в dashboard_server):
    # даже если она не удалась, БД уже консистентна, хвост подметёт
    # cleanup_result_dir.
    try:
        delete_project_result_dir(result_root, disk_name)
    except Exception as exc:  # noqa: BLE001 — файловый сбой не отменяет удаление в БД
        print(f"warning: каталог проекта не удалось почистить: {exc}",
              file=sys.stderr)
    print("Удалено.")
    return 0


def run(conn, *, report_id: int | None = None, run_idx: int | None = None,
        model: str | None = None, project: str | None = None,
        apply: bool = False, result_root: Path | None = None) -> int:
    """Диспетчер режимов (валидация сочетаний аргументов — в main)."""
    if report_id is not None and run_idx is not None:
        return delete_one_run(conn, report_id, run_idx, apply=apply)
    if report_id is not None:
        return delete_one_report(conn, report_id, apply=apply)
    if model is not None:
        provider, model_name = db.split_model_ref(model)
        return delete_model(conn, provider, model_name, project, apply=apply)
    if project is not None:
        return delete_whole_project(conn, project, apply=apply,
                                    result_root=result_root or RESULT_ROOT)
    raise ValueError("не задан режим удаления")


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("model", nargs="?", default=None,
                        help="пара provider/model (режим «вся модель»)")
    parser.add_argument("--report-id", type=int, default=None,
                        help="id отчёта (без --run-idx — отчёт целиком)")
    parser.add_argument("--run-idx", type=int, default=None,
                        help="номер копии внутри отчёта (требует --report-id)")
    parser.add_argument("--project", default=None,
                        help="сузить модель до проекта; без модели — проект целиком")
    parser.add_argument("--apply", action="store_true",
                        help="удалить (по умолчанию dry-run)")
    args = parser.parse_args()

    # Вся валидация сочетаний — ДО открытия базы.
    if args.report_id is not None and (args.model or args.project):
        parser.error("--report-id не сочетается с provider/model и --project")
    if args.run_idx is not None and args.report_id is None:
        parser.error("--run-idx требует --report-id")
    if args.report_id is None and args.model is None and args.project is None:
        parser.error("нужен режим: --report-id | provider/model | --project")
    if args.model is not None:
        try:
            db.split_model_ref(args.model)
        except ValueError as exc:
            parser.error(str(exc))

    with db.session() as conn:
        return run(conn, report_id=args.report_id, run_idx=args.run_idx,
                   model=args.model, project=args.project, apply=args.apply)


if __name__ == "__main__":
    raise SystemExit(main())

"""Общие хелперы для тестов (issue #54, находка #9).

pytest подхватывает conftest.py автоматически и кладёт его каталог на sys.path,
так что тесты могут делать `from conftest import ...`. Сюда вынесено то, что
дублировалось по тестам:
- build_index_data — сборка index.json из набора отчётов во временной БД
  (раньше 3 копии: метод в test_bench + test_fix_B4 + test_fix_B5);
- fake_artifacts — артефакты копий отчёта-фикстуры (issue #142);
- capture_stdout — захват stdout (раньше дублировался в test_fix_B11 и инлайн).

Массовая миграция ~50 temp-DB scaffolding'ов на общую фикстуру — отдельный
follow-up (объёмный, механический); здесь только явные дубли.
"""

import contextlib
import hashlib
import io
import json
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

# Корень репозитория на sys.path — для import db / index_builder из этого модуля.
_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import db  # noqa: E402
import index_builder  # noqa: E402


def capture_stdout(fn) -> str:
    """Выполнить fn() и вернуть захваченный stdout."""
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        fn()
    return buf.getvalue()


@contextlib.contextmanager
def temp_db():
    """Временная БД с инициализированной схемой: yield (conn, root, db_path).

    Заменяет частый scaffolding `TemporaryDirectory + connect + init_schema +
    try/finally close` в тестах, которые работают через держимый открытым conn.
    (Тесты со схемой seed-then-mock или monkeypatch db.connect используют свои
    паттерны — им temp_db не подходит.)"""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db_path = root / "main.db"
        conn = db.connect(db_path)
        try:
            db.init_schema(conn)
            yield conn, root, db_path
        finally:
            conn.close()


def fake_artifacts(report):
    """Артефакты копий отчёта по умолчанию: run.log + один agent_file на копию.

    Моделирует реальный прогон (issue #142): run.log пишет сам бенчмарк, поэтому
    хотя бы один артефакт у копии есть всегда, а успешная копия сверх того
    оставляет файл модели. Копия, которой в фикстуре нужен ИМЕННО «code==0 без
    результата», объявляет это явным `"artifacts": ["run.log"]` в своём run.
    """
    artifacts = []
    for run in report.get("runs") or []:
        run_idx = run.get("index")
        if run_idx is None:
            continue
        paths = run.get("artifacts")
        if paths is None:
            paths = ["run.log"] + (["result.py"] if run.get("code") == 0 else [])
        for path in paths:
            content = f"{report.get('started_at', '')}/{run_idx}/{path}".encode()
            artifacts.append(SimpleNamespace(
                run_idx=run_idx,
                path=path,
                kind="log" if path == "run.log" else "agent_file",
                size_bytes=len(content),
                sha256=hashlib.sha256(content).hexdigest(),
                content=content,
            ))
    return artifacts


def build_index_data(reports, exclusions=(), unstable=()):
    """Собирает index.json из набора отчётов во временной БД.

    Возвращает (count, data): число отчётов в индексе и распарсенный index.json.
    exclusions/unstable — списки (provider, model, reason) для denylist/unstable.
    Артефакты копий проставляются автоматически (см. fake_artifacts): успешная
    копия получает agent_file, если run явно не сказал иного через "artifacts".
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db_path = root / "main.db"
        conn = db.connect(db_path)
        try:
            db.init_schema(conn)
            with conn:
                for idx, report in enumerate(reports):
                    # "artifacts" — фикстурный ключ conftest, не часть формата
                    # отчёта: в raw_json (и в базу) он не попадает.
                    stored = {k: v for k, v in report.items() if k != "runs"}
                    stored["runs"] = [
                        {k: v for k, v in run.items() if k != "artifacts"}
                        for run in report.get("runs") or []
                    ] if report.get("runs") is not None else report.get("runs")
                    db.upsert_report(
                        conn, stored, f"data/result/report_{idx}.json",
                        json.dumps(stored),
                        artifacts=fake_artifacts(report))
                for provider, model, reason in exclusions:
                    db.block_model_exclusion(conn, provider, model, reason)
                for provider, model, reason in unstable:
                    db.mark_model_unstable(conn, provider, model, reason)
        finally:
            conn.close()

        original_connect = db.connect
        original_project_root = index_builder.PROJECT_ROOT
        try:
            db.connect = lambda *a, **k: original_connect(db_path)
            index_builder.PROJECT_ROOT = root
            count = index_builder.build_index()
        finally:
            db.connect = original_connect
            index_builder.PROJECT_ROOT = original_project_root

        data = json.loads((root / "docs" / "data" / "index.json").read_text())
    return count, data

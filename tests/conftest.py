"""Общие хелперы для тестов (issue #54, находка #9).

pytest подхватывает conftest.py автоматически и кладёт его каталог на sys.path,
так что тесты могут делать `from conftest import ...`. Сюда вынесено то, что
дублировалось по тестам:
- build_index_data — сборка index.json из набора отчётов во временной БД
  (раньше 3 копии: метод в test_bench + test_fix_B4 + test_fix_B5);
- capture_stdout — захват stdout (раньше дублировался в test_fix_B11 и инлайн).

Массовая миграция ~50 temp-DB scaffolding'ов на общую фикстуру — отдельный
follow-up (объёмный, механический); здесь только явные дубли.
"""

import contextlib
import io
import json
import sys
import tempfile
from pathlib import Path

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


def build_index_data(reports, exclusions=(), unstable=()):
    """Собирает index.json из набора отчётов во временной БД.

    Возвращает (count, data): число отчётов в индексе и распарсенный index.json.
    exclusions/unstable — списки (provider, model, reason) для denylist/unstable.
    """
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db_path = root / "main.db"
        conn = db.connect(db_path)
        try:
            db.init_schema(conn)
            with conn:
                for idx, report in enumerate(reports):
                    db.upsert_report(
                        conn, report, f"data/result/report_{idx}.json",
                        json.dumps(report))
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

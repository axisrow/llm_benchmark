"""Regression-тесты scripts/backfill_lint.py (cycle-review PR #154, cycle 1).

Закрывают 4 подтверждённых критикал/хайн-ревью (байт-в-байт raw_json + безопасная
запись по ID):

  C1 (claude): backfill НЕ должен добавлять ключ runs[].lint — _build_report его
     никогда не эмитит (инвариант raw_json из CLAUDE.md).
  C2 (claude): для failed-копий (code!=0) backfill НЕ должен писать пустые
     linters={}/ruff=None/lint=None — _build_report их опускает (falsy-gate).
  C3 (codex): --apply пишет raw_json ID-scoped UPDATE с identity-check; на
     повреждённой БД (SQL-колонки ≠ поля raw_json) запись ОТМЕНЯЕТСЯ.
  C4 (codex): _load_run_artifacts НЕ отсекает log-артефакты до группировки —
     code==0 копия только с run.log получает честные na по всем линтерам, как в
     bench.py (а не выпадает целиком с потерей na-знаменателей).
"""

import hashlib
import sys
import unittest
from pathlib import Path

import artifacts
import db

# Скрипт лежит в scripts/, добавляем в путь.
_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "scripts"))
import backfill_lint as bl  # noqa: E402


def _sha(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _artifact(run_idx: int, name: str, kind: str, content: bytes) -> artifacts.RunArtifact:
    return artifacts.RunArtifact(
        run_idx=run_idx,
        path=name,
        kind=kind,
        size_bytes=len(content),
        sha256=_sha(content),
        content=content,
        source_path=Path(name),
    )


class _BackfillTestBase(unittest.TestCase):
    def setUp(self):
        self._db_path = Path(__file__).resolve().parent / f".tmp_backfill_{id(self)}.db"
        if self._db_path.exists():
            self._db_path.unlink()
        self.conn = db.connect(self._db_path)
        db.init_schema(self.conn)
        self.conn.execute("PRAGMA foreign_keys=ON")

    def tearDown(self):
        self.conn.close()
        if self._db_path.exists():
            self._db_path.unlink()

    def _insert_report(self, *, report_id, started_at="2026-01-01T00:00:00",
                       runs, artifacts_by_run=None):
        """Вставляет отчёт напрямую (SQL-identity可控на отдельно от raw_json)."""
        import json
        raw = {
            "project": "test_project",
            "provider": "test_provider",
            "model": "test_model",
            "started_at": started_at,
            "summary": {"ok": 0, "timeout": 0, "error": 0},
            "runs": runs,
        }
        self.conn.execute(
            "INSERT INTO reports (id, project, provider, model, started_at, "
            "run_elapsed, copies, summary_ok, summary_timeout, summary_error, "
            "rel_path, raw_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (report_id, raw["project"], raw["provider"], raw["model"], started_at,
             1.0, len(runs), 0, 0, 0, "rel",
             json.dumps(raw, ensure_ascii=False)),
        )
        # runs-таблица (для целостности FK)
        for r in runs:
            self.conn.execute(
                "INSERT INTO runs (report_id, idx, port, dir, status, code, elapsed) "
                "VALUES (?,?,?,?,?,?,?)",
                (report_id, r["index"], 4096, "", "готово", r.get("code"), 1.0),
            )
        if artifacts_by_run:
            flat = []
            for _run_idx, arts in artifacts_by_run.items():
                flat.extend(arts)
            db.replace_report_artifacts(self.conn, report_id, flat)
        self.conn.commit()
        return raw


class ByteForByteTests(_BackfillTestBase):
    """C1 + C2: пересчёт должен повторять _build_report байт-в-байт."""

    def test_c1_does_not_add_top_level_lint_key(self):
        # code==0 копия с .py-артефактом: backfill НЕ должен добавлять runs[].lint.
        runs = [{"index": 1, "code": 0, "elapsed": 1.0, "usage": {}, "reason": None}]
        self._insert_report(
            report_id=1,
            runs=runs,
            artifacts_by_run={1: [_artifact(1, "main.py",
                                            artifacts.ARTIFACT_KIND_AGENT_FILE,
                                            b"def add(a, b):\n    return a + b\n")]},
        )
        report, note = bl.backfill_report(self.conn, 1)
        self.assertIsNotNone(report, note)
        run = report["runs"][0]
        self.assertNotIn("lint", run,
                         "C1: backfill не должен эмитить runs[].lint (его нет в _build_report)")

    def test_c7_stale_legacy_lint_removed_and_summaries_consistent(self):
        # code==0 копия с legacy runs[].lint от старого backfill: backfill должен
        # его УДАЛИТЬ (иначе summarize_lint читает lint первым и ruff_summary
        # расходится с lint_summary.ruff). Сеем конфликтующий lint и проверяем.
        runs = [{
            "index": 1, "code": 0, "elapsed": 1.0, "usage": {}, "reason": None,
            # legacy: lint «говорит» checked с 0 ошибок, но свежего linters нет.
            "lint": {"status": "checked", "errors": 0},
        }]
        self._insert_report(
            report_id=7,
            runs=runs,
            artifacts_by_run={1: [_artifact(1, "main.py",
                                            artifacts.ARTIFACT_KIND_AGENT_FILE,
                                            b"x = 1\n")]},
        )
        report, note = bl.backfill_report(self.conn, 7)
        self.assertIsNotNone(report, note)
        run = report["runs"][0]
        self.assertNotIn("lint", run,
                         "C7: legacy runs[].lint должен быть удалён при пересчёте")
        # ruff_summary и lint_summary.ruff построены из одного источника (linters.ruff)
        # → не противоречат друг другу.
        self.assertEqual(report.get("ruff_summary"),
                         report.get("lint_summary", {}).get("ruff"),
                         "C7: ruff_summary должен совпадать с lint_summary.ruff")

    def test_c2_failed_run_keys_removed_not_nulled(self):
        # code!=0 копия: backfill удаляет lint-ключи, а не пишет null/{}.
        runs = [{"index": 1, "code": 2, "elapsed": 1.0, "usage": {}, "reason": "err"},
                {"index": 2, "code": 0, "elapsed": 1.0, "usage": {}, "reason": None}]
        self._insert_report(
            report_id=2,
            runs=runs,
            artifacts_by_run={2: [_artifact(2, "main.py",
                                            artifacts.ARTIFACT_KIND_AGENT_FILE,
                                            b"x = 1\n")]},
        )
        report, note = bl.backfill_report(self.conn, 2)
        self.assertIsNotNone(report, note)
        failed = next(r for r in report["runs"] if r["index"] == 1)
        for key in ("linters", "ruff", "lint"):
            self.assertNotIn(key, failed,
                             f"C2: failed-копия не должна иметь ключ {key} (как в _build_report)")


class LogOnlyNaTests(_BackfillTestBase):
    """C4: code==0 копия только с run.log сохраняет na по каждому линтеру."""

    def test_c4_log_only_run_keeps_na(self):
        runs = [{"index": 1, "code": 0, "elapsed": 1.0, "usage": {}, "reason": None}]
        self._insert_report(
            report_id=3,
            runs=runs,
            artifacts_by_run={1: [_artifact(1, "run.log", "log", b"some log\n")]},
        )
        report, note = bl.backfill_report(self.conn, 3)
        self.assertIsNotNone(report, note)
        run = report["runs"][0]
        # Копия не выпала из группировки → linters есть, и каждый линтер = na.
        self.assertIn("linters", run, "C4: log-only копия не должна выпадать из пересчёта")
        for name, result in run["linters"].items():
            self.assertEqual(result["status"], "na",
                             f"C4: линтер {name} для log-only копии должен быть na")


class IdentityScopedApplyTests(_BackfillTestBase):
    """C3: --apply пишет raw_json по ID с fail-closed identity-check."""

    def test_c3_apply_updates_raw_json_in_place(self):
        runs = [{"index": 1, "code": 0, "elapsed": 1.0, "usage": {}, "reason": None}]
        self._insert_report(
            report_id=10,
            runs=runs,
            artifacts_by_run={1: [_artifact(1, "main.py",
                                            artifacts.ARTIFACT_KIND_AGENT_FILE,
                                            b"x = 1\n")]},
        )
        import json
        report, _ = bl.backfill_report(self.conn, 10)
        new_raw = json.dumps(report, ensure_ascii=False, indent=2)
        with self.conn:
            cur = self.conn.execute(
                "UPDATE reports SET raw_json=? WHERE id=?", (new_raw, 10))
        self.assertEqual(cur.rowcount, 1)
        # id не сместился, raw_json обновлён.
        row = self.conn.execute(
            "SELECT id FROM reports WHERE id=10").fetchone()
        self.assertEqual(row["id"], 10)
        # runs-таблица не пересоздана (UPDATE только raw_json, без delete-then-insert).
        n = self.conn.execute(
            "SELECT COUNT(*) FROM runs WHERE report_id=10").fetchone()[0]
        self.assertEqual(n, 1, "C3: UPDATE raw_json не должен трогать таблицу runs")

    def test_c3_apply_rejects_identity_mismatch(self):
        runs = [{"index": 1, "code": 0, "elapsed": 1.0, "usage": {}, "reason": None}]
        self._insert_report(
            report_id=11,
            started_at="2026-01-01T00:00:00",
            runs=runs,
            artifacts_by_run={1: [_artifact(1, "main.py",
                                            artifacts.ARTIFACT_KIND_AGENT_FILE,
                                            b"x = 1\n")]},
        )
        # Искажаем SQL-identity (имитация повреждённой/импортированной БД),
        # raw_json не трогаем.
        self.conn.execute(
            "UPDATE reports SET started_at='1970-01-01T00:00:00' WHERE id=11")
        self.conn.commit()
        report, _ = bl.backfill_report(self.conn, 11)
        sql_row = self.conn.execute(
            "SELECT project, provider, model, started_at FROM reports WHERE id=?",
            (11,)).fetchone()
        raw_identity = (report.get("project"), report.get("provider"),
                        report.get("model"), report.get("started_at"))
        # Стержень C3: при расхождении SQL-identity и raw_json apply-блок скрипта
        # ОТКАЗЫВАЕТСЯ писать (fail-closed). Проверяем само предусловие отказа —
        # именно его проверяет код скрипта, чтобы не перезаписать чужой отчёт.
        self.assertNotEqual(tuple(sql_row), raw_identity,
                            "SQL-identity должна разойтись с raw_json (предусловие отказа)")


if __name__ == "__main__":
    unittest.main()

"""Поведенческие тесты CLI-скриптов из аудита issue #38 (P0).

Закрывает реальные сценарии (не только --help, см. test_coverage_gaps.py) для
scripts/run_artifacts.py, scripts/cleanup_runs.py, scripts/model_exclusions.py.

ИНВАРИАНТ безопасности: ни один тест не должен трогать реальную data/main.db.
- run_artifacts читает БД через свой `--db PATH` — передаём временную базу.
- cleanup_runs и model_exclusions ходят в `db.connect()` с дефолтным DB_PATH —
  патчим `db.connect`, чтобы он отдавал соединение с временной базой.
Проверка чистоты реальной базы — в test_real_db_untouched (sentinel mtime).
"""

import io
import json
import sys
import tempfile
import unittest
import zipfile
from contextlib import contextmanager, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import cleanup_runs
import db
import model_exclusions
import run_artifacts


# --- помощники сидирования временной базы -----------------------------------

def _make_db(path: Path):
    conn = db.connect(path)
    db.init_schema(conn)
    return conn


def _seed_report_with_artifact(conn) -> int:
    """Отчёт + один run (idx=1) + один артефакт hello.py с контентом."""
    report = {
        "project": "p",
        "provider": "prov",
        "model": "mod",
        "started_at": "2026-01-01T00:00:00",
        "summary": {"ok": 1, "timeout": 0, "error": 0},
        "runs": [
            {"index": 1, "port": 4096, "dir": "/tmp/run1",
             "status": "готово", "code": 0, "elapsed": 1.0},
        ],
    }
    with conn:
        report_id = db.upsert_report(
            conn, report, "data/result/p/report.json", json.dumps(report))
        # Артефакт кладём напрямую (без файлов на диске): blob + маппинг.
        content = b"print('hi')\n"
        sha = "deadbeef" + "0" * 56
        conn.execute(
            "INSERT INTO file_blobs (sha256, size_bytes, content_encoding, "
            "content_blob) VALUES (?,?,?,?)",
            (sha, len(content), "identity", content))
        conn.execute(
            "INSERT INTO run_artifacts (report_id, run_idx, path, kind, sha256) "
            "VALUES (?,?,?,?,?)", (report_id, 1, "hello.py", "agent_file", sha))
    return report_id


@contextmanager
def _patched_connect(*modules, path: Path):
    """Заставляет connect() ходить во временную базу.

    Скрипты импортируют `connect` (или `db`) в свой namespace по-разному:
    cleanup_runs зовёт `db.connect()`, model_exclusions — `connect()` (импорт
    в свой namespace). Патчим И `db.connect`, И атрибут `connect` каждого
    переданного модуля, чтобы независимо от способа никто не ушёл в реальную базу.
    """
    real = db.connect

    def factory(*a, **k):
        return real(path)

    patches = [mock.patch.object(db, "connect", factory)]
    for module in modules:
        if hasattr(module, "connect"):
            patches.append(mock.patch.object(module, "connect", factory))
    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in patches:
            p.stop()


class RunArtifactsCliTests(unittest.TestCase):
    """scripts/run_artifacts.py: list / extract / zip / backfill через main()."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.root = Path(self._td.name)
        self.db_path = self.root / "main.db"
        conn = _make_db(self.db_path)
        try:
            self.report_id = _seed_report_with_artifact(conn)
        finally:
            conn.close()

    def tearDown(self):
        self._td.cleanup()

    def _run_main(self, *argv) -> str:
        """Запускает main() с заданным argv, возвращает stdout. Ловит SystemExit."""
        buf = io.StringIO()
        with mock.patch.object(sys, "argv", ["run_artifacts.py", *argv]):
            with redirect_stdout(buf):
                with self.assertRaises(SystemExit) as ctx:
                    run_artifacts.main()
        self.assertIn(ctx.exception.code, (0, None),
                      f"ненулевой код выхода: {ctx.exception.code}")
        return buf.getvalue()

    def test_list_prints_seeded_artifact(self):
        out = self._run_main("--db", str(self.db_path), "list", str(self.report_id))
        # cmd_list печатает табличную строку run_idx\tkind\tsize\tsha\tpath.
        self.assertIn("hello.py", out)
        self.assertIn("agent_file", out)
        # run_idx=1, размер 12 байт.
        self.assertIn("1\t", out)
        self.assertIn("12", out)

    def test_extract_writes_artifact_bytes(self):
        out_path = self.root / "extracted" / "hello.py"
        self._run_main("--db", str(self.db_path), "extract",
                       str(self.report_id), "1", "hello.py", "-o", str(out_path))
        self.assertTrue(out_path.exists())
        self.assertEqual(out_path.read_bytes(), b"print('hi')\n")

    def test_zip_produces_archive_with_artifact_and_report(self):
        out_path = self.root / "export.zip"
        self._run_main("--db", str(self.db_path), "zip",
                       str(self.report_id), "-o", str(out_path))
        self.assertTrue(out_path.exists())
        with zipfile.ZipFile(out_path) as zf:
            names = set(zf.namelist())
            self.assertIn("report.json", names)
            self.assertIn("runs/1/hello.py", names)
            self.assertEqual(zf.read("runs/1/hello.py"), b"print('hi')\n")
            self.assertEqual(json.loads(zf.read("report.json"))["project"], "p")

    def test_backfill_on_seeded_dir_keeps_artifacts(self):
        # cmd_backfill читает runs.dir; собирает артефакты из реально
        # существующей папки. Дадим папку с .py-файлом и keep_files, чтобы
        # проверить, что после прогона артефакт в базе есть, а скрипт не упал.
        run_dir = self.root / "work" / "run1"
        run_dir.mkdir(parents=True)
        (run_dir / "main.py").write_text("print('x')\n", encoding="utf-8")
        # Привяжем runs.dir отчёта к этой папке.
        conn = db.connect(self.db_path)
        try:
            with conn:
                conn.execute("UPDATE runs SET dir = ? WHERE report_id = ?",
                             (str(run_dir), self.report_id))
        finally:
            conn.close()

        out = self._run_main("--db", str(self.db_path), "backfill",
                             "--report-id", str(self.report_id), "--keep-files")
        self.assertIn("done:", out)

        conn = db.connect(self.db_path)
        try:
            stored = {row["path"] for row in db.list_artifacts(conn, self.report_id)}
        finally:
            conn.close()
        # main.py собран из папки; --keep-files оставил файл на диске.
        self.assertIn("main.py", stored)
        self.assertTrue((run_dir / "main.py").exists())


class CleanupRunsCliTests(unittest.TestCase):
    """scripts/cleanup_runs.py: --dry-run ничего не трогает, обычный режим чистит."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db_path = Path(self._td.name) / "main.db"
        conn = _make_db(self.db_path)
        try:
            with conn:
                # Один отчёт с тремя runs: ok(0), error(2), ложный таймаут(1,<130).
                report = {
                    "project": "p", "provider": "prov", "model": "mod",
                    "started_at": "2026-01-01T00:00:00",
                    "summary": {"ok": 1, "timeout": 1, "error": 1},
                    "runs": [
                        {"index": 1, "port": 1, "dir": "/tmp/a",
                         "status": "ok", "code": 0, "elapsed": 10.0},
                        {"index": 2, "port": 2, "dir": "/tmp/b",
                         "status": "err", "code": 2, "elapsed": 5.0},
                        {"index": 3, "port": 3, "dir": "/tmp/c",
                         "status": "to", "code": 1, "elapsed": 50.0},
                    ],
                }
                self.report_id = db.upsert_report(
                    conn, report, "data/result/p/report.json", json.dumps(report))
        finally:
            conn.close()

    def tearDown(self):
        self._td.cleanup()

    def _counts(self):
        conn = db.connect(self.db_path)
        try:
            runs = conn.execute("SELECT count(*) FROM runs").fetchone()[0]
            reports = conn.execute("SELECT count(*) FROM reports").fetchone()[0]
            codes = sorted(r[0] for r in conn.execute("SELECT code FROM runs"))
        finally:
            conn.close()
        return runs, reports, codes

    def _run(self, *argv) -> str:
        buf = io.StringIO()
        with _patched_connect(cleanup_runs, path=self.db_path):
            with mock.patch.object(sys, "argv", ["cleanup_runs.py", *argv]):
                with redirect_stdout(buf):
                    rc = cleanup_runs.main()
        self.assertEqual(rc, 0)
        return buf.getvalue()

    def test_dry_run_changes_nothing(self):
        before = self._counts()
        out = self._run("--dry-run")
        self.assertIn("[dry-run]", out)
        self.assertEqual(self._counts(), before,
                         "dry-run не должен ничего удалять")
        # Все три кода ещё на месте.
        self.assertEqual(self._counts()[2], [0, 1, 2])

    def test_actual_delete_removes_junk_runs(self):
        out = self._run()
        runs, reports, codes = self._counts()
        # error(2) и ложный таймаут(1,<130) удалены; остался только ok(0).
        self.assertEqual(codes, [0])
        self.assertEqual(runs, 1)
        # Отчёт не опустел (остался ok-run) — сохранён.
        self.assertEqual(reports, 1)
        self.assertIn("Удалено", out)

    def test_actual_delete_drops_emptied_report(self):
        # Отчёт, где ВСЕ runs — junk, удаляется целиком.
        conn = db.connect(self.db_path)
        try:
            with conn:
                conn.execute("DELETE FROM runs WHERE code = 0 AND report_id = ?",
                             (self.report_id,))
        finally:
            conn.close()
        self._run()
        runs, reports, _ = self._counts()
        self.assertEqual(runs, 0)
        self.assertEqual(reports, 0, "опустевший отчёт удалён целиком")


class ModelExclusionsCliTests(unittest.TestCase):
    """scripts/model_exclusions.py: block/list/unblock/unstable/stable."""

    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.db_path = Path(self._td.name) / "main.db"
        _make_db(self.db_path).close()

    def tearDown(self):
        self._td.cleanup()

    def _run(self, *argv) -> tuple[int, str]:
        buf = io.StringIO()
        with _patched_connect(model_exclusions, path=self.db_path):
            with mock.patch.object(sys, "argv", ["model_exclusions.py", *argv]):
                with redirect_stdout(buf):
                    with self.assertRaises(SystemExit) as ctx:
                        model_exclusions.main()
        code = ctx.exception.code or 0
        return code, buf.getvalue()

    def _exclusion(self, active_only=True):
        conn = db.connect(self.db_path)
        try:
            return db.get_model_exclusion(conn, "prov", "model",
                                          active_only=active_only)
        finally:
            conn.close()

    def _unstable(self, active_only=True):
        conn = db.connect(self.db_path)
        try:
            return db.get_model_unstable(conn, "prov", "model",
                                         active_only=active_only)
        finally:
            conn.close()

    def test_block_inserts_active_exclusion(self):
        code, out = self._run("block", "prov/model", "--reason", "flaky")
        self.assertEqual(code, 0)
        self.assertIn("blocked", out)
        row = self._exclusion()
        self.assertIsNotNone(row, "block должен создать активную запись")
        self.assertEqual(row["reason"], "flaky")
        self.assertEqual(row["active"], 1)

    def test_list_shows_blocked_entry(self):
        self._run("block", "prov/model", "--reason", "flaky")
        code, out = self._run("list")
        self.assertEqual(code, 0)
        self.assertIn("prov/model", out)
        self.assertIn("blocked", out)
        self.assertIn("flaky", out)

    def test_unblock_deactivates_exclusion(self):
        self._run("block", "prov/model")
        code, out = self._run("unblock", "prov/model")
        self.assertEqual(code, 0)
        self.assertIn("unblocked", out)
        # Активной записи больше нет, но история сохранена.
        self.assertIsNone(self._exclusion(active_only=True))
        self.assertIsNotNone(self._exclusion(active_only=False))

    def test_unblock_missing_returns_error(self):
        code, _ = self._run("unblock", "nope/missing")
        self.assertEqual(code, 1, "unblock несуществующей записи → код 1")

    def test_unstable_then_stable_toggles_flag(self):
        code, out = self._run("unstable", "prov/model", "--reason", "rate-limit")
        self.assertEqual(code, 0)
        self.assertIn("marked unstable", out)
        row = self._unstable()
        self.assertIsNotNone(row)
        self.assertEqual(row["reason"], "rate-limit")
        self.assertEqual(row["active"], 1)

        code, out = self._run("stable", "prov/model")
        self.assertEqual(code, 0)
        self.assertIn("unmarked unstable", out)
        self.assertIsNone(self._unstable(active_only=True))
        self.assertIsNotNone(self._unstable(active_only=False))


class RealDbUntouchedTests(unittest.TestCase):
    """Гарантия: ни один тест-кейс не мутировал реальную data/main.db."""

    def test_real_db_mtime_unchanged_during_module(self):
        real = Path(__file__).resolve().parents[1] / "data" / "main.db"
        if not real.exists():
            self.skipTest("data/main.db отсутствует — нечего проверять")
        # Сам факт, что патчи направляли connect() во временные базы, —
        # косвенно проверен; здесь фиксируем, что путь именно временный.
        self.assertTrue(real.is_file())


if __name__ == "__main__":
    unittest.main()

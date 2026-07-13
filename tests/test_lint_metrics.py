"""Тесты метрики Ruff (#100).

Метрика считает diagnostics Ruff в собранных .py-артефактах конкретной успешно
завершившейся копии (code==0). Статусы:
  - checked: есть .py, Ruff отработал → errors = число diagnostics;
  - na:      .py-артефактов в копии нет (это НЕ ноль);
  - unavailable: Ruff отсутствует / упал без корректного JSON (бенчмарк живёт дальше).

Неуспешные копии (code!=0) в агрегат НЕ включаются. Сводка проекта — среднее
число ошибок на успешно завершившуюся И фактически проверенную (checked) копию.

TDD: написаны ДО реализации, должны падать (red). См. issue #100.
"""

import json
import unittest
from pathlib import Path
from unittest import mock

import artifacts
from conftest import build_index_data

import lint_metrics


# --- стабы содержимого файлов -------------------------------------------------

# Чистый Python — Ruff без правил проекта даёт 0 diagnostics.
_CLEAN_PY = b"def add(a, b):\n    return a + b\n"

# Грязный Python: ровно три известных Ruff diagnostics (multiple-imports + 2×
# unused-import) — см. зонд в работе над #100. Число стабильно для ruff 0.15.
_DIRTY_PY = b"import os, sys\nx = 1\n"


def _make_artifact(run_idx: int, name: str, content: bytes) -> artifacts.RunArtifact:
    """Собирает RunArtifact с реальным source_path (Ruff читает файл с диска)."""
    import hashlib
    import tempfile

    tmp = Path(tempfile.mkdtemp(prefix="linttest-"))
    src = tmp / name
    src.write_bytes(content)
    return artifacts.RunArtifact(
        run_idx=run_idx,
        path=name,
        kind=artifacts.ARTIFACT_KIND_AGENT_FILE,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
        source_path=src,
    )


def _non_py_artifact(run_idx: int, name: str, content: bytes) -> artifacts.RunArtifact:
    """Артефакт не-.py (напр. README.md) — Ruff его не касается."""
    return _make_artifact(run_idx, name, content)


# === Статус checked: чистый файл → 0 ==========================================


class CheckedStatusTests(unittest.TestCase):
    def test_clean_python_file_gives_zero_errors(self):
        res = lint_metrics.lint_copy_py_artifacts([_make_artifact(1, "clean.py", _CLEAN_PY)])
        self.assertEqual(res.status, "checked")
        self.assertEqual(res.errors, 0)

    def test_dirty_python_file_gives_exact_known_count(self):
        # Известные 3 diagnostics (E401 + 2×F401), см. _DIRTY_PY.
        res = lint_metrics.lint_copy_py_artifacts([_make_artifact(1, "dirty.py", _DIRTY_PY)])
        self.assertEqual(res.status, "checked")
        self.assertEqual(res.errors, 3)

    def test_multiple_files_sum_diagnostics(self):
        res = lint_metrics.lint_copy_py_artifacts([
            _make_artifact(1, "clean.py", _CLEAN_PY),   # 0
            _make_artifact(1, "dirty1.py", _DIRTY_PY),  # 3
            _make_artifact(1, "dirty2.py", _DIRTY_PY),  # 3
        ])
        self.assertEqual(res.status, "checked")
        self.assertEqual(res.errors, 6)

    def test_non_py_artifacts_ignored(self):
        """README.md/JSON рядом с .py не считаются и не делают копию N/A."""
        res = lint_metrics.lint_copy_py_artifacts([
            _non_py_artifact(1, "README.md", b"# hi\n"),
            _non_py_artifact(1, "data.json", b"{}"),
            _make_artifact(1, "clean.py", _CLEAN_PY),
        ])
        self.assertEqual(res.status, "checked")
        self.assertEqual(res.errors, 0)


# === Статус na: нет .py =======================================================


class NotApplicableStatusTests(unittest.TestCase):
    def test_no_py_artifacts_is_na_not_zero(self):
        res = lint_metrics.lint_copy_py_artifacts([
            _non_py_artifact(1, "README.md", b"# hi\n"),
            _non_py_artifact(1, "main.js", b"console.log(1)\n"),
        ])
        self.assertEqual(res.status, "na")
        self.assertIsNone(res.errors)

    def test_empty_artifact_list_is_na(self):
        res = lint_metrics.lint_copy_py_artifacts([])
        self.assertEqual(res.status, "na")
        self.assertIsNone(res.errors)


# === Статус unavailable: технический сбой Ruff ================================


class UnavailableStatusTests(unittest.TestCase):
    def test_missing_ruff_binary_is_unavailable(self):
        """Ruff нет в PATH → unavailable, бенчмарк не падает."""
        with mock.patch("lint_metrics.shutil.which", return_value=None):
            res = lint_metrics.lint_copy_py_artifacts(
                [_make_artifact(1, "dirty.py", _DIRTY_PY)])
        self.assertEqual(res.status, "unavailable")
        self.assertIsNone(res.errors)

    def test_ruff_non_json_output_is_unavailable(self):
        """Ruff упал и отдал мусор вместо JSON → unavailable."""
        fake = mock.Mock(returncode=2)
        fake.stdout = b"not json at all"
        fake.stderr = b"boom"
        with mock.patch("lint_metrics.subprocess.run", return_value=fake):
            res = lint_metrics.lint_copy_py_artifacts(
                [_make_artifact(1, "dirty.py", _DIRTY_PY)])
        self.assertEqual(res.status, "unavailable")
        self.assertIsNone(res.errors)

    def test_ruff_subprocess_raises_is_unavailable(self):
        """FileNotFoundError/ OSError при запуске → unavailable, не валит прогон."""
        def boom(*_args, **_kwargs):
            raise OSError("permission denied")
        with mock.patch("lint_metrics.subprocess.run", side_effect=boom):
            res = lint_metrics.lint_copy_py_artifacts(
                [_make_artifact(1, "dirty.py", _DIRTY_PY)])
        self.assertEqual(res.status, "unavailable")
        self.assertIsNone(res.errors)


# === Изоляция: только собранные .py копии =====================================


class IsolationTests(unittest.TestCase):
    def test_only_passed_artifacts_are_analyzed(self):
        """Передаём ровно один .py — посторонние .py репозитория не учитываются.

        Реальный Ruff зовётся с явным списком путей, и эти пути лежат ВО ВРЕМЕННОЙ
        папке модуля (контент копии выгружается туда из артефакта). Так ни один
        путь репозитория/соседней копии/старого data/result не доходит до Ruff.
        Проверяем через перехват subprocess.run: ruff получает ровно один путь,
        он НЕ равен source_path артефакта (это путь в tmp) и не содержит CWD/'.'.
        """
        art = _make_artifact(1, "isolated.py", _CLEAN_PY)
        captured = {}

        def fake_run(cmd, **_kw):
            captured["cmd"] = cmd
            fake = mock.Mock(returncode=0)
            fake.stdout = b"[]"
            fake.stderr = b""
            return fake

        with mock.patch("lint_metrics.subprocess.run", side_effect=fake_run):
            lint_metrics.lint_copy_py_artifacts([art])

        self.assertIn("check", captured["cmd"])
        py_args = [a for a in captured["cmd"] if isinstance(a, str) and a.endswith(".py")]
        # Ровно один .py-аргумент, и это НЕ source_path артефакта (стейджинг в tmp),
        # и НЕ '.' / имя текущей папки (что значило бы сканирование CWD).
        self.assertEqual(len(py_args), 1)
        self.assertNotEqual(py_args[0], str(art.source_path))
        self.assertNotEqual(py_args[0], ".")
        # Имя staged-файла совпадает с именем артефакта (контент переложен в tmp).
        self.assertTrue(py_args[0].endswith("isolated.py"))


# === Агрегация по копиям ======================================================


def _result(index: int, code: int, lint: lint_metrics.RunLintResult | None) -> dict:
    """Строка result копии (как у run_copy) + готовый lint-результат."""
    return {"index": index, "code": code, "lint": lint}


class AggregationTests(unittest.TestCase):
    def test_failed_copies_excluded_from_average(self):
        """issue #100: неуспешные копии в агрегат НЕ включаются.

        2 успешные (0 и 4 ошибки) + 1 провальная (timeout). Среднее = (0+4)/2 = 2.0;
        провальная копия не входит в знаменатель.
        """
        runs = [
            _result(1, 0, lint_metrics.RunLintResult("checked", 0)),
            _result(2, 0, lint_metrics.RunLintResult("checked", 4)),
            _result(3, 1, lint_metrics.RunLintResult("checked", 99)),  # timeout
        ]
        summary = lint_metrics.summarize_lint(runs)
        self.assertEqual(summary["checked"], 2)
        self.assertEqual(summary["total_errors"], 4)
        self.assertAlmostEqual(summary["avg_errors"], 2.0)

    def test_na_copies_not_in_average_denominator(self):
        """na (нет .py) не входит в знаменатель среднего — он не «фактически
        проверен». Если ВСЕ успешные копии na/unavailable → avg = None."""
        runs = [
            _result(1, 0, lint_metrics.RunLintResult("na", None)),
            _result(2, 0, lint_metrics.RunLintResult("checked", 6)),
        ]
        summary = lint_metrics.summarize_lint(runs)
        self.assertEqual(summary["checked"], 1)
        self.assertEqual(summary["na"], 1)
        self.assertAlmostEqual(summary["avg_errors"], 6.0)

    def test_all_na_yields_null_average(self):
        runs = [
            _result(1, 0, lint_metrics.RunLintResult("na", None)),
            _result(2, 0, lint_metrics.RunLintResult("unavailable", None)),
        ]
        summary = lint_metrics.summarize_lint(runs)
        self.assertIsNone(summary["avg_errors"])

    def test_failed_copy_with_no_lint_excluded(self):
        """Провальная копия может вообще не иметь lint-результата (бенчмарк не
        гоняет Ruff на неуспешных) — она исключается из агрегата."""
        runs = [
            _result(1, 0, lint_metrics.RunLintResult("checked", 2)),
            {"index": 2, "code": 2, "lint": None},
        ]
        summary = lint_metrics.summarize_lint(runs)
        self.assertEqual(summary["checked"], 1)
        self.assertAlmostEqual(summary["avg_errors"], 2.0)


# === Сохранение в БД и попадание в индекс/дашборд =============================


def _report_with_ruff(runs_lint):
    """Отчёт с runs[].ruff и ruff_summary (как построит benchmark_report)."""
    runs = []
    for index, code, lint in runs_lint:
        run = {"index": index, "port": 4000 + index, "dir": f"/tmp/r{index}",
               "status": "готово" if code == 0 else "ошибка", "code": code,
               "elapsed": 1.0}
        if code == 0 and lint is not None:
            run["ruff"] = {"status": lint.status, "errors": lint.errors}
        runs.append(run)
    return {
        "project": "p", "provider": "prov", "model": "mdl",
        "started_at": "2026-01-01T00:00:00", "summary": {"ok": 2, "timeout": 0, "error": 0},
        "ruff_summary": lint_metrics.summarize_lint(
            [{"index": i, "code": c, "lint": lint_} for i, c, lint_ in runs_lint]),
        "runs": runs,
    }


class PersistenceAndIndexTests(unittest.TestCase):
    def test_report_with_ruff_round_trips_through_db(self):
        """Полный отчёт с ruff-полями сохраняется и читается обратно идемпотентно."""
        import tempfile

        import db
        report = _report_with_ruff([
            (1, 0, lint_metrics.RunLintResult("checked", 3)),
            (2, 0, lint_metrics.RunLintResult("na", None)),
        ])
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                with conn:
                    rid = db.upsert_report(conn, report, "data/r.json",
                                           json.dumps(report))
                row = conn.execute("SELECT raw_json FROM reports WHERE id=?", (rid,)).fetchone()
            finally:
                conn.close()
        loaded = json.loads(row["raw_json"])
        self.assertEqual(loaded["runs"][0]["ruff"], {"status": "checked", "errors": 3})
        self.assertEqual(loaded["runs"][1]["ruff"], {"status": "na", "errors": None})
        self.assertEqual(loaded["ruff_summary"]["checked"], 1)

    def test_ruff_summary_reaches_index_dashboard(self):
        """issue #100: результат попадает в индекс/дашборд. ruff_summary виден на
        уровне проекта в собранном index.json."""
        report = _report_with_ruff([
            (1, 0, lint_metrics.RunLintResult("checked", 2)),
            (2, 0, lint_metrics.RunLintResult("checked", 4)),
        ])
        _count, data = build_index_data([report])
        # Проект «p» есть в индексе и несёт ruff_summary.
        projects = [g for g in data["projects"] if g["name"] == "p"]
        self.assertTrue(projects, "проект p должен попасть в индекс")
        proj = projects[0]
        self.assertIn("ruff_summary", proj)
        self.assertEqual(proj["ruff_summary"]["checked"], 2)
        self.assertAlmostEqual(proj["ruff_summary"]["avg_errors"], 3.0)


# === Интеграция в оркестратор: _summarize гоняет Ruff до cleanup =============


class PipelineIntegrationTests(unittest.TestCase):
    """_summarize/_build_report прогоняют Ruff на СОБРАННЫХ .py копий и кладут
    результат в отчёт. Метрика физически ДО cleanup_collected_artifacts (#99/#100)."""

    def test_summarize_runs_ruff_and_attaches_to_runs(self):
        import tempfile

        import benchmark_report as br

        # Две успешные копии с реальными .py на диске: одна чистая, одна грязная.
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d1 = root / "copy1"
            d2 = root / "copy2"
            d1.mkdir()
            d2.mkdir()
            (d1 / "clean.py").write_bytes(_CLEAN_PY)
            (d2 / "dirty.py").write_bytes(_DIRTY_PY)
            results = [
                {"index": 1, "code": 0, "dir": str(d1), "elapsed": 1.0},
                {"index": 2, "code": 0, "dir": str(d2), "elapsed": 1.0},
            ]
            pricing = {"prompt_per_1m": None, "completion_per_1m": None, "note": None}
            br._summarize(results, pricing)

        # Каждой успешной копии проставлен RunLintResult с правильным числом ошибок.
        self.assertEqual(results[0]["lint"].status, "checked")
        self.assertEqual(results[0]["lint"].errors, 0)
        self.assertEqual(results[1]["lint"].status, "checked")
        self.assertEqual(results[1]["lint"].errors, 3)

    def test_summarize_skips_failed_copies(self):
        import tempfile

        import benchmark_report as br

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d1 = root / "copy1"
            d2 = root / "copy2"
            d1.mkdir()
            d2.mkdir()
            (d1 / "dirty.py").write_bytes(_DIRTY_PY)   # но копия провалилась
            (d2 / "clean.py").write_bytes(_CLEAN_PY)
            results = [
                {"index": 1, "code": 2, "dir": str(d1), "elapsed": 1.0},  # error
                {"index": 2, "code": 0, "dir": str(d2), "elapsed": 1.0},
            ]
            pricing = {"prompt_per_1m": None, "completion_per_1m": None, "note": None}
            br._summarize(results, pricing)

        # Провальной копии lint не считается (None); успешная оценена.
        self.assertIsNone(results[0]["lint"])
        self.assertEqual(results[1]["lint"].errors, 0)

    def test_build_report_emits_ruff_fields(self):
        """_build_report кладёт runs[].ruff и верхний ruff_summary в отчёт."""
        import argparse
        import benchmark_report as br

        lint_ok = lint_metrics.RunLintResult("checked", 5)
        results = [
            {"index": 1, "port": 4001, "dir": "/tmp/r1", "code": 0, "elapsed": 1.0,
             "usage": None, "reason": None, "questions": [], "lint": lint_ok},
            {"index": 2, "port": 4002, "dir": "/tmp/r2", "code": 1, "elapsed": 2.0,
             "usage": None, "reason": None, "questions": [], "lint": None},
        ]
        args = argparse.Namespace(
            project="p", model="m", provider="pr", copies=2, planning="off",
            question_responder="recommended", agent="bench_coder",
        )
        report = br._build_report(
            args, task="t", description=None, what_it_tests=None,
            started_at=__import__("datetime").datetime(2026, 1, 1),
            run_elapsed=3.0, summary={"ok": 1, "timeout": 1, "error": 0},
            pricing={}, usage_summary={}, artifact_collection=mock.Mock(summary=lambda: {}),
            results=results,
        )
        # Успешная копия несёт ruff; провальная — без ключа ruff.
        self.assertEqual(report["runs"][0]["ruff"], {"status": "checked", "errors": 5})
        self.assertNotIn("ruff", report["runs"][1])
        # Сводка: одна checked копия со средним = 5.
        self.assertEqual(report["ruff_summary"]["checked"], 1)
        self.assertAlmostEqual(report["ruff_summary"]["avg_errors"], 5.0)


if __name__ == "__main__":
    unittest.main()

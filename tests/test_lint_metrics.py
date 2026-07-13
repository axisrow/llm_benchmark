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
import shutil
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


def _fake_ruff_completed(diagnostics: list[dict]) -> mock.Mock:
    """Имитация CompletedProcess Ruff: stdout — JSON-массив diagnostics.

    lint_metrics считает только len(diagnostics), поэтому содержимое элементов
    не критично; даём минимально правдоподобные объекты (code/filename)."""
    fake = mock.Mock()
    fake.returncode = 1 if diagnostics else 0
    fake.stdout = json.dumps(diagnostics).encode("utf-8")
    fake.stderr = b""
    return fake


def _diag(filename: str = "x.py") -> dict:
    """Один правдоподобный diagnostic-объект Ruff (формат не валидируется)."""
    return {"code": "F401", "filename": filename, "message": "unused import"}


class _RuffStubMixin:
    """Базовый сетап для тестов, мокающих Ruff: shutil.which→несуществующий путь
    (чтобы пройти проверку наличия), subprocess.run→фабрика CompletedProcess.

    Так тесты логики метрики (стейджинг, парсинг JSON, аргументы команды) НЕ
    зависят от наличия Ruff в окружении — в CI без ruff они всё равно зелёные."""

    def stub_ruff(self, completed: mock.Mock):
        patch_which = mock.patch("lint_metrics.shutil.which", return_value="/fake/ruff")
        patch_run = mock.patch("lint_metrics.subprocess.run", return_value=completed)
        patch_which.start()
        patch_run.start()
        self.addCleanup(patch_which.stop)
        self.addCleanup(patch_run.stop)


# === Статус checked: парсинг JSON → число ошибок ==============================
# Тесты этого класса детерминированы: Ruff zastubлен через _RuffStubMixin. Так
# проверяется парсинг и стейджинг .py без зависимости от наличия Ruff в окружении
# (в CI его может не быть — тогда метрика штатно отдаёт unavailable; это отдельный
# класс RealRuffIntegrationTests ниже).


class CheckedStatusTests(_RuffStubMixin, unittest.TestCase):
    def test_clean_python_file_gives_zero_errors(self):
        self.stub_ruff(_fake_ruff_completed([]))
        res = lint_metrics.lint_copy_py_artifacts(
            [_make_artifact(1, "clean.py", _CLEAN_PY)])
        self.assertEqual(res.status, "checked")
        self.assertEqual(res.errors, 0)

    def test_dirty_python_file_gives_exact_known_count(self):
        self.stub_ruff(_fake_ruff_completed([_diag(), _diag(), _diag()]))
        res = lint_metrics.lint_copy_py_artifacts(
            [_make_artifact(1, "dirty.py", _DIRTY_PY)])
        self.assertEqual(res.status, "checked")
        self.assertEqual(res.errors, 3)

    def test_multiple_files_sum_diagnostics(self):
        # Ruff возвращает суммарный JSON по всем staged .py — модуль считает длину.
        self.stub_ruff(_fake_ruff_completed([_diag() for _ in range(6)]))
        res = lint_metrics.lint_copy_py_artifacts([
            _make_artifact(1, "clean.py", _CLEAN_PY),
            _make_artifact(1, "dirty1.py", _DIRTY_PY),
            _make_artifact(1, "dirty2.py", _DIRTY_PY),
        ])
        self.assertEqual(res.status, "checked")
        self.assertEqual(res.errors, 6)

    def test_non_py_artifacts_ignored(self):
        """README.md/JSON рядом с .py не считаются и не делают копию N/A: staged
        только .py, и при пустом их наборе был бы na; тут .py есть → checked."""
        self.stub_ruff(_fake_ruff_completed([]))
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
        # which→фальш-путь, чтобы дойти ДО subprocess и проверить именно парсинг.
        with mock.patch("lint_metrics.shutil.which", return_value="/fake/ruff"), \
             mock.patch("lint_metrics.subprocess.run", return_value=fake):
            res = lint_metrics.lint_copy_py_artifacts(
                [_make_artifact(1, "dirty.py", _DIRTY_PY)])
        self.assertEqual(res.status, "unavailable")
        self.assertIsNone(res.errors)

    def test_ruff_subprocess_raises_is_unavailable(self):
        """FileNotFoundError/ OSError при запуске → unavailable, не валит прогон."""
        def boom(*_args, **_kwargs):
            raise OSError("permission denied")
        with mock.patch("lint_metrics.shutil.which", return_value="/fake/ruff"), \
             mock.patch("lint_metrics.subprocess.run", side_effect=boom):
            res = lint_metrics.lint_copy_py_artifacts(
                [_make_artifact(1, "dirty.py", _DIRTY_PY)])
        self.assertEqual(res.status, "unavailable")
        self.assertIsNone(res.errors)

    def test_staging_tempdir_failure_is_unavailable(self):
        """issue #100 (Codex review): сбой создания TemporaryDirectory (ENOSPC,
        нет /tmp, права) НЕ должен вылетать из метрики и валить прогон — метрика
        переводит его в unavailable. staging выполняется вне subprocess-границы,
        поэтому нужен отдельный охват всего lifecycle."""
        with mock.patch("lint_metrics.tempfile.TemporaryDirectory",
                        side_effect=OSError("no usable temporary directory")):
            res = lint_metrics.lint_copy_py_artifacts(
                [_make_artifact(1, "dirty.py", _DIRTY_PY)])
        self.assertEqual(res.status, "unavailable")
        self.assertIsNone(res.errors)

    def test_staging_write_failure_is_unavailable(self):
        """Сбой записи staged-файла (ENOSPC/права на родителя) → unavailable.

        Артефакт строим ДО mock-патча Path.write_bytes: патч глобален для класса
        pathlib.Path, иначе упал бы уже на src.write_bytes внутри _make_artifact."""
        art = _make_artifact(1, "dirty.py", _DIRTY_PY)
        with mock.patch("lint_metrics.shutil.which", return_value="/fake/ruff"), \
             mock.patch("lint_metrics.Path.write_bytes",
                        side_effect=OSError("no space left on device")):
            res = lint_metrics.lint_copy_py_artifacts([art])
        self.assertEqual(res.status, "unavailable")
        self.assertIsNone(res.errors)

    def test_ruff_timeout_is_unavailable(self):
        """Зависший Ruff (TimeoutExpired) → unavailable, прогон не висит."""
        import subprocess as sp
        with mock.patch("lint_metrics.shutil.which", return_value="/fake/ruff"), \
             mock.patch("lint_metrics.subprocess.run",
                        side_effect=sp.TimeoutExpired(cmd="ruff", timeout=1)):
            res = lint_metrics.lint_copy_py_artifacts(
                [_make_artifact(1, "dirty.py", _DIRTY_PY)])
        self.assertEqual(res.status, "unavailable")
        self.assertIsNone(res.errors)

    def test_non_oserror_staging_exception_is_unavailable(self):
        """Codex-review cycle 2: не-OSError исключение из staging/cleanup (напр.
        RuntimeError от TemporaryDirectory.__exit__ или глубокий путь →
        RecursionError) НЕ должно вылетать из метрики. Контракт #100 — ANY сбой
        гасится в unavailable; узкий except (OSError, ValueError, SubprocessError)
        этого не гарантирует. Базовый класс BaseException (KeyboardInterrupt/
        SystemExit) при этом пропускаем — это сигнал остановки, не метрический сбой."""
        art = _make_artifact(1, "dirty.py", _DIRTY_PY)
        with mock.patch("lint_metrics.tempfile.TemporaryDirectory",
                        side_effect=RuntimeError("cleanup blew up")):
            res = lint_metrics.lint_copy_py_artifacts([art])
        self.assertEqual(res.status, "unavailable")
        self.assertIsNone(res.errors)

    def test_baseexception_is_not_swallowed(self):
        """BaseException (KeyboardInterrupt/SystemExit) НЕ глотается метрикой —
        это сигнал остановки прогона, а не метрический сбой. except Exception
        ловит только Exception-подклассы, BaseException проходит наверх."""
        art = _make_artifact(1, "dirty.py", _DIRTY_PY)
        with mock.patch("lint_metrics.tempfile.TemporaryDirectory",
                        side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                lint_metrics.lint_copy_py_artifacts([art])


# === Изоляция: только собранные .py копии =====================================


class IsolationTests(_RuffStubMixin, unittest.TestCase):
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

        patch_which = mock.patch("lint_metrics.shutil.which", return_value="/fake/ruff")
        patch_run = mock.patch("lint_metrics.subprocess.run", side_effect=fake_run)
        patch_which.start()
        patch_run.start()
        self.addCleanup(patch_which.stop)
        self.addCleanup(patch_run.stop)
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
    """_summarize/_build_report приклеивают lint-результат к копиям и кладут его
    в отчёт. Метрика физически ДО cleanup_collected_artifacts (#99/#100).

    Сам Ruff тут мокается (через lint_copy_py_artifacts) — эти тесты про
    оркестратор-склейку, а не про бинарник; реальные end-to-end-прогоны Ruff —
    в RealRuffIntegrationTests (skip, если Ruff не установлен)."""

    def test_summarize_attaches_lint_to_successful_copies(self):
        import tempfile

        import benchmark_report as br

        # Две успешные копии с .py на диске (нужны collect_report_artifacts).
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
            # Мокаем Ruff: copy1 → 0 ошибок, copy2 → 3.
            by_idx = {1: lint_metrics.RunLintResult("checked", 0),
                      2: lint_metrics.RunLintResult("checked", 3)}
            with mock.patch("benchmark_report.lint_copy_py_artifacts",
                            side_effect=lambda arts: by_idx[arts[0].run_idx]):
                br._summarize(results, pricing)

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
            # Если бы метрика зачем-то вернула результат для провальной копии, тест
            # всё равно должен подтвердить, что _summarize её не приклеил.
            with mock.patch(
                "benchmark_report.lint_copy_py_artifacts",
                return_value=lint_metrics.RunLintResult("checked", 99),
            ):
                br._summarize(results, pricing)

        # Провальной копии lint=None (код != 0); успешная оценена.
        self.assertIsNone(results[0]["lint"])
        self.assertEqual(results[1]["lint"].errors, 99)

    def test_summarize_survives_staging_failure(self):
        """Codex-review cycle 1: отказ staging Ruff (нет tmp/ENOSPC) НЕ должен
        валить _summarize — иначе отчёт законченного прогона не доедет до БД.

        Метрика должна перевести сбой в unavailable, а _summarize — вернуть
        управление (usage_summary/summary/artifact_collection на месте), чтобы
        _build_report/_finalize/save_report выполнились штатно."""
        import tempfile

        import benchmark_report as br

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            d1 = root / "copy1"
            d1.mkdir()
            (d1 / "dirty.py").write_bytes(_DIRTY_PY)
            results = [{"index": 1, "code": 0, "dir": str(d1), "elapsed": 1.0}]
            pricing = {"prompt_per_1m": None, "completion_per_1m": None, "note": None}
            # Ломаем ВЕСЬ lifecycle staging (TemporaryDirectory) — это имитирует
            # деградированное окружение без tmp. Истинный путь _summarize, не мок.
            with mock.patch("lint_metrics.tempfile.TemporaryDirectory",
                            side_effect=OSError("no usable temporary directory")):
                usage_summary, summary, collection = br._summarize(results, pricing)

        # _summarize вернулся без исключения — отчёт достижим для сохранения.
        self.assertIsNotNone(usage_summary)
        self.assertIsNotNone(summary)
        self.assertIsNotNone(collection)
        # Метрика переведена в unavailable; копия не потеряла lint-оценку как None.
        self.assertEqual(results[0]["lint"].status, "unavailable")
        self.assertIsNone(results[0]["lint"].errors)

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


# === Подлинная end-to-end интеграция с Ruff (только если он установлен) =======


@unittest.skipUnless(shutil.which("ruff"), "Ruff не установлен в окружении")
class RealRuffIntegrationTests(unittest.TestCase):
    """Smoke-тесты на настоящий бинарник Ruff: подтверждает, что модуль зовёт
    корректную команду и стабы _CLEAN_PY/_DIRTY_PY действительно дают 0 и 3.

    Skip'ается в окружениях без Ruff (напр. CI без ruff в requirements) — это
    штатный режим метрики (unavailable). См. lint_metrics.unavailable-тесты."""

    def test_real_clean_file_is_zero(self):
        res = lint_metrics.lint_copy_py_artifacts([_make_artifact(1, "clean.py", _CLEAN_PY)])
        self.assertEqual(res.status, "checked")
        self.assertEqual(res.errors, 0)

    def test_real_dirty_file_is_three(self):
        res = lint_metrics.lint_copy_py_artifacts([_make_artifact(1, "dirty.py", _DIRTY_PY)])
        self.assertEqual(res.status, "checked")
        self.assertEqual(res.errors, 3)


if __name__ == "__main__":
    unittest.main()

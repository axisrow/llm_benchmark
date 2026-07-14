"""Тесты мультиязычной lint-метрики (#101, follow-up #100).

#100 добавила Ruff для Python. #101 расширяет метрику на не-Python языки,
сохранив единый формат (checked/na/unavailable), раздельные счётчики по каждому
инструменту, хранение/агрегацию/отображение.

Первый набор линтеров утверждён ПО ФАКТУ накопленных в data/main.db артефактов
(требование #101). Не-Python агентские файлы в базе: .html (реальные веб-страницы,
проект library_fine) и .json (конфиги, stock_downloader). Отдельных .js/.ts/.css
файлов в базе нет — они вне первого набора. Инструменты:
  - .html/.htm → HTML Tidy (`tidy`): каждая строка `line N column M - Error|Warning:`
                 в stderr = одна diagnostic;
  - .json      → `jq`: невалидный JSON = 1 diagnostic на файл, валидный = 0.

TDD: написаны ДО реализации, должны падать (red). См. issue #101.

Каждый адаптер покрыт: чистый файл=0, файл с известным числом diagnostics=точное
число, нет подходящих файлов=na, нет executable=unavailable, некорректный вывод/
техошибка=unavailable (не ломает прогон), объединение нескольких языков в копии.
"""

import hashlib
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import artifacts
import lint_metrics


# --- хелперы ------------------------------------------------------------------


def _artifact(run_idx: int, name: str, content: bytes) -> artifacts.RunArtifact:
    """RunArtifact с реальным source_path (линтеры читают контент из артефакта,
    но source_path нужен для совместимости с dataclass)."""
    tmp = Path(tempfile.mkdtemp(prefix="lintmulti-"))
    src = tmp / name
    src.parent.mkdir(parents=True, exist_ok=True)
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


# Валидный/битый JSON.
_CLEAN_JSON = b'{"tickers": ["MSFT"], "period": "1y"}\n'
_DIRTY_JSON = b'{"tickers": ["MSFT", }\n'

# Чистый HTML (современный tidy → 0 diagnostics) и грязный (незакрытые теги).
_CLEAN_HTML = (
    b"<!DOCTYPE html>\n<html lang=\"en\">\n<head>\n<meta charset=\"utf-8\">\n"
    b"<title>Ok</title>\n</head>\n<body>\n<p>Hello</p>\n</body>\n</html>\n"
)
_DIRTY_HTML = b"<html><head><title>Bad</title>\n<body><p>x <b>y <div>z</p>\n"


# === jq-адаптер: валидность JSON ==============================================


class JqAdapterTests(unittest.TestCase):
    """jq считает валидность: невалидный JSON = 1 diagnostic, валидный = 0."""

    def _stub_jq(self, per_file_exit: dict[str, int]):
        """Мокает наличие jq и subprocess.run: exit-код зависит от имени файла
        в переданной команде (jq зовётся по одному файлу)."""
        def fake_run(cmd, **_kw):
            target = cmd[-1]
            code = 0
            for suffix, exit_code in per_file_exit.items():
                if target.endswith(suffix):
                    code = exit_code
                    break
            fake = mock.Mock(returncode=code)
            fake.stdout = b""
            fake.stderr = b"" if code == 0 else b"jq: parse error"
            return fake

        pw = mock.patch("lint_metrics.shutil.which",
                        side_effect=lambda b: "/fake/" + b)
        pr = mock.patch("lint_metrics.subprocess.run", side_effect=fake_run)
        pw.start()
        pr.start()
        self.addCleanup(pw.stop)
        self.addCleanup(pr.stop)

    def test_clean_json_gives_zero_errors(self):
        self._stub_jq({"clean.json": 0})
        result = lint_metrics.lint_copy_artifacts([_artifact(1, "clean.json", _CLEAN_JSON)])
        self.assertIn("jq", result)
        self.assertEqual(result["jq"].status, "checked")
        self.assertEqual(result["jq"].errors, 0)

    def test_dirty_json_gives_one_error(self):
        self._stub_jq({"dirty.json": 5})
        result = lint_metrics.lint_copy_artifacts([_artifact(1, "dirty.json", _DIRTY_JSON)])
        self.assertEqual(result["jq"].status, "checked")
        self.assertEqual(result["jq"].errors, 1)

    def test_multiple_json_files_sum_errors(self):
        """Два битых + один валидный → 2 diagnostics (по одному на каждый битый)."""
        self._stub_jq({"a.json": 0, "b.json": 5, "c.json": 5})
        result = lint_metrics.lint_copy_artifacts([
            _artifact(1, "a.json", _CLEAN_JSON),
            _artifact(1, "b.json", _DIRTY_JSON),
            _artifact(1, "c.json", _DIRTY_JSON),
        ])
        self.assertEqual(result["jq"].status, "checked")
        self.assertEqual(result["jq"].errors, 2)

    def test_no_json_no_jq_entry(self):
        """Нет .json → jq вообще не запускается и его нет в результате."""
        result = lint_metrics.lint_copy_artifacts([_artifact(1, "app.py", b"x = 1\n")])
        self.assertNotIn("jq", result)

    def test_missing_jq_binary_is_unavailable(self):
        with mock.patch("lint_metrics.shutil.which",
                        side_effect=lambda b: None if b == "jq" else "/fake/" + b):
            result = lint_metrics.lint_copy_artifacts(
                [_artifact(1, "clean.json", _CLEAN_JSON)])
        self.assertEqual(result["jq"].status, "unavailable")
        self.assertIsNone(result["jq"].errors)

    def test_jq_subprocess_raises_is_unavailable(self):
        def boom(*_a, **_k):
            raise OSError("permission denied")
        with mock.patch("lint_metrics.shutil.which",
                        side_effect=lambda b: "/fake/" + b), \
             mock.patch("lint_metrics.subprocess.run", side_effect=boom):
            result = lint_metrics.lint_copy_artifacts(
                [_artifact(1, "clean.json", _CLEAN_JSON)])
        self.assertEqual(result["jq"].status, "unavailable")
        self.assertIsNone(result["jq"].errors)


# === tidy-адаптер: подсчёт строк diagnostics ==================================


class TidyAdapterTests(unittest.TestCase):
    """tidy печатает построчно `line N column M - Error|Warning: ...` в stderr;
    каждая такая строка = одна diagnostic. Формат стабилен между версиями tidy."""

    def _stub_tidy(self, stderr: bytes, returncode: int = 1):
        def fake_run(cmd, **_kw):
            fake = mock.Mock(returncode=returncode)
            fake.stdout = b""
            fake.stderr = stderr
            return fake

        pw = mock.patch("lint_metrics.shutil.which",
                        side_effect=lambda b: "/fake/" + b)
        pr = mock.patch("lint_metrics.subprocess.run", side_effect=fake_run)
        pw.start()
        pr.start()
        self.addCleanup(pw.stop)
        self.addCleanup(pr.stop)

    def test_clean_html_gives_zero_errors(self):
        self._stub_tidy(b"", returncode=0)
        result = lint_metrics.lint_copy_artifacts([_artifact(1, "clean.html", _CLEAN_HTML)])
        self.assertIn("tidy", result)
        self.assertEqual(result["tidy"].status, "checked")
        self.assertEqual(result["tidy"].errors, 0)

    def test_dirty_html_counts_diagnostic_lines(self):
        stderr = (
            b"line 1 column 1 - Warning: missing <!DOCTYPE> declaration\n"
            b"line 2 column 19 - Warning: missing </b> before <div>\n"
            b"line 2 column 32 - Error: <div> isn't allowed here\n"
        )
        self._stub_tidy(stderr, returncode=1)
        result = lint_metrics.lint_copy_artifacts([_artifact(1, "dirty.html", _DIRTY_HTML)])
        self.assertEqual(result["tidy"].status, "checked")
        self.assertEqual(result["tidy"].errors, 3)

    def test_summary_and_blank_lines_not_counted(self):
        """Только строки формата `line N column M - Error|Warning:` считаются;
        итоговая сводка tidy и пустые строки — нет."""
        stderr = (
            b"line 1 column 1 - Warning: missing <!DOCTYPE> declaration\n"
            b"\n"
            b"Info: Document content looks like HTML5\n"
            b"Tidy found 1 warning and 0 errors!\n"
        )
        self._stub_tidy(stderr, returncode=1)
        result = lint_metrics.lint_copy_artifacts([_artifact(1, "x.html", _DIRTY_HTML)])
        self.assertEqual(result["tidy"].errors, 1)

    def test_htm_extension_also_linted(self):
        self._stub_tidy(b"", returncode=0)
        result = lint_metrics.lint_copy_artifacts([_artifact(1, "page.htm", _CLEAN_HTML)])
        self.assertIn("tidy", result)
        self.assertEqual(result["tidy"].errors, 0)

    def test_no_html_no_tidy_entry(self):
        result = lint_metrics.lint_copy_artifacts([_artifact(1, "app.py", b"x = 1\n")])
        self.assertNotIn("tidy", result)

    def test_missing_tidy_binary_is_unavailable(self):
        with mock.patch("lint_metrics.shutil.which",
                        side_effect=lambda b: None if b == "tidy" else "/fake/" + b):
            result = lint_metrics.lint_copy_artifacts(
                [_artifact(1, "x.html", _DIRTY_HTML)])
        self.assertEqual(result["tidy"].status, "unavailable")
        self.assertIsNone(result["tidy"].errors)

    def test_tidy_non_ascii_stderr_does_not_crash(self):
        """Кириллица/битые байты в stderr не роняют парсер (decode errors=replace)."""
        stderr = "line 1 column 1 - Warning: тест\n".encode("utf-8") + b"\xff\xfe"
        self._stub_tidy(stderr, returncode=1)
        result = lint_metrics.lint_copy_artifacts([_artifact(1, "x.html", _DIRTY_HTML)])
        self.assertEqual(result["tidy"].status, "checked")
        self.assertEqual(result["tidy"].errors, 1)


# === Объединение нескольких языков в одной копии ==============================


class MultiLanguageCopyTests(unittest.TestCase):
    """Копия с .py + .html + .json запускает ВСЕ три линтера; результаты
    раздельны по имени инструмента, без смешивания diagnostics."""

    def _stub_all(self, ruff_diags, tidy_stderr, jq_exit):
        import json as _json

        def fake_run(cmd, **_kw):
            fake = mock.Mock()
            fake.stdout = b""
            fake.stderr = b""
            # Различаем линтеры по первому элементу команды (имя бинарника).
            binary = Path(cmd[0]).name
            if binary == "ruff":
                fake.returncode = 1 if ruff_diags else 0
                fake.stdout = _json.dumps(ruff_diags).encode("utf-8")
            elif binary == "tidy":
                fake.returncode = 1 if tidy_stderr else 0
                fake.stderr = tidy_stderr
            elif binary == "jq":
                fake.returncode = jq_exit
                fake.stderr = b"" if jq_exit == 0 else b"jq: parse error"
            return fake

        pw = mock.patch("lint_metrics.shutil.which",
                        side_effect=lambda b: "/fake/" + b)
        pr = mock.patch("lint_metrics.subprocess.run", side_effect=fake_run)
        pw.start()
        pr.start()
        self.addCleanup(pw.stop)
        self.addCleanup(pr.stop)

    def test_three_languages_separate_counters(self):
        self._stub_all(
            ruff_diags=[{"code": "F401"}, {"code": "F401"}],  # 2 py-ошибки
            tidy_stderr=b"line 1 column 1 - Warning: x\n",     # 1 html-ошибка
            jq_exit=5,                                          # 1 json-ошибка
        )
        result = lint_metrics.lint_copy_artifacts([
            _artifact(1, "app.py", b"import os\n"),
            _artifact(1, "index.html", _DIRTY_HTML),
            _artifact(1, "config.json", _DIRTY_JSON),
        ])
        self.assertEqual(result["ruff"].errors, 2)
        self.assertEqual(result["tidy"].errors, 1)
        self.assertEqual(result["jq"].errors, 1)
        # Раздельные счётчики — суммы не смешаны.
        self.assertEqual(set(result), {"ruff", "tidy", "jq"})

    def test_one_linter_failure_does_not_hide_others(self):
        """Техсбой одного линтера (tidy отсутствует) не скрывает результаты
        остальных — они остаются checked."""
        import json as _json

        def fake_run(cmd, **_kw):
            binary = Path(cmd[0]).name
            fake = mock.Mock(stdout=b"", stderr=b"")
            if binary == "ruff":
                fake.returncode = 0
                fake.stdout = _json.dumps([]).encode()
            elif binary == "jq":
                fake.returncode = 0
            return fake

        with mock.patch("lint_metrics.shutil.which",
                        side_effect=lambda b: None if b == "tidy" else "/fake/" + b), \
             mock.patch("lint_metrics.subprocess.run", side_effect=fake_run):
            result = lint_metrics.lint_copy_artifacts([
                _artifact(1, "app.py", b"x = 1\n"),
                _artifact(1, "index.html", _DIRTY_HTML),
                _artifact(1, "config.json", _CLEAN_JSON),
            ])
        self.assertEqual(result["ruff"].status, "checked")
        self.assertEqual(result["jq"].status, "checked")
        self.assertEqual(result["tidy"].status, "unavailable")


# === Агрегация по нескольким инструментам =====================================


def _run(index, code, linters):
    """Строка копии: linters — dict[str, RunLintResult] | None."""
    return {"index": index, "code": code, "linters": linters}


class MultiLinterAggregationTests(unittest.TestCase):
    def test_summarize_linters_separate_per_tool(self):
        """summarize_linters даёт по каждому инструменту свою сводку
        checked/na/unavailable/total_errors/avg_errors."""
        runs = [
            _run(1, 0, {
                "ruff": lint_metrics.RunLintResult("checked", 2),
                "tidy": lint_metrics.RunLintResult("checked", 4),
            }),
            _run(2, 0, {
                "ruff": lint_metrics.RunLintResult("checked", 0),
                "jq": lint_metrics.RunLintResult("checked", 1),
            }),
        ]
        summary = lint_metrics.summarize_linters(runs)
        self.assertEqual(summary["ruff"]["checked"], 2)
        self.assertEqual(summary["ruff"]["total_errors"], 2)
        self.assertAlmostEqual(summary["ruff"]["avg_errors"], 1.0)
        self.assertEqual(summary["tidy"]["checked"], 1)
        self.assertAlmostEqual(summary["tidy"]["avg_errors"], 4.0)
        self.assertEqual(summary["jq"]["checked"], 1)
        self.assertAlmostEqual(summary["jq"]["avg_errors"], 1.0)

    def test_failed_copies_excluded_per_tool(self):
        """Неуспешная копия (code!=0) не входит ни в один инструментальный агрегат."""
        runs = [
            _run(1, 0, {"tidy": lint_metrics.RunLintResult("checked", 3)}),
            _run(2, 1, {"tidy": lint_metrics.RunLintResult("checked", 99)}),  # fail
        ]
        summary = lint_metrics.summarize_linters(runs)
        self.assertEqual(summary["tidy"]["checked"], 1)
        self.assertEqual(summary["tidy"]["total_errors"], 3)
        self.assertAlmostEqual(summary["tidy"]["avg_errors"], 3.0)

    def test_na_and_unavailable_counted_but_not_averaged(self):
        runs = [
            _run(1, 0, {"jq": lint_metrics.RunLintResult("checked", 2)}),
            _run(2, 0, {"jq": lint_metrics.RunLintResult("unavailable", None)}),
        ]
        summary = lint_metrics.summarize_linters(runs)
        self.assertEqual(summary["jq"]["checked"], 1)
        self.assertEqual(summary["jq"]["unavailable"], 1)
        self.assertAlmostEqual(summary["jq"]["avg_errors"], 2.0)

    def test_empty_when_no_linters(self):
        runs = [_run(1, 0, None), _run(2, 0, {})]
        summary = lint_metrics.summarize_linters(runs)
        self.assertEqual(summary, {})

    def test_reads_linters_from_dict_form(self):
        """summarize_linters принимает и raw_json-форму (dict {status,errors})."""
        runs = [
            {"index": 1, "code": 0, "linters": {
                "jq": {"status": "checked", "errors": 5},
            }},
        ]
        summary = lint_metrics.summarize_linters(runs)
        self.assertEqual(summary["jq"]["checked"], 1)
        self.assertAlmostEqual(summary["jq"]["avg_errors"], 5.0)


# === Обратная совместимость: Ruff-путь #100 не сломан =========================


class BackwardCompatTests(unittest.TestCase):
    def test_lint_copy_py_artifacts_still_works(self):
        """Старая точка входа #100 продолжает возвращать один RunLintResult для
        Python (тонкая обёртка над реестром)."""
        import json as _json

        def fake_run(cmd, **_kw):
            fake = mock.Mock(returncode=0)
            fake.stdout = _json.dumps([]).encode()
            fake.stderr = b""
            return fake

        with mock.patch("lint_metrics.shutil.which", return_value="/fake/ruff"), \
             mock.patch("lint_metrics.subprocess.run", side_effect=fake_run):
            res = lint_metrics.lint_copy_py_artifacts(
                [_artifact(1, "clean.py", b"x = 1\n")])
        self.assertEqual(res.status, "checked")
        self.assertEqual(res.errors, 0)

    def test_lint_copy_py_artifacts_na_without_py(self):
        res = lint_metrics.lint_copy_py_artifacts(
            [_artifact(1, "config.json", _CLEAN_JSON)])
        self.assertEqual(res.status, "na")

    def test_lint_copy_artifacts_ruff_matches_py_helper(self):
        """lint_copy_artifacts["ruff"] эквивалентен lint_copy_py_artifacts."""
        import json as _json

        def fake_run(cmd, **_kw):
            fake = mock.Mock(returncode=0)
            fake.stdout = _json.dumps([]).encode()
            fake.stderr = b""
            return fake

        with mock.patch("lint_metrics.shutil.which", return_value="/fake/ruff"), \
             mock.patch("lint_metrics.subprocess.run", side_effect=fake_run):
            multi = lint_metrics.lint_copy_artifacts([_artifact(1, "a.py", b"x=1\n")])
        self.assertEqual(multi["ruff"].status, "checked")
        self.assertEqual(multi["ruff"].errors, 0)


# === Хранение в БД и попадание в индекс/дашборд ==============================


def _report_with_linters(project, runs_linters):
    """Отчёт с runs[].linters, runs[].ruff и lint_summary/ruff_summary.

    runs_linters — список (index, code, {имя_линтера → RunLintResult}).
    Собирает поля так же, как benchmark_report._build_report.
    """
    runs = []
    summary_runs = []
    for index, code, linters in runs_linters:
        run = {"index": index, "port": 4000 + index, "dir": f"/tmp/r{index}",
               "status": "готово" if code == 0 else "ошибка", "code": code,
               "elapsed": 1.0}
        if code == 0 and linters:
            run["linters"] = {
                name: {"status": r.status, "errors": r.errors}
                for name, r in linters.items()
            }
            if "ruff" in linters:
                ruff = linters["ruff"]
                run["ruff"] = {"status": ruff.status, "errors": ruff.errors}
        runs.append(run)
        summary_runs.append({"index": index, "code": code, "linters": linters})
    return {
        "project": project, "provider": "prov", "model": "mdl",
        "started_at": "2026-01-01T00:00:00",
        "summary": {"ok": sum(1 for _, c, _ in runs_linters if c == 0),
                    "timeout": 0, "error": 0},
        "ruff_summary": lint_metrics.summarize_lint(summary_runs),
        "lint_summary": lint_metrics.summarize_linters(summary_runs),
        "runs": runs,
    }


class PersistenceAndIndexTests(unittest.TestCase):
    def test_report_with_linters_round_trips_through_db(self):
        import json as _json

        import db
        report = _report_with_linters("proj_multi", [
            (1, 0, {"ruff": lint_metrics.RunLintResult("checked", 3),
                    "tidy": lint_metrics.RunLintResult("checked", 2)}),
            (2, 0, {"jq": lint_metrics.RunLintResult("checked", 1)}),
        ])
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                with conn:
                    rid = db.upsert_report(conn, report, "data/r.json",
                                           _json.dumps(report))
                row = conn.execute("SELECT raw_json FROM reports WHERE id=?",
                                   (rid,)).fetchone()
            finally:
                conn.close()
        loaded = _json.loads(row["raw_json"])
        self.assertEqual(loaded["runs"][0]["linters"]["tidy"],
                         {"status": "checked", "errors": 2})
        self.assertEqual(loaded["runs"][1]["linters"]["jq"],
                         {"status": "checked", "errors": 1})
        # ruff_summary остался синонимом lint_summary.ruff (обратная совместимость).
        self.assertEqual(loaded["ruff_summary"]["checked"], 1)
        self.assertEqual(loaded["lint_summary"]["ruff"]["checked"], 1)
        self.assertEqual(loaded["lint_summary"]["tidy"]["checked"], 1)
        self.assertEqual(loaded["lint_summary"]["jq"]["checked"], 1)

    def test_lint_summary_reaches_index_per_tool(self):
        """issue #101: сводка по инструментам попадает в index.json на уровне
        проекта, с раздельными avg по каждому линтеру."""
        from conftest import build_index_data

        report = _report_with_linters("proj_idx", [
            (1, 0, {"tidy": lint_metrics.RunLintResult("checked", 2),
                    "jq": lint_metrics.RunLintResult("checked", 0)}),
            (2, 0, {"tidy": lint_metrics.RunLintResult("checked", 4),
                    "jq": lint_metrics.RunLintResult("checked", 1)}),
        ])
        _count, data = build_index_data([report])
        projects = [g for g in data["projects"] if g["name"] == "proj_idx"]
        self.assertTrue(projects, "проект proj_idx должен попасть в индекс")
        proj = projects[0]
        self.assertIn("lint_summary", proj)
        self.assertEqual(proj["lint_summary"]["tidy"]["checked"], 2)
        self.assertAlmostEqual(proj["lint_summary"]["tidy"]["avg_errors"], 3.0)
        self.assertEqual(proj["lint_summary"]["jq"]["checked"], 2)
        self.assertAlmostEqual(proj["lint_summary"]["jq"]["avg_errors"], 0.5)

    def test_index_still_exposes_ruff_summary(self):
        """Обратная совместимость: ruff_summary на уровне проекта не пропал (#100
        дашборд читает именно его)."""
        from conftest import build_index_data

        report = _report_with_linters("proj_ruff", [
            (1, 0, {"ruff": lint_metrics.RunLintResult("checked", 2)}),
            (2, 0, {"ruff": lint_metrics.RunLintResult("checked", 4)}),
        ])
        _count, data = build_index_data([report])
        proj = [g for g in data["projects"] if g["name"] == "proj_ruff"][0]
        self.assertIn("ruff_summary", proj)
        self.assertEqual(proj["ruff_summary"]["checked"], 2)
        self.assertAlmostEqual(proj["ruff_summary"]["avg_errors"], 3.0)
        # И тот же результат виден через lint_summary.ruff.
        self.assertEqual(proj["lint_summary"]["ruff"]["checked"], 2)


# === Реальные бинарники (skip если не установлены) ============================


@unittest.skipUnless(shutil.which("jq"), "jq не установлен")
class RealJqTests(unittest.TestCase):
    def test_real_clean_json_zero(self):
        result = lint_metrics.lint_copy_artifacts([_artifact(1, "clean.json", _CLEAN_JSON)])
        self.assertEqual(result["jq"].status, "checked")
        self.assertEqual(result["jq"].errors, 0)

    def test_real_dirty_json_one(self):
        result = lint_metrics.lint_copy_artifacts([_artifact(1, "dirty.json", _DIRTY_JSON)])
        self.assertEqual(result["jq"].status, "checked")
        self.assertEqual(result["jq"].errors, 1)


@unittest.skipUnless(shutil.which("tidy"), "tidy не установлен")
class RealTidyTests(unittest.TestCase):
    def test_real_dirty_html_has_diagnostics(self):
        result = lint_metrics.lint_copy_artifacts([_artifact(1, "dirty.html", _DIRTY_HTML)])
        self.assertEqual(result["tidy"].status, "checked")
        # Точное число зависит от версии tidy; главное — их несколько (>0).
        self.assertGreater(result["tidy"].errors, 0)


if __name__ == "__main__":
    unittest.main()

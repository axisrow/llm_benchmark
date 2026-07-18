"""Тесты функциональной оценки HTML-калькуляторов library_fine (#119)."""

import datetime as dt
import hashlib
import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from fractions import Fraction
from pathlib import Path
from unittest import mock

import artifacts
import db
import library_fine_grading as grading

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import grade_library_fine as grade_cli  # noqa: E402


_REAL_DB_PATH = ROOT / "data" / "main.db"
FileFingerprint = tuple[int, int, str]
DbSnapshot = dict[str, FileFingerprint | None]

_REAL_DB_SNAPSHOT: DbSnapshot | None = None


def _file_fingerprint(path: Path) -> FileFingerprint | None:
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return (stat.st_mtime_ns, stat.st_size,
            hashlib.sha256(path.read_bytes()).hexdigest())


def _db_snapshot(path: Path) -> DbSnapshot:
    return {
        suffix: _file_fingerprint(path.with_name(path.name + suffix))
        for suffix in ("", "-wal", "-shm")
    }


def setUpModule() -> None:
    global _REAL_DB_SNAPSHOT
    _REAL_DB_SNAPSHOT = _db_snapshot(_REAL_DB_PATH)


def tearDownModule() -> None:
    after = _db_snapshot(_REAL_DB_PATH)
    if _REAL_DB_SNAPSHOT != after:
        raise AssertionError(
            "data/main.db изменилась во время тестов library_fine CLI: "
            f"было={_REAL_DB_SNAPSHOT} стало={after}"
        )


def _case(
    name: str = "case",
    *,
    control: dt.date = dt.date(2025, 6, 2),
    actual: dt.date = dt.date(2025, 6, 3),
    category: int = 0,
    deposit: int = 100_000,
    student: int = 0,
    pensioner: int = 0,
    repeat: int = 0,
) -> grading.FineCase:
    return grading.FineCase(
        name=name,
        fio="Иванов Иван",
        control_date=control,
        actual_date=actual,
        category=category,
        deposit=deposit,
        student=student,
        pensioner=pensioner,
        repeat=repeat,
        tags=(),
    )


def _artifact(run_idx: int, path: str, content: bytes,
              *, kind: str = artifacts.ARTIFACT_KIND_AGENT_FILE,
              ) -> artifacts.RunArtifact:
    return artifacts.RunArtifact(
        run_idx=run_idx,
        path=path,
        kind=kind,
        size_bytes=len(content),
        sha256=hashlib.sha256(content).hexdigest(),
        content=content,
        source_path=Path("/tmp") / path,
    )


def _html_grade(
    *,
    status: str = grading.GRADE_STATUS_GRADED,
    passed: int = 1,
    total: int = 1,
    autonomy: tuple[str, ...] = (),
    outcomes: tuple[grading.ComboOutcome, ...] = (),
    error: str | None = None,
) -> grading.HtmlGrade:
    return grading.HtmlGrade(
        status=status,
        function_name="calculateFine" if status == grading.GRADE_STATUS_GRADED else None,
        fn_arity=1 if status == grading.GRADE_STATUS_GRADED else None,
        adapter="object+date_local" if status == grading.GRADE_STATUS_GRADED else None,
        passed=passed,
        total=total,
        outcomes=outcomes,
        autonomy_violations=autonomy,
        engine="fake",
        exec_warning=None,
        error=error,
    )


REFERENCE_HTML = b"""<!doctype html><html><body><script>
function ceilFraction(numerator, denominator) {
  return Math.ceil(numerator / (denominator * 10)) * 10;
}
function overdueDays(control, actual) {
  if (actual <= control) return 0;
  var day = new Date(control.getTime());
  day.setDate(day.getDate() + 1);
  var count = 0;
  while (day <= actual) {
    var weekday = day.getDay();
    if (weekday !== 0 && weekday !== 6) count += 1;
    day.setDate(day.getDate() + 1);
  }
  return count;
}
function calculateFine(c) {
  var days = overdueDays(c.controlDate, c.actualDate);
  if (days === 0) return 0;
  var rate = c.category ? 75 : 25;
  var grace = c.category ? 4 : 8;
  var pensionerDiscount = Boolean(c.pensioner);
  if (c.student) {
    var base = 25;
    grace = 8;
    if (c.pensioner) {
      base = -base;
      pensionerDiscount = false;
    }
    rate = base * 77 / 100;
  }
  var total = 0;
  var graceDays = Math.min(days, grace);
  for (var n = 1; n <= graceDays; n += 1) {
    total += ceilFraction(rate * (21 + 10 * (n - 1)), 100);
  }
  for (var fullDay = grace; fullDay < days; fullDay += 1) {
    total += ceilFraction(rate, 1);
  }
  if (c.repeat) total += 240;
  if (pensionerDiscount) total = ceilFraction(total * 46, 100);
  return Math.min(Math.max(total, 20), c.deposit);
}
</script></body></html>"""


class ReferenceCalculationTests(unittest.TestCase):
    def test_ceil10_positive_zero_and_negative(self):
        cases = (
            (Fraction(0), 0),
            (Fraction(1), 10),
            (Fraction(10), 10),
            (Fraction(1001, 100), 20),
            (-4.04, 0),
            (-11.74, -10),
        )
        for value, expected in cases:
            with self.subTest(value=value):
                self.assertEqual(grading.ceil10(value), expected)

    def test_overdue_days_boundaries_and_weekends(self):
        cases = (
            (dt.date(2025, 6, 10), dt.date(2025, 6, 10), 0),
            (dt.date(2025, 6, 10), dt.date(2025, 6, 5), 0),
            (dt.date(2025, 6, 13), dt.date(2025, 6, 15), 0),
            (dt.date(2025, 6, 13), dt.date(2025, 6, 16), 1),
            (dt.date(2025, 6, 12), dt.date(2025, 6, 14), 1),
            (dt.date(2025, 6, 2), dt.date(2025, 6, 13), 9),
        )
        for control, actual, expected in cases:
            with self.subTest(control=control, actual=actual):
                self.assertEqual(grading.overdue_days(control, actual), expected)

    def test_reference_golden_cases(self):
        long_actual = dt.date(2025, 6, 13)  # 9 рабочих дней после 2 июня
        cases = (
            (_case(actual=dt.date(2025, 6, 2)), 0),
            (_case(control=dt.date(2025, 6, 13),
                   actual=dt.date(2025, 6, 15), repeat=1), 0),
            (_case(), 20),
            (_case(category=1), 20),
            (_case(actual=long_actual), 190),
            (_case(actual=long_actual, repeat=1), 430),
            (_case(actual=long_actual, pensioner=1), 90),
            (_case(actual=long_actual, deposit=100), 100),
            (_case(deposit=15), 15),
            (_case(actual=dt.date(2025, 6, 2), repeat=1), 0),
            # Правило 9: ставка студента 19.25 не округляется; округляется
            # только готовое число каждого дня.
            (_case(actual=dt.date(2025, 6, 9), student=1), 60),
            # Правило 9: каждый полный день округляется отдельно:
            # редкая, 7 дней, grace=4 (промпт правило 6: «редкая — 4 дня»):
            # 4 льготных (20+30+40+40) + 3 полных ⌈75⌉₁₀=80 ×3 = 130+240 = 370.
            (_case(actual=dt.date(2025, 6, 11), category=1), 370),
        )
        for case, expected in cases:
            with self.subTest(expected=expected, case=case):
                self.assertEqual(grading.reference_fine(case), expected)

    def test_grace_rare_is_four_days_as_in_prompt(self):
        # Промпт правило 6: «Льготный период: обычная книга — 8, редкая — 4 дня».
        # Эталон обязан использовать grace=4 для редкой (GRACE_RARE=4), а не 5.
        self.assertEqual(grading.GRACE_RARE, 4)
        self.assertEqual(grading.GRACE_REGULAR, 8)
        # Редкая, 5 рабочих дней просрочки, не студент: при grace=4 это
        # 4 льготных (20+30+40+40=130) + 1 полный ⌈75⌉₁₀=80 → 210.
        # (Если бы grace было 5, было бы 180 — расхождение ловит баг.)
        case = _case(actual=dt.date(2025, 6, 9), category=1)  # 5 раб. дней
        self.assertEqual(grading.overdue_days(case.control_date, case.actual_date), 5)
        self.assertEqual(grading.reference_fine(case), 210)

    def test_i5_uses_exact_negative_rate_and_never_point_46(self):
        # Редкая, студент+пенсионер, повтор, 11 рабочих дней: точная ставка
        # −25 × 0.77 = −19.25; льготные дни дают −40, 3 полных дня −30;
        # +240 = 170. Пенсионерское ×0.46 отменено (I5).
        case = _case(control=dt.date(2025, 6, 2), actual=dt.date(2025, 6, 17),
                     category=1, student=1, pensioner=1, repeat=1)
        rate = Fraction(-25 * 77, 100)
        self.assertEqual(rate, Fraction(-77, 4))
        daily = [grading.ceil10(Fraction(rate * (21 + 10 * (n - 1)), 100))
                 for n in range(1, 9)]
        manual = (sum(daily) + 3 * grading.ceil10(rate)
                  + grading.REPEAT_SURCHARGE)
        self.assertEqual(manual, 170)
        self.assertEqual(grading.reference_fine(case), manual)
        # Если бы пенсионерское ×0.46 применялось, итог был бы 80, а не 170.
        self.assertEqual(grading.ceil10(Fraction(manual * 46, 100)), 80)


class MatrixTests(unittest.TestCase):
    def test_matrix_loads_34_unique_cases_deterministically(self):
        self.assertEqual(grading._load_matrix(), grading.TEST_MATRIX)
        self.assertEqual(len(grading.TEST_MATRIX), 34)
        names = [case.name for case in grading.TEST_MATRIX]
        self.assertEqual(len(names), len(set(names)))

    def test_reference_reproduces_manual_expected_of_every_case(self):
        # Матрица — источник правды (#126): expected.fine выверены вручную,
        # эталон обязан воспроизводить каждый (иначе JSON и код разъехались).
        rows = json.loads(grading.MATRIX_PATH.read_text(encoding="utf-8"))[
            "calculations"]["rows"]
        self.assertEqual(len(rows), len(grading.TEST_MATRIX))
        for case, row in zip(grading.TEST_MATRIX, rows):
            with self.subTest(case=case.name):
                self.assertEqual(grading.reference_fine(case),
                                 row["expected"]["fine"])

    def test_matrix_expected_detail_matches_reference_logic(self):
        # Страж самосогласованности (#126, правило 9): ВЕСЬ expected-блок матрицы
        # (effectiveRate, dailyCharges, dailySubtotal/afterSurcharge/beforeLimits,
        # graceDays, minimum/deposit-cap и т.д.) обязан совпадать с тем, что
        # считает эталон. Независимое перестроение детализации (клон тела
        # reference_fine) — ловит любое расхождение detail↔эталон, чтобы будущая
        # правка эталона не смогла снова разъехать матрицу (как произошло при
        # вводе правила 9: fine перенесли, детализацию — нет).
        rows = json.loads(grading.MATRIX_PATH.read_text(encoding="utf-8"))[
            "calculations"]["rows"]
        for case, row in zip(grading.TEST_MATRIX, rows):
            with self.subTest(case=case.name):
                exp = row["expected"]
                days = grading.overdue_days(case.control_date, case.actual_date)
                self.assertEqual(exp["overdueDays"], days,
                                 f"{case.name}: overdueDays")
                if days == 0:
                    # I6: нет просрочки — нет штрафа; все суммы 0, минимума нет.
                    self.assertEqual(exp["fine"], 0)
                    self.assertEqual(exp["dailySubtotal"], 0)
                    self.assertEqual(exp["afterSurcharge"], 0)
                    self.assertEqual(exp["beforeLimits"], 0)
                    self.assertEqual(exp["dailyCharges"], [])
                    self.assertFalse(exp["minimumApplied"])
                    continue

                # Эффективная ставка и grace — по эталону (правило 9: ставка
                # студента НЕ округляется; студент→обычная→grace=8).
                rate = Fraction(grading.BASE_RATE_RARE if case.category
                                else grading.BASE_RATE_REGULAR)
                grace = grading.GRACE_RARE if case.category else grading.GRACE_REGULAR
                pensioner_discount = bool(case.pensioner)
                if case.student:
                    base = Fraction(grading.BASE_RATE_REGULAR)
                    grace = grading.GRACE_REGULAR
                    if case.pensioner:
                        base = -base
                        pensioner_discount = False
                    rate = base * grading.STUDENT_RATE_MULT
                self.assertEqual(exp["originalRate"],
                                 grading.BASE_RATE_RARE if case.category
                                 else grading.BASE_RATE_REGULAR)
                self.assertEqual(exp["effectiveRate"], float(rate),
                                 f"{case.name}: effectiveRate (точная, неокруглённая)")
                self.assertEqual(exp["graceDays"], grace)

                # dailyCharges: льготные дни + полные (каждый отдельно, правило 9).
                expected_dc: list[dict] = []
                total = Fraction(0)
                for n in range(1, min(days, grace) + 1):
                    pct = Fraction(21 + 10 * (n - 1), 100)
                    raw = rate * pct
                    charged = grading.ceil10(raw)
                    total += charged
                    expected_dc.append({
                        "fromDay": n, "toDay": n, "count": 1,
                        "percent": 21 + 10 * (n - 1),
                        "rawAmount": float(raw), "chargedAmount": charged})
                for d in range(grace + 1, days + 1):
                    charged = grading.ceil10(rate)
                    total += charged
                    expected_dc.append({
                        "fromDay": d, "toDay": d, "count": 1, "percent": 100,
                        "unitRate": float(rate), "rawAmount": float(rate),
                        "chargedAmount": charged})
                self.assertEqual(exp["dailyCharges"], expected_dc,
                                 f"{case.name}: dailyCharges")

                # Инвариант: Σ chargedAmount == dailySubtotal.
                self.assertEqual(sum(dc["chargedAmount"] for dc in exp["dailyCharges"]),
                                 exp["dailySubtotal"], f"{case.name}: Σ≠dailySubtotal")

                repeat_surch = grading.REPEAT_SURCHARGE if case.repeat else 0
                self.assertEqual(exp["repeatSurcharge"], repeat_surch)
                after = int(total) + repeat_surch
                self.assertEqual(exp["afterSurcharge"], after)
                self.assertEqual(exp["dailySubtotal"], int(total))

                if pensioner_discount:
                    self.assertEqual(exp["pensionerMultiplier"],
                                     float(grading.PENSIONER_TOTAL_MULT))
                    before = grading.ceil10(Fraction(after)
                                            * grading.PENSIONER_TOTAL_MULT)
                else:
                    self.assertIsNone(exp["pensionerMultiplier"])
                    before = after
                self.assertEqual(exp["beforeLimits"], before)

                fine_after_min = max(before, grading.MIN_FINE)
                self.assertEqual(exp["minimumApplied"],
                                 before < grading.MIN_FINE)
                self.assertEqual(exp["depositCapApplied"],
                                 fine_after_min > case.deposit)
                self.assertEqual(exp["fine"],
                                 min(fine_after_min, case.deposit))
                # Финальный страж: детализация обязана сходиться с эталоном.
                self.assertEqual(exp["fine"], grading.reference_fine(case))


class HtmlParsingAndAutonomyTests(unittest.TestCase):
    def test_extract_inline_scripts_filters_src_and_non_js_types(self):
        html = """
        <SCRIPT>function one() { return 1; }</SCRIPT>
        <script type="application/javascript">function two() { return 2; }</script>
        <script type="application/json">{"x": 1}</script>
        <script src="https://cdn.example/x.js"></script>
        """
        scripts = grading.extract_inline_scripts(html)
        self.assertEqual(len(scripts), 2)
        self.assertIn("function one", scripts[0])
        self.assertIn("function two", scripts[1])

    def test_autonomy_detects_external_dependencies_and_dynamic_import(self):
        html = """
        <script src="https://cdn.example/a.js"></script>
        <script src="local.js"></script>
        <link rel="stylesheet" href="//cdn.example/a.css">
        <link rel="stylesheet" href="local.css">
        <script>
          import x from "https://cdn.example/static.js";
          import("https://cdn.example/dynamic.js");
          importScripts("//cdn.example/worker.js");
        </script>
        """
        violations = grading.check_autonomy(html)
        self.assertEqual(len(violations), 7)
        self.assertTrue(any("dynamic.js" in item for item in violations))
        self.assertTrue(any("local.css" in item for item in violations))

    def test_data_uri_and_inline_code_are_autonomous(self):
        html = """
        <script src="data:text/javascript,void(0)"></script>
        <link rel="stylesheet" href="data:text/css,body{}">
        <style>body { color: black; }</style>
        <script>const url = "https://example.test/not-an-import";</script>
        """
        self.assertEqual(grading.check_autonomy(html), ())


class _FakeEngine(grading.JsEngine):
    name = "fake"

    def __init__(self, *, found: object,
                 adapter_results: dict[tuple[str, str], object] | None = None,
                 default_result: object = None,
                 fail_run_index: int | None = None) -> None:
        self.found = found
        self.adapter_results = adapter_results or {}
        self.default_result = default_result
        self.fail_run_index = fail_run_index
        self.run_count = 0

    def run(self, code: str) -> None:
        del code
        self.run_count += 1
        if self.run_count == self.fail_run_index:
            raise RuntimeError("top-level boom")

    def eval_json(self, expr: str) -> object:
        if expr.startswith("__gradeDiscover("):
            return [self.found] if isinstance(self.found, dict) else self.found
        for (convention, rep), result in self.adapter_results.items():
            if (f'"conv": "{convention}"' in expr
                    and f'"rep": "{rep}"' in expr):
                return result
        return self.default_result


class EngineBoundaryTests(unittest.TestCase):
    _HTML = b"<html><script>function calculateFine(x) { return 0; }</script></html>"

    def _grade_with(self, engine: _FakeEngine,
                    matrix: tuple[grading.FineCase, ...]) -> grading.HtmlGrade:
        # isolated=False: эти тесты мокают create_engine и проверяют
        # внутреннюю логику оценки в текущем процессе — в subprocess мок недоступен.
        with mock.patch.object(grading, "create_engine",
                               return_value=lambda: engine):
            return grading.grade_html(self._HTML, matrix=matrix, isolated=False)

    def test_best_adapter_maximizes_matches_then_numeric_results(self):
        matrix = (_case("one"), _case("repeat", repeat=1))
        engine = _FakeEngine(
            found={"name": "calculateFine", "arity": 1},
            adapter_results={
                ("object8", "date_local"): [{"v": 20}, {"v": 0}],
                ("object8", "date_utc"): [{"v": 20}, {"v": 250}],
            },
            default_result=[{"e": "bad"}, {"e": "bad"}],
        )
        grade = self._grade_with(engine, matrix)
        self.assertEqual(grade.status, grading.GRADE_STATUS_GRADED)
        self.assertEqual(grade.adapter, "object8+date_utc")
        self.assertEqual((grade.passed, grade.total), (2, 2))

    def test_unavailable_no_function_no_adapter_and_exec_error_statuses(self):
        matrix = (_case(),)
        with self.subTest(status="unavailable"):
            with mock.patch.object(grading, "create_engine", return_value=None):
                grade = grading.grade_html(self._HTML, matrix=matrix, isolated=False)
            self.assertEqual(grade.status, grading.GRADE_STATUS_UNAVAILABLE)

        with self.subTest(status="no_function"):
            grade = self._grade_with(_FakeEngine(found=None), matrix)
            self.assertEqual(grade.status, grading.GRADE_STATUS_NO_FUNCTION)

        with self.subTest(status="parse_error"):
            engine = _FakeEngine(
                found={"name": "calculateFine", "arity": 1},
                default_result=[{"e": "non-numeric"}],
            )
            grade = self._grade_with(engine, matrix)
            self.assertEqual(grade.status, grading.GRADE_STATUS_PARSE_ERROR)

        with self.subTest(status="exec_error"):
            with mock.patch.object(grading, "create_engine",
                                   return_value=lambda: (_ for _ in ()).throw(
                                       RuntimeError("factory boom"))):
                grade = grading.grade_html(self._HTML, matrix=matrix, isolated=False)
            self.assertEqual(grade.status, grading.GRADE_STATUS_EXEC_ERROR)

    def test_top_level_failure_is_warning_when_function_can_still_be_called(self):
        matrix = (_case(),)
        engine = _FakeEngine(
            found={"name": "calculateFine", "arity": 1},
            adapter_results={("object8", "date_local"): [{"v": 20}]},
            default_result=[{"e": "bad"}],
            fail_run_index=3,
        )
        grade = self._grade_with(engine, matrix)
        self.assertEqual(grade.status, grading.GRADE_STATUS_GRADED)
        self.assertIn("top-level boom", grade.exec_warning or "")

    def test_invalid_engine_name_is_rejected(self):
        with self.assertRaises(ValueError):
            grading.create_engine("not-an-engine")


class ReportAndCopyTests(unittest.TestCase):
    def test_grade_report_filters_artifacts_and_caches_duplicate_sha(self):
        html = b"<script>function calculateFine(x) { return 20; }</script>"
        report = {
            "project": "library_fine",
            "provider": "provider",
            "model": "model",
            "started_at": "2026-07-15T00:00:00",
            "summary": {"ok": 1, "timeout": 0, "error": 0},
            "runs": [{"index": 1, "status": "готово", "code": 0}],
        }
        with tempfile.TemporaryDirectory() as td:
            conn = db.connect(Path(td) / "main.db")
            try:
                db.init_schema(conn)
                with conn:
                    report_id = db.upsert_report(
                        conn,
                        report,
                        "data/result/library_fine/report.json",
                        json.dumps(report),
                        artifacts=[
                            _artifact(1, "a.html", html),
                            _artifact(1, "b.html", html),
                            _artifact(1, "notes.txt", b"ignore"),
                            _artifact(1, "run.log", b"ignore",
                                      kind=artifacts.ARTIFACT_KIND_LOG),
                        ],
                    )
                expected = _html_grade()
                with mock.patch.object(grading, "grade_html",
                                       return_value=expected) as mocked:
                    grades = grading.grade_report(conn, report_id, cache={})
                self.assertEqual(len(grades), 2)
                self.assertEqual(mocked.call_count, 1)
            finally:
                conn.close()

    def test_grade_copy_contracts_na_unavailable_and_best_html(self):
        non_html = [_artifact(1, "readme.txt", b"x")]
        self.assertEqual(
            grading.grade_copy_artifacts(non_html).status,
            grading.FINE_STATUS_NA,
        )

        htmls = [
            _artifact(1, "one.html", b"one"),
            _artifact(1, "two.html", b"two"),
        ]
        with mock.patch.object(
            grading,
            "grade_html",
            side_effect=[
                _html_grade(passed=1, total=2, autonomy=("cdn",)),
                _html_grade(passed=2, total=2),
            ],
        ):
            result = grading.grade_copy_artifacts(htmls)
        self.assertEqual(result.status, grading.FINE_STATUS_CHECKED)
        self.assertEqual((result.passed, result.total), (2, 2))
        self.assertTrue(result.autonomous)

        with mock.patch.object(
            grading,
            "grade_html",
            return_value=_html_grade(status=grading.GRADE_STATUS_UNAVAILABLE),
        ):
            result = grading.grade_copy_artifacts(htmls[:1])
        self.assertEqual(result.status, grading.FINE_STATUS_UNAVAILABLE)

        with mock.patch.object(
            grading,
            "grade_html",
            return_value=_html_grade(
                status=grading.GRADE_STATUS_PARSE_ERROR,
                error="нет независимой функции расчёта",
            ),
        ):
            result = grading.grade_copy_artifacts(htmls[:1])
        self.assertEqual(result.status, grading.FINE_STATUS_PARSE_ERROR)
        self.assertIsNone(result.passed)
        self.assertIn("нет независимой функции", " ".join(result.errors))

    def test_calibrate_reports_modal_consensus(self):
        matrix = (_case("one"),)
        outcomes = (
            grading.ComboOutcome("one", 20, 30, False, None),
        )
        grades = [
            grading.ArtifactGrade(
                report_id=idx,
                run_idx=1,
                path="x.html",
                sha256=str(idx) * 64,
                grade=_html_grade(outcomes=outcomes),
            )
            for idx in (1, 2)
        ]
        row = grading.calibrate(grades, matrix)[0]
        self.assertEqual(row["reference"], 20)
        self.assertEqual((row["consensus"], row["consensus_count"]), (30, 2))


_PRICING = {"prompt_per_1m": None, "completion_per_1m": None, "note": None}


class PipelineIntegrationTests(unittest.TestCase):
    """_summarize/_build_report приклеивают fine-оценку к копиям library_fine
    (#126): runs[].fine + fine_summary. Прочие проекты не получают ни ключей,
    ни вызова грейдера. Сам грейдер тут мокается — тесты про оркестратор."""

    @staticmethod
    def _results_with_html(root: Path) -> list[dict]:
        copy_dir = root / "copy1"
        copy_dir.mkdir()
        (copy_dir / "calc.html").write_bytes(b"<html></html>")
        return [{"index": 1, "code": 0, "dir": str(copy_dir), "elapsed": 1.0}]

    def _summarize(self, project: str, results: list[dict]) -> mock.Mock:
        import benchmark_report as br

        graded = grading.RunFineGradeResult("checked", 30, 34, True)
        with (mock.patch("benchmark_report.lint_copy_artifacts",
                         return_value={}),
              mock.patch("benchmark_report.grade_copy_artifacts",
                         return_value=graded) as mocked):
            br._summarize(results, _PRICING, project)
        return mocked

    def test_summarize_grades_only_library_fine_copies(self):
        with tempfile.TemporaryDirectory() as td:
            results = self._results_with_html(Path(td))
            mocked = self._summarize(grading.PROJECT_NAME, results)
        self.assertEqual(mocked.call_count, 1)
        self.assertEqual((results[0]["fine"].passed, results[0]["fine"].total),
                         (30, 34))

    def test_summarize_other_projects_get_fine_none(self):
        with tempfile.TemporaryDirectory() as td:
            results = self._results_with_html(Path(td))
            mocked = self._summarize("hello_world", results)
        mocked.assert_not_called()
        self.assertIsNone(results[0]["fine"])

    def test_summarize_failed_copy_gets_fine_none(self):
        # gate code==0 должен срабатывать ДО вызова грейдера: он исполняет
        # недоверенный JS модели в subprocess, поэтому для фейловой копии
        # (результат которой всё равно отбрасывается) его запускать нельзя.
        with tempfile.TemporaryDirectory() as td:
            results = self._results_with_html(Path(td))
            results[0]["code"] = 1
            mocked = self._summarize(grading.PROJECT_NAME, results)
        mocked.assert_not_called()
        self.assertIsNone(results[0]["fine"])

    def test_summarize_fine_counts_statuses_and_score(self):
        runs = [
            {"index": 1, "fine": grading.RunFineGradeResult(
                "checked", 30, 34, True, ())},
            {"index": 2, "fine": grading.RunFineGradeResult(
                "checked", 34, 34, False, ("Внешняя зависимость",))},
            {"index": 3, "fine": grading.RunFineGradeResult(
                "na", None, None, None, ())},
            {"index": 4,
             "fine": grading.RunFineGradeResult(
                 "unavailable", None, None, None, ("Нет JS-движка",))},
            {"index": 5,
             "fine": grading.RunFineGradeResult(
                 "parse_error", None, None, True, ("Ошибка парсера",))},
            {"index": 6, "fine": None},  # провальная копия — вне сводки
        ]
        self.assertEqual(
            grading.summarize_fine(runs),
            {"checked": 2, "na": 1, "unavailable": 1, "parse_error": 1,
             "autonomy_errors": 1, "passed": 64, "total": 68},
        )

    def _build_report(self, project: str, fine) -> dict:
        import argparse

        import benchmark_report as br

        results = [
            {"index": 1, "port": 4001, "dir": "/tmp/r1", "code": 0,
             "elapsed": 1.0, "usage": None, "reason": None, "lint": None,
             "fine": fine},
            {"index": 2, "port": 4002, "dir": "/tmp/r2", "code": 1,
             "elapsed": 2.0, "usage": None, "reason": None, "lint": None,
             "fine": None},
        ]
        args = argparse.Namespace(
            project=project, model="m", provider="pr", copies=2, planning="off",
            question_responder="recommended", agent="bench_coder",
        )
        return br._build_report(
            args, task="t", description=None, what_it_tests=None,
            started_at=dt.datetime(2026, 1, 1), run_elapsed=3.0,
            summary={"ok": 1, "timeout": 0, "error": 1}, pricing={},
            usage_summary={}, artifact_collection=mock.Mock(summary=lambda: {}),
            results=results,
        )

    def test_build_report_emits_fine_fields_only_for_library_fine(self):
        fine = grading.RunFineGradeResult(
            "checked", 30, 34, False, ("Внешняя зависимость: CDN",))
        report = self._build_report(grading.PROJECT_NAME, fine)
        self.assertEqual(report["runs"][0]["fine"],
                         {"status": "checked", "passed": 30, "total": 34,
                          "autonomous": False,
                          "errors": ["Внешняя зависимость: CDN"]})
        self.assertNotIn("fine", report["runs"][1])
        self.assertEqual(report["fine_summary"],
                         {"checked": 1, "na": 0, "unavailable": 0,
                          "parse_error": 0, "autonomy_errors": 1,
                          "passed": 30, "total": 34})

        other = self._build_report("hello_world", None)
        self.assertNotIn("fine_summary", other)
        self.assertNotIn("fine", other["runs"][0])

    def test_fine_summary_reaches_index_project_group(self):
        from conftest import build_index_data

        def report(started: str, fine_summary: dict | None) -> dict:
            rep = {
                "project": grading.PROJECT_NAME, "provider": "prov",
                "model": "mdl", "started_at": started,
                "summary": {"ok": 1, "timeout": 0, "error": 0},
                "runs": [{"index": 1, "status": "готово", "code": 0}],
            }
            if fine_summary is not None:
                rep["fine_summary"] = fine_summary
            return rep

        reports = [
            report("2026-01-01T00:00:00", {"checked": 2, "na": 0,
                                           "unavailable": 0,
                                           "passed": 60, "total": 68}),
            report("2026-01-02T00:00:00", {"checked": 1, "na": 1,
                                           "unavailable": 0,
                                           "passed": 34, "total": 34}),
            report("2026-01-03T00:00:00", None),  # старый отчёт без сводки
        ]
        _count, data = build_index_data(reports)
        proj = [g for g in data["projects"]
                if g["name"] == grading.PROJECT_NAME][0]
        # Семантика #121: суммирование по ВСЕМ отчётам ячейки.
        self.assertEqual(proj["fine_summary"],
                         {"checked": 3, "na": 1, "unavailable": 0,
                          "parse_error": 0, "autonomy_errors": 0,
                          "passed": 94, "total": 102})

    def test_projects_without_fine_summary_get_no_index_key(self):
        from conftest import build_index_data

        rep = {
            "project": "hello_world", "provider": "prov", "model": "mdl",
            "started_at": "2026-01-01T00:00:00",
            "summary": {"ok": 1, "timeout": 0, "error": 0},
            "runs": [{"index": 1, "status": "готово", "code": 0}],
        }
        _count, data = build_index_data([rep])
        proj = [g for g in data["projects"] if g["name"] == "hello_world"][0]
        self.assertNotIn("fine_summary", proj)


@unittest.skipUnless(grading.create_engine(), "JS-движок не установлен")
class RealEngineIntegrationTests(unittest.TestCase):
    def test_reference_mirror_scores_full_matrix_on_both_backends(self):
        for backend in ("mini-racer", "quickjs"):
            with self.subTest(backend=backend):
                self.assertIsNotNone(grading.create_engine(backend))
                grade = grading.grade_html(REFERENCE_HTML, prefer_engine=backend)
                self.assertEqual(grade.status, grading.GRADE_STATUS_GRADED)
                self.assertEqual((grade.passed, grade.total), (34, 34))
                self.assertEqual(grade.adapter, "object7+date_local")

    def test_positional_batch_and_dom_stub_conventions(self):
        matrix = (_case(),)
        samples = (
            (b"<script>function calculateFine(a,b,c,d,e,f,g){return 20}</script>",
             "positional7+date_local"),
            (b"<script>function calculateFine(n,a,b,c,d,e,f,g){return 20}</script>",
             "positional8+date_local"),
            (b"<script>function calculateFine(rows){return Array.isArray(rows)"
             b"?{results:[{fine:20}]}:{results:[]}}</script>",
             "row+date_local"),
            (b"<script>document.getElementById('x').addEventListener('click',"
             b"function(){});function calculateFine(x){return {fine:20}}</script>",
             "row+date_local"),
        )
        for html, adapter in samples:
            with self.subTest(adapter=adapter):
                grade = grading.grade_html(html, matrix=matrix)
                self.assertEqual(grade.status, grading.GRADE_STATUS_GRADED)
                self.assertEqual((grade.passed, grade.total), (1, 1))
                self.assertEqual(grade.adapter, adapter)

    def test_discovers_arbitrary_function_and_recursive_result_key(self):
        html = b"""<script>
        function helperOrbit(x) { return 'not a score'; }
        function nebulaQuartz(a,b,c,d,e,f,g,h) {
          return {payload: {completelyArbitraryKey: 20}};
        }
        </script>"""
        grade = grading.grade_html(html, matrix=(_case(),))
        self.assertEqual(grade.status, grading.GRADE_STATUS_GRADED)
        self.assertEqual(grade.function_name, "nebulaQuartz")
        self.assertEqual((grade.passed, grade.total), (1, 1))

    def test_discovers_independent_method_inside_arbitrary_object(self):
        html = b"""<script>
        const violetModule = {
          lunarMethod(a,b,c,d,e,f,g,h) { return {deep: {scorelessName: 20}}; }
        };
        </script>"""
        grade = grading.grade_html(html, matrix=(_case(),))
        self.assertEqual(grade.status, grading.GRADE_STATUS_GRADED)
        self.assertEqual(grade.function_name, "violetModule.lunarMethod")
        self.assertEqual((grade.passed, grade.total), (1, 1))

    def test_flat_ordered_row_is_a_supported_independent_function(self):
        html = b"""<script>
        const lunarRow = row => Array.isArray(row) && !Array.isArray(row[0])
          ? {anything: 20} : {anything: 999};
        </script>"""
        grade = grading.grade_html(html, matrix=(_case(),))
        self.assertEqual(grade.status, grading.GRADE_STATUS_GRADED)
        self.assertEqual(grade.function_name, "lunarRow")
        self.assertTrue((grade.adapter or "").startswith("row+"))

    def test_object_fields_are_derived_without_alias_dictionary(self):
        html = b"""<script>
        function cobalt(record) {
          void record.alpha; void record.beta; void record.gamma;
          void record.delta; void record.epsilon; void record.zeta;
          void record.eta; void record.theta;
          return record.delta === 1000 ? {omega: 20} : {omega: 999};
        }
        </script>"""
        grade = grading.grade_html(html, matrix=(_case(deposit=1000),))
        self.assertEqual(grade.status, grading.GRADE_STATUS_GRADED)
        self.assertEqual(grade.function_name, "cobalt")
        self.assertTrue((grade.adapter or "").startswith("object8+"))

    def test_dom_dependent_candidate_is_rejected_when_pure_function_exists(self):
        html = b"""<script>
        function interfaceWrapper(a,b,c,d,e,f,g,h) {
          document.getElementById('result').textContent = '20';
          return 20;
        }
        function pureCore(a,b,c,d,e,f,g,h) { return 20; }
        </script>"""
        grade = grading.grade_html(html, matrix=(_case(),))
        self.assertEqual(grade.status, grading.GRADE_STATUS_GRADED)
        self.assertEqual(grade.function_name, "pureCore")

    def test_only_dom_dependent_function_is_parse_error_without_score(self):
        html = b"""<script>
        function interfaceOnly(a,b,c,d,e,f,g,h) {
          return Number(document.getElementById('fine').textContent);
        }
        </script>"""
        grade = grading.grade_html(html, matrix=(_case(),))
        self.assertEqual(grade.status, grading.GRADE_STATUS_PARSE_ERROR)
        self.assertEqual(grade.passed, 0)
        self.assertIn("независим", grade.error or "")

    def test_real_engine_reports_missing_function(self):
        grade = grading.grade_html(
            b"<script>function unrelated(){return 1}</script>",
            matrix=(_case(),),
        )
        self.assertEqual(grade.status, grading.GRADE_STATUS_GRADED)
        self.assertEqual(grade.passed, 0)

    def test_mini_racer_timeout_bounds_infinite_loop(self):
        if grading.create_engine("mini-racer") is None:
            self.skipTest("mini-racer не установлен")
        started = time.monotonic()
        grade = grading.grade_html(
            b"<script>function calculateFine(x){while(true){}}</script>",
            matrix=(_case(),),
            prefer_engine="mini-racer",
            eval_timeout_sec=0.02,
        )
        self.assertIn(grade.status, {
            grading.GRADE_STATUS_NO_ADAPTER,
            grading.GRADE_STATUS_EXEC_ERROR,
        })
        self.assertLess(time.monotonic() - started, 2.0)

    def test_mini_racer_memory_limit_bounds_unbounded_allocation(self):
        # Недоверенный код артефакта не должен уметь уйти в OOM-килл процесса:
        # mini-racer обязан поднять JSOOMException (а не дождаться таймаута),
        # и граница grade_html переводит это в штатный статус, не краш.
        if grading.create_engine("mini-racer") is None:
            self.skipTest("mini-racer не установлен")
        html = (b"<script>function calculateFine(x){"
                b"var a=[];while(true){a.push(new Array(4096).fill(0))}}"
                b"</script>")
        started = time.monotonic()
        grade = grading.grade_html(
            html, matrix=(_case(),), prefer_engine="mini-racer",
            eval_timeout_sec=5.0, max_memory_bytes=8 * 1024 * 1024,
        )
        self.assertIn(grade.status, {
            grading.GRADE_STATUS_NO_ADAPTER,
            grading.GRADE_STATUS_EXEC_ERROR,
        })
        # срабатывает лимит памяти, а не таймаут — должен быть заметно быстрее 5 с
        self.assertLess(time.monotonic() - started, 4.0)

    def test_subprocess_isolation_bounds_external_buffer_oom(self):
        # External ArrayBuffer-буферы (Uint8Array) обходят max_memory V8 —
        # поэтому grade_html исполняет артефакт в дочернем процессе. Эта аллокация
        # убивает/зависает ТОЛЬКО дочерний процесс (OOM-киллер ОС или wall-clock
        # дедлайн); родитель получает exec_error, а не OOM-килл.
        if grading.create_engine("mini-racer") is None:
            self.skipTest("mini-racer не установлен")
        html = (b"<script>function calculateFine(x){"
                b"var a=[];while(true){a.push(new Uint8Array(64*1024*1024).fill(1))}}"
                b"</script>")
        started = time.monotonic()
        grade = grading.grade_html(
            html, matrix=(_case(),), prefer_engine="mini-racer",
            eval_timeout_sec=1.0, max_memory_bytes=1024 * 1024 * 1024,
        )
        self.assertIn(grade.status, {
            grading.GRADE_STATUS_EXEC_ERROR,
            grading.GRADE_STATUS_NO_ADAPTER,
        })
        # дочерний ограничен wall-clock дедлайном — не висит
        self.assertLess(time.monotonic() - started, 30.0)


@unittest.skipUnless(grading.create_engine(), "JS-движок не установлен")
class GradeCliTests(unittest.TestCase):
    def test_cli_reads_temporary_db_scores_artifact_and_does_not_mutate_db(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                report = {
                    "project": "library_fine",
                    "provider": "provider",
                    "model": "model",
                    "started_at": "2026-07-15T01:00:00",
                    "summary": {"ok": 1, "timeout": 0, "error": 0},
                    "runs": [{"index": 1, "status": "готово", "code": 0}],
                }
                with conn:
                    db.upsert_report(
                        conn,
                        report,
                        "data/result/library_fine/report.json",
                        json.dumps(report),
                        artifacts=[_artifact(1, "calculator.html", REFERENCE_HTML)],
                    )
            finally:
                conn.close()

            before = _db_snapshot(db_path)
            output = io.StringIO()
            argv = ["grade_library_fine.py", "--db", str(db_path)]
            with mock.patch.object(sys, "argv", argv), redirect_stdout(output):
                code = grade_cli.main()
            self.assertEqual(code, 0)
            self.assertIn("score=34/34", output.getvalue())
            self.assertIn("autonomous=yes", output.getvalue())
            self.assertEqual(_db_snapshot(db_path), before)


if __name__ == "__main__":
    unittest.main()

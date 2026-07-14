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
        error=None,
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
  var baseRate = c.category ? 75 : 25;
  var studentNumerator = 100;
  var grace = c.category ? 5 : 8;
  var pensionerDiscount = Boolean(c.pensioner);
  if (c.student) {
    baseRate = 25;
    studentNumerator = 77;
    grace = 8;
  }
  if (c.student && c.pensioner) {
    baseRate = -baseRate;
    pensionerDiscount = false;
  }
  var total = 0;
  for (var n = 1; n <= days; n += 1) {
    var graceNumerator = n <= grace ? 21 + 10 * (n - 1) : 100;
    total += ceilFraction(baseRate * studentNumerator * graceNumerator, 10000);
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
        )
        for case, expected in cases:
            with self.subTest(expected=expected, case=case):
                self.assertEqual(grading.reference_fine(case), expected)

    def test_i5_uses_negative_25_then_discounts_and_never_point_46(self):
        case = next(c for c in grading.TEST_MATRIX
                    if c.name == "cat1_stu1_pen1_rep1_far_beyond_big")
        # 11 дней: ceil10(-25 × .77 × grace_n) суммарно -70; +240 = 170.
        daily = [
            grading.ceil10(Fraction(-25 * 77 * (21 + 10 * (n - 1)), 10_000))
            if n <= 8 else grading.ceil10(Fraction(-25 * 77, 100))
            for n in range(1, 12)
        ]
        manual = sum(daily) + grading.REPEAT_SURCHARGE
        self.assertEqual(manual, 170)
        self.assertEqual(grading.reference_fine(case), manual)
        # Если бы пенсионерское ×0.46 применялось, итог был бы 80, а не 170.
        self.assertEqual(grading.ceil10(Fraction(manual * 46, 100)), 80)


class MatrixTests(unittest.TestCase):
    def test_matrix_is_deterministic_unique_and_rule_complete(self):
        rebuilt = grading._build_matrix()
        self.assertEqual(rebuilt, grading.TEST_MATRIX)
        self.assertEqual(len(grading.TEST_MATRIX), 325)
        names = [case.name for case in grading.TEST_MATRIX]
        self.assertEqual(len(names), len(set(names)))
        for tag in (f"r{rule}" for rule in range(2, 11)):
            with self.subTest(tag=tag):
                count = sum(tag in case.tags for case in grading.TEST_MATRIX)
                self.assertGreaterEqual(count, 2)

    def test_matrix_contains_all_flag_combinations_and_i5_discriminators(self):
        flags = {
            (case.category, case.student, case.pensioner, case.repeat)
            for case in grading.TEST_MATRIX
            if not case.name.startswith("special_")
        }
        self.assertEqual(len(flags), 16)
        discriminators = [
            case for case in grading.TEST_MATRIX
            if case.category == case.student == case.pensioner == case.repeat == 1
            and "far_beyond" in case.name
        ]
        self.assertGreaterEqual(len(discriminators), 2)

    def test_expected_vector_matches_committed_snapshot(self):
        fixture = json.loads(
            (ROOT / "tests" / "data" / "library_fine_expected.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(grading.expected_vector(), fixture)


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
        if expr == "__gradeDiscover()":
            return self.found
        for (convention, rep), result in self.adapter_results.items():
            if (f'"conv": "{convention}"' in expr
                    and f'"rep": "{rep}"' in expr):
                return result
        return self.default_result


class EngineBoundaryTests(unittest.TestCase):
    _HTML = b"<html><script>function calculateFine(x) { return 0; }</script></html>"

    def _grade_with(self, engine: _FakeEngine,
                    matrix: tuple[grading.FineCase, ...]) -> grading.HtmlGrade:
        with mock.patch.object(grading, "create_engine",
                               return_value=lambda: engine):
            return grading.grade_html(self._HTML, matrix=matrix)

    def test_best_adapter_maximizes_matches_then_numeric_results(self):
        matrix = (_case("one"), _case("repeat", repeat=1))
        engine = _FakeEngine(
            found={"name": "calculateFine", "arity": 1},
            adapter_results={
                ("object", "date_local"): [{"v": 20}, {"v": 0}],
                ("object", "date_utc"): [{"v": 20}, {"v": 250}],
            },
            default_result=[{"e": "bad"}, {"e": "bad"}],
        )
        grade = self._grade_with(engine, matrix)
        self.assertEqual(grade.status, grading.GRADE_STATUS_GRADED)
        self.assertEqual(grade.adapter, "object+date_utc")
        self.assertEqual((grade.passed, grade.total), (2, 2))

    def test_unavailable_no_function_no_adapter_and_exec_error_statuses(self):
        matrix = (_case(),)
        with self.subTest(status="unavailable"):
            with mock.patch.object(grading, "create_engine", return_value=None):
                grade = grading.grade_html(self._HTML, matrix=matrix)
            self.assertEqual(grade.status, grading.GRADE_STATUS_UNAVAILABLE)

        with self.subTest(status="no_function"):
            grade = self._grade_with(_FakeEngine(found=None), matrix)
            self.assertEqual(grade.status, grading.GRADE_STATUS_NO_FUNCTION)

        with self.subTest(status="no_adapter"):
            engine = _FakeEngine(
                found={"name": "calculateFine", "arity": 1},
                default_result=[{"e": "non-numeric"}],
            )
            grade = self._grade_with(engine, matrix)
            self.assertEqual(grade.status, grading.GRADE_STATUS_NO_ADAPTER)

        with self.subTest(status="exec_error"):
            with mock.patch.object(grading, "create_engine",
                                   return_value=lambda: (_ for _ in ()).throw(
                                       RuntimeError("factory boom"))):
                grade = grading.grade_html(self._HTML, matrix=matrix)
            self.assertEqual(grade.status, grading.GRADE_STATUS_EXEC_ERROR)

    def test_top_level_failure_is_warning_when_function_can_still_be_called(self):
        matrix = (_case(),)
        engine = _FakeEngine(
            found={"name": "calculateFine", "arity": 1},
            adapter_results={("object", "date_local"): [{"v": 20}]},
            default_result=[{"e": "bad"}],
            fail_run_index=2,
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


@unittest.skipUnless(grading.create_engine(), "JS-движок не установлен")
class RealEngineIntegrationTests(unittest.TestCase):
    def test_reference_mirror_scores_full_matrix_on_both_backends(self):
        for backend in ("mini-racer", "quickjs"):
            with self.subTest(backend=backend):
                self.assertIsNotNone(grading.create_engine(backend))
                grade = grading.grade_html(REFERENCE_HTML, prefer_engine=backend)
                self.assertEqual(grade.status, grading.GRADE_STATUS_GRADED)
                self.assertEqual((grade.passed, grade.total), (325, 325))
                self.assertEqual(grade.adapter, "object+date_local")

    def test_positional_batch_and_dom_stub_conventions(self):
        matrix = (_case(),)
        samples = (
            (b"<script>function calculateFine(a,b,c,d,e,f,g){return 20}</script>",
             "positional7+date_local"),
            (b"<script>function calculateFine(n,a,b,c,d,e,f,g){return 20}</script>",
             "positional8+date_local"),
            (b"<script>function calculateFine(rows){return Array.isArray(rows)"
             b"?{results:[{fine:20}]}:{results:[]}}</script>",
             "batch+dmy"),
            (b"<script>document.getElementById('x').addEventListener('click',"
             b"function(){});function calculateFine(x){return {fine:20}}</script>",
             "object+date_local"),
        )
        for html, adapter in samples:
            with self.subTest(adapter=adapter):
                grade = grading.grade_html(html, matrix=matrix)
                self.assertEqual(grade.status, grading.GRADE_STATUS_GRADED)
                self.assertEqual((grade.passed, grade.total), (1, 1))
                self.assertEqual(grade.adapter, adapter)

    def test_real_engine_reports_missing_function(self):
        grade = grading.grade_html(
            b"<script>function unrelated(){return 1}</script>",
            matrix=(_case(),),
        )
        self.assertEqual(grade.status, grading.GRADE_STATUS_NO_FUNCTION)

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
            self.assertIn("score=325/325", output.getvalue())
            self.assertIn("autonomous=yes", output.getvalue())
            self.assertEqual(_db_snapshot(db_path), before)


if __name__ == "__main__":
    unittest.main()

"""issue #93 — index_builder enrichment: review_key/review_verdict/review_summary.

Контракт (тело #93):
- load_reports получает reports.id вместе с raw_json.
- reviews грузятся одним запросом и сопоставляются с runs[].questions.
- в in-memory question добавляются review_key (составной ключ для API) и
  review_verdict — ТОЛЬКО для оценённого вопроса.
- в planning-отчёт добавляется review_summary {total, reviewed, useful, unnecessary,
  useful_percent (null при reviewed=0), coverage_percent (0 при total=0)}.
- reports.raw_json после build_index остаётся байт-в-байт неизменным.
- review-записи для неоценённых вопросов НЕ создаются.

Гоняется на временной БД через сессию index_builder (как test_e2e._generate_index_json).
"""

import json
import tempfile
import unittest
from pathlib import Path

import db
import index_builder


def _question(**overrides):
    base = {
        "attempt_idx": 1, "session_id": "s", "request_id": "req",
        "round_idx": 1, "question_idx": 1, "header": "H",
        "question": "Какой формат?", "multiple": False, "custom": True,
        "options": [{"label": "JSON"}, {"label": "YAML"}],
        "answer": ["JSON"], "responder": "first", "fallback_used": False,
        "reply_status": "replied", "reply_error": None, "elapsed": 0.1,
    }
    base.update(overrides)
    return base


def _planning_report():
    return {
        "project": "plan", "provider": "v", "model": "m",
        "started_at": "2026-01-01", "copies": 1, "summary": {"ok": 1},
        "pricing": {"prompt_per_1m": 0.5, "completion_per_1m": 1.0},
        "planning": {"enabled": True, "agent": "bench_planner",
                     "responder": "first"},
        "planning_summary": {"questions": 2, "runs_with_questions": 1,
                             "recommended_matches": 0, "fallbacks_to_first": 0,
                             "reply_errors": 0},
        "runs": [
            {"index": 1, "port": 1, "dir": "d", "code": 0, "elapsed": 1.0,
             "questions": [
                 _question(request_id="q1", question_idx=1,
                           question="Первый вопрос?"),
                 _question(request_id="q2", question_idx=2,
                           question="Второй вопрос?"),
             ]},
        ],
    }


def _build_with_reviews(reports, reviews):
    """Собирает index.json во временной БД, накатывает reviews, возвращает data."""
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        db_path = root / "main.db"
        conn = db.connect(db_path)
        try:
            db.init_schema(conn)
            with conn:
                report_ids = []
                for i, rep in enumerate(reports):
                    rid = db.upsert_report(
                        conn, rep, f"data/result/r{i}.json",
                        json.dumps(rep, ensure_ascii=False, indent=2))
                    report_ids.append(rid)
                for rev in reviews:
                    db.put_question_review(conn, **rev)
        finally:
            conn.close()

        orig_connect = db.connect
        orig_root = index_builder.PROJECT_ROOT
        try:
            db.connect = lambda *a, **k: orig_connect(db_path)
            index_builder.PROJECT_ROOT = root
            index_builder.build_index()
            data = json.loads(
                (root / "docs" / "data" / "index.json").read_text())
        finally:
            db.connect = orig_connect
            index_builder.PROJECT_ROOT = orig_root
    return data, root, db_path


class ReviewEnrichmentTests(unittest.TestCase):
    def _review(self, report_id, request_id, verdict):
        # question_idx соответствует фикстуре _planning_report: q1→1, q2→2.
        question_idx = 1 if request_id == "q1" else 2
        return dict(report_id=report_id, run_idx=1, attempt_idx=1,
                    request_id=request_id, question_idx=question_idx,
                    verdict=verdict)

    def test_review_verdict_and_key_added_to_assessed_question(self):
        report = _planning_report()
        data, _root, _ = _build_with_reviews(
            [report], [self._review(1, "q1", "useful")])
        project = data["projects"][0]
        questions = project["reports"][0]["runs"][0]["questions"]

        # Первый вопрос оценён → review_verdict + review_key.
        self.assertEqual(questions[0]["review_verdict"], "useful")
        self.assertEqual(questions[0]["review_key"], {
            "report_id": 1, "run_idx": 1, "attempt_idx": 1,
            "request_id": "q1", "question_idx": 1,
        })
        # Второй не оценён → review_verdict отсутствует, review_key ЕСТЬ (нужен
        # для кнопок PUT даже на неоценённом вопросе).
        self.assertNotIn("review_verdict", questions[1])
        self.assertIn("review_key", questions[1])

    def test_review_summary_aggregates(self):
        report = _planning_report()
        # q1 → useful, q2 → unnecessary. total=2, reviewed=2.
        data, _, _ = _build_with_reviews(
            [report],
            [self._review(1, "q1", "useful"),
             self._review(1, "q2", "unnecessary")])
        summary = data["projects"][0]["reports"][0]["review_summary"]
        self.assertEqual(summary, {
            "total": 2, "reviewed": 2, "useful": 1, "unnecessary": 1,
            "useful_percent": 50.0, "coverage_percent": 100.0,
        })

    def test_review_summary_useful_percent_null_when_zero_reviewed(self):
        """reviewed=0 → useful_percent=null (неоценённые не ухудшают метрику),
        coverage_percent=0."""
        report = _planning_report()
        data, _, _ = _build_with_reviews([report], [])
        summary = data["projects"][0]["reports"][0]["review_summary"]
        self.assertEqual(summary, {
            "total": 2, "reviewed": 0, "useful": 0, "unnecessary": 0,
            "useful_percent": None, "coverage_percent": 0.0,
        })

    def test_review_summary_coverage_zero_when_no_questions(self):
        """total=0 → coverage_percent=0 (не деление на ноль)."""
        report = _planning_report()
        for run in report["runs"]:
            run["questions"] = []
        report["planning_summary"] = {
            "questions": 0, "runs_with_questions": 0,
            "recommended_matches": 0, "fallbacks_to_first": 0, "reply_errors": 0}
        data, _, _ = _build_with_reviews([report], [])
        summary = data["projects"][0]["reports"][0]["review_summary"]
        self.assertEqual(summary["total"], 0)
        self.assertEqual(summary["coverage_percent"], 0.0)
        self.assertIsNone(summary["useful_percent"])

    def test_raw_json_unchanged_after_build_index(self):
        """reports.raw_json байт-в-байт неизменен после build_index."""
        report = _planning_report()
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            db_path = root / "main.db"
            conn = db.connect(db_path)
            try:
                db.init_schema(conn)
                raw = json.dumps(report, ensure_ascii=False, indent=2)
                with conn:
                    rid = db.upsert_report(conn, report, "data/result/r0.json", raw)
                    db.put_question_review(
                        conn, report_id=rid, run_idx=1, attempt_idx=1,
                        request_id="q1", question_idx=1, verdict="useful")
                stored_before = conn.execute(
                    "SELECT raw_json FROM reports WHERE id=?", (rid,)).fetchone()[0]
            finally:
                conn.close()

            orig_connect = db.connect
            orig_root = index_builder.PROJECT_ROOT
            try:
                db.connect = lambda *a, **k: orig_connect(db_path)
                index_builder.PROJECT_ROOT = root
                index_builder.build_index()
            finally:
                db.connect = orig_connect
                index_builder.PROJECT_ROOT = orig_root

            conn = db.connect(db_path)
            try:
                stored_after = conn.execute(
                    "SELECT raw_json FROM reports WHERE id=?", (rid,)).fetchone()[0]
            finally:
                conn.close()
        self.assertEqual(stored_before, stored_after)
        # и в raw_json нет review-ключей
        parsed = json.loads(stored_after)
        for run in parsed["runs"]:
            for q in run.get("questions", []):
                self.assertNotIn("review_verdict", q)
                self.assertNotIn("review_key", q)

    def test_coding_report_has_no_review_summary(self):
        """Coding-отчёт (без planning) не получает review_summary и не падает."""
        coding = {
            "project": "cod", "provider": "v", "model": "m2",
            "started_at": "2026-01-01", "copies": 1, "summary": {"ok": 1},
            "pricing": {"prompt_per_1m": 0.5, "completion_per_1m": 1.0},
            "runs": [{"index": 1, "port": 1, "dir": "d", "code": 0,
                      "elapsed": 1.0}],
        }
        data, _, _ = _build_with_reviews([coding], [])
        report = data["projects"][0]["reports"][0]
        self.assertNotIn("review_summary", report)
        self.assertNotIn("planning", report)


if __name__ == "__main__":
    unittest.main()

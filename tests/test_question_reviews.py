"""issue #93 — ручная разметка planning-вопросов: слой БД + upsert-invariant.

Контракт (читай тело #93 дословно):
- question_reviews PK/FK = (report_id, run_idx, attempt_idx, request_id,
  question_idx) — как у agent_questions; FK на reports(id) ON DELETE CASCADE.
- verdict CHECK IN (useful, unnecessary); question_hash TEXT (считает сервер).
- upsert_report перед удалением agent_questions/runs сохраняет reviews в памяти,
  после вставки восстанавливает ТОЛЬКО совпавшие по 5-ключу И question_hash.
  created_at сохраняется, updated_at при restore НЕ меняется. Идемпотентно.

Тесты гоняются на :memory: БД (как test_planning.QuestionPersistenceTests) —
никакой реальной data/main.db, никакой сети.
"""

import json
import sqlite3
import unittest

from db import SCHEMA, upsert_report


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


def _report(questions=None):
    questions = questions if questions is not None else [_question()]
    return {
        "project": "p", "provider": "v", "model": "m",
        "started_at": "2026-01-01", "copies": 1, "summary": {},
        "runs": [{"index": 1, "port": 1, "dir": "d", "code": 0,
                  "elapsed": 1.0, "questions": questions}],
    }


def _fresh_conn():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(SCHEMA)
    return conn


class QuestionReviewSchemaTests(unittest.TestCase):
    def test_check_constraint_rejects_unknown_verdict(self):
        from db import put_question_review
        conn = _fresh_conn()
        rid = upsert_report(conn, _report(), "r", json.dumps(_report()))
        with self.assertRaises(sqlite3.IntegrityError):
            put_question_review(
                conn, report_id=rid, run_idx=1, attempt_idx=1,
                request_id="req", question_idx=1, verdict="bogus")
        conn.close()

    def test_fk_cascade_on_report_delete(self):
        """Удаление отчёта каскадно удаляет его reviews (FK на reports)."""
        from db import put_question_review
        conn = _fresh_conn()
        report = _report()
        rid = upsert_report(conn, report, "r", json.dumps(report))
        put_question_review(conn, report_id=rid, run_idx=1, attempt_idx=1,
                            request_id="req", question_idx=1, verdict="useful")
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM question_reviews").fetchone()[0], 1)
        conn.execute("DELETE FROM reports WHERE id=?", (rid,))
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM question_reviews").fetchone()[0], 0)
        conn.close()

    def test_question_hash_column_exists(self):
        conn = _fresh_conn()
        cols = {r[1] for r in conn.execute("PRAGMA table_info(question_reviews)")}
        self.assertIn("question_hash", cols)
        self.assertIn("verdict", cols)
        self.assertIn("created_at", cols)
        self.assertIn("updated_at", cols)
        conn.close()


class QuestionReviewCrudTests(unittest.TestCase):
    def test_put_creates_review_and_computes_hash(self):
        from db import put_question_review
        conn = _fresh_conn()
        report = _report()
        rid = upsert_report(conn, report, "r", json.dumps(report))
        result = put_question_review(
            conn, report_id=rid, run_idx=1, attempt_idx=1,
            request_id="req", question_idx=1, verdict="useful")
        self.assertEqual(result["verdict"], "useful")
        self.assertTrue(result["question_hash"])
        row = conn.execute(
            "SELECT * FROM question_reviews WHERE report_id=?", (rid,)).fetchone()
        self.assertEqual(row["verdict"], "useful")
        self.assertEqual(row["question_hash"], result["question_hash"])
        self.assertIsNotNone(row["created_at"])
        conn.close()

    def test_put_replaces_verdict_and_keeps_created_at(self):
        """Повторный PUT заменяет verdict; created_at не сбрасывается,
        updated_at обновляется."""
        from db import put_question_review
        conn = _fresh_conn()
        report = _report()
        rid = upsert_report(conn, report, "r", json.dumps(report))
        first = put_question_review(
            conn, report_id=rid, run_idx=1, attempt_idx=1,
            request_id="req", question_idx=1, verdict="useful")
        second = put_question_review(
            conn, report_id=rid, run_idx=1, attempt_idx=1,
            request_id="req", question_idx=1, verdict="unnecessary")
        self.assertEqual(second["verdict"], "unnecessary")
        self.assertEqual(second["created_at"], first["created_at"])
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM question_reviews").fetchone()[0], 1)
        conn.close()

    def test_delete_removes_review_idempotent(self):
        """DELETE убирает review; повторный DELETE — идемпотентен (без ошибки)."""
        from db import delete_question_review, put_question_review
        conn = _fresh_conn()
        report = _report()
        rid = upsert_report(conn, report, "r", json.dumps(report))
        put_question_review(
            conn, report_id=rid, run_idx=1, attempt_idx=1,
            request_id="req", question_idx=1, verdict="useful")
        delete_question_review(
            conn, report_id=rid, run_idx=1, attempt_idx=1,
            request_id="req", question_idx=1)
        self.assertEqual(conn.execute(
            "SELECT count(*) FROM question_reviews").fetchone()[0], 0)
        # повторное удаление — не падает
        delete_question_review(
            conn, report_id=rid, run_idx=1, attempt_idx=1,
            request_id="req", question_idx=1)
        conn.close()


class QuestionHashTests(unittest.TestCase):
    """question_hash считает ТОЛЬКО сервер из header/question/options/multiple/custom.

    Хеш стабилен при том же наборе и меняется при изменении любого из полей.
    """

    def test_hash_stable_for_same_question(self):
        from db import compute_question_hash
        q = _question()
        h1 = compute_question_hash(q)
        h2 = compute_question_hash(_question())
        self.assertEqual(h1, h2)

    def test_hash_changes_when_question_text_changes(self):
        from db import compute_question_hash
        h1 = compute_question_hash(_question(question="Какой формат?"))
        h2 = compute_question_hash(_question(question="Какой формат!?"))
        self.assertNotEqual(h1, h2)

    def test_hash_changes_when_options_change(self):
        from db import compute_question_hash
        h1 = compute_question_hash(
            _question(options=[{"label": "JSON"}, {"label": "YAML"}]))
        h2 = compute_question_hash(
            _question(options=[{"label": "JSON"}, {"label": "TOML"}]))
        self.assertNotEqual(h1, h2)

    def test_hash_changes_when_multiple_flag_changes(self):
        from db import compute_question_hash
        h1 = compute_question_hash(_question(multiple=False))
        h2 = compute_question_hash(_question(multiple=True))
        self.assertNotEqual(h1, h2)

    def test_hash_changes_when_header_changes(self):
        from db import compute_question_hash
        h1 = compute_question_hash(_question(header="H"))
        h2 = compute_question_hash(_question(header="H2"))
        self.assertNotEqual(h1, h2)

    def test_hash_independent_of_answer_and_responder(self):
        """answer/responder/reply_status НЕ входят в question_hash — только
        header/question/options/multiple/custom (что и закрепляет инвариант
        upsert-restore: изменение ответа не сбрасывает разметку)."""
        from db import compute_question_hash
        h1 = compute_question_hash(
            _question(answer=["JSON"], responder="first", reply_status="replied"))
        h2 = compute_question_hash(
            _question(answer=["YAML"], responder="recommended",
                      reply_status="captured"))
        self.assertEqual(h1, h2)


class UpsertReportRestoreReviewsTests(unittest.TestCase):
    """issue #93: upsert_report пересоздаёт agent_questions/runs (delete-then-insert),
    поэтому перед удалением сохраняет reviews в памяти и после вставки
    восстанавливает ТОЛЬКО совпавшие по 5-ключу И question_hash.

    - одинаковый ключ+хеш → review выживает (created_at сохранён, updated_at не
      меняется — это технический restore, не новое сохранение человеком);
    - изменился текст/options вопроса → review не восстанавливается;
    - вопрос исчез → review не восстанавливается;
    - повторный идентичный upsert идемпотентен.
    """

    def _seed_and_review(self, conn, report):
        from db import put_question_review
        rid = upsert_report(conn, report, "r", json.dumps(report))
        put_question_review(conn, report_id=rid, run_idx=1, attempt_idx=1,
                            request_id="req", question_idx=1, verdict="useful")
        return rid

    def test_review_survives_identical_reupsert_with_same_created_at(self):
        """Тот же отчёт → review восстановлен; created_at сохранён, updated_at НЕ
        сдвинут (технический restore, а не новое сохранение)."""
        conn = _fresh_conn()
        report = _report()
        rid = self._seed_and_review(conn, report)
        before = conn.execute(
            "SELECT created_at, updated_at FROM question_reviews "
            "WHERE report_id=?", (rid,)).fetchone()

        # повторный идентичный upsert
        rid2 = upsert_report(conn, report, "r", json.dumps(report))
        self.assertEqual(rid, rid2)

        rows = conn.execute(
            "SELECT verdict, created_at, updated_at, question_hash "
            "FROM question_reviews WHERE report_id=?", (rid,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "useful")
        self.assertEqual(rows[0]["created_at"], before["created_at"])
        self.assertEqual(rows[0]["updated_at"], before["updated_at"])
        conn.close()

    def test_review_lost_when_question_text_changes(self):
        """Изменился текст вопроса → question_hash расходится → review не restored."""
        conn = _fresh_conn()
        report = _report()
        rid = self._seed_and_review(conn, report)

        changed = _report()
        changed["runs"][0]["questions"][0]["question"] = "Совсем другой вопрос"
        upsert_report(conn, changed, "r", json.dumps(changed))

        self.assertEqual(conn.execute(
            "SELECT count(*) FROM question_reviews WHERE report_id=?",
            (rid,)).fetchone()[0], 0)
        conn.close()

    def test_review_lost_when_options_change(self):
        """Изменились options → hash расходится → review не restored."""
        conn = _fresh_conn()
        report = _report()
        rid = self._seed_and_review(conn, report)

        changed = _report()
        changed["runs"][0]["questions"][0]["options"] = [{"label": "JSON"},
                                                         {"label": "TOML"}]
        upsert_report(conn, changed, "r", json.dumps(changed))

        self.assertEqual(conn.execute(
            "SELECT count(*) FROM question_reviews WHERE report_id=?",
            (rid,)).fetchone()[0], 0)
        conn.close()

    def test_review_lost_when_question_disappears(self):
        """Вопрос удалён из отчёта → review не restored (восстанавливать некуда)."""
        conn = _fresh_conn()
        report = _report()
        rid = self._seed_and_review(conn, report)

        changed = _report(questions=[])  # копия без вопросов
        upsert_report(conn, changed, "r", json.dumps(changed))

        self.assertEqual(conn.execute(
            "SELECT count(*) FROM question_reviews WHERE report_id=?",
            (rid,)).fetchone()[0], 0)
        conn.close()

    def test_review_survives_when_only_answer_changes(self):
        """answer/responder/reply_status НЕ входят в question_hash — смена ответа
        НЕ должна сбрасывать разметку (восстанавливается по совпадающему хешу)."""
        conn = _fresh_conn()
        report = _report()
        rid = self._seed_and_review(conn, report)

        changed = _report()
        changed["runs"][0]["questions"][0]["answer"] = ["YAML"]
        changed["runs"][0]["questions"][0]["reply_status"] = "captured"
        upsert_report(conn, changed, "r", json.dumps(changed))

        rows = conn.execute(
            "SELECT verdict FROM question_reviews WHERE report_id=?",
            (rid,)).fetchall()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["verdict"], "useful")
        conn.close()


if __name__ == "__main__":
    unittest.main()
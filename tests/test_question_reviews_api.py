"""issue #93 — локальный API разметки planning-вопросов (слой 3).

Контракт (тело #93):
- GET /api/capabilities → 200 {"question_reviews": true}.
- PUT /api/question-reviews — JSON с составным ключом + verdict; сервер проверяет
  существование agent_questions и сам считает question_hash; ответ 200 со
  сохранённым verdict.
- DELETE /api/question-reviews — тот же ключ без verdict; 200 с verdict=null;
  повторное удаление идемпотентно.
- 400 (malformed JSON / нет поля / неверный тип / неизвестный verdict),
  404 (неизвестный вопрос), 500 (tx-ошибка без изменения БД), стандартный 404/405
  для неверного path/method. Лимит тела 16 KiB. 127.0.0.1, same-origin, без CORS.

Production-Handler вынесен из serve() в тестируемую фабрику make_dashboard_handler
(см. dashboard_server) — тесты НЕ дублируют его логику, а инстанцируют фабрику с
временной БД. Запросы — реальные (urllib, loopback, эфемерный порт).
"""

import functools
import json
import shutil
import socketserver
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

import dashboard_server
import db

REQUEST_TIMEOUT = 5.0
MAX_BODY = 16 * 1024


def _planning_question(**overrides):
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


def _seed_db(db_path: Path) -> int:
    """Создаёт временную БД с одним planning-отчётом, возвращает report_id."""
    report = {
        "project": "p", "provider": "v", "model": "m",
        "started_at": "2026-01-01", "copies": 1, "summary": {},
        "planning": {"enabled": True, "agent": "bench_planner",
                     "responder": "first"},
        "runs": [{"index": 1, "port": 1, "dir": "d", "code": 0,
                  "elapsed": 1.0, "questions": [_planning_question()]}],
    }
    conn = db.connect(db_path)
    try:
        db.init_schema(conn)
        with conn:
            rid = db.upsert_report(conn, report, "r", json.dumps(report))
    finally:
        conn.close()
    return rid


class _ApiHandlerFactoryHarness(unittest.TestCase):
    """Общий сетап: эфемерный сервер на фабрике make_dashboard_handler с врем. БД."""

    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        self._docs = Path(self._tmp) / "docs"
        (self._docs / "data").mkdir(parents=True)
        (self._docs / "index.html").write_text("<html>dash</html>",
                                               encoding="utf-8")
        self._db_path = Path(self._tmp) / "main.db"
        self.report_id = _seed_db(self._db_path)

        handler_cls = dashboard_server.make_dashboard_handler(self._db_path)
        handler = functools.partial(handler_cls, directory=str(self._docs))
        self._httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self._port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self._httpd.shutdown()
        self._thread.join(timeout=5)
        self._httpd.server_close()

    def _key(self):
        return {
            "report_id": self.report_id, "run_idx": 1, "attempt_idx": 1,
            "request_id": "req", "question_idx": 1,
        }

    # --- низкоуровневые HTTP-хелперы ---
    def _request(self, method, path, body=None, raw_body=None):
        url = f"http://127.0.0.1:{self._port}{path}"
        data = None
        headers = {}
        if raw_body is not None:
            data = raw_body.encode("utf-8")
        elif body is not None:
            data = json.dumps(body).encode("utf-8")
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
            return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def _get(self, path):
        url = f"http://127.0.0.1:{self._port}{path}"
        with urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT) as resp:
            return resp.status, resp.read(), dict(resp.headers)


class CapabilitiesTests(_ApiHandlerFactoryHarness):
    def test_get_capabilities_returns_question_reviews_true(self):
        status, body = self._request("GET", "/api/capabilities")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        # capabilities растёт со временем (issue #110 добавил delete_project,
        # issue #168 — provider_quota) — проверяем нужные флаги, а не точное
        # равенство всего словаря.
        self.assertIs(payload.get("question_reviews"), True)

    def test_get_capabilities_returns_provider_quota_true(self):
        # issue #168: раздел квот провайдеров — только локальный serve.
        # Фронт рендерит панель при capabilities.provider_quota === true.
        status, body = self._request("GET", "/api/capabilities")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIs(payload.get("provider_quota"), True)

    def test_capabilities_no_cors_headers(self):
        """API без CORS: заголовки Access-Control-* отсутствуют."""
        status, _body, headers = self._get("/api/capabilities")
        self.assertEqual(status, 200)
        for key in headers:
            self.assertFalse(key.lower().startswith("access-control-"), key)


class PutReviewTests(_ApiHandlerFactoryHarness):
    def test_put_creates_review_returns_verdict(self):
        status, body = self._request("PUT", "/api/question-reviews",
                                     {**self._key(), "verdict": "useful"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"verdict": "useful"})

    def test_put_replaces_verdict(self):
        self._request("PUT", "/api/question-reviews",
                      {**self._key(), "verdict": "useful"})
        status, body = self._request("PUT", "/api/question-reviews",
                                     {**self._key(), "verdict": "unnecessary"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"verdict": "unnecessary"})

    def test_put_unknown_question_returns_404(self):
        body = {**self._key(), "request_id": "no-such-req", "verdict": "useful"}
        status, _ = self._request("PUT", "/api/question-reviews", body)
        self.assertEqual(status, 404)

    def test_put_unknown_verdict_returns_400(self):
        body = {**self._key(), "verdict": "bogus"}
        status, _ = self._request("PUT", "/api/question-reviews", body)
        self.assertEqual(status, 400)

    def test_put_missing_field_returns_400(self):
        body = {**self._key(), "verdict": "useful"}
        del body["verdict"]
        status, _ = self._request("PUT", "/api/question-reviews", body)
        self.assertEqual(status, 400)

    def test_put_wrong_type_returns_400(self):
        body = {**self._key(), "question_idx": "not-int", "verdict": "useful"}
        status, _ = self._request("PUT", "/api/question-reviews", body)
        self.assertEqual(status, 400)

    def test_put_malformed_json_returns_400(self):
        status, _ = self._request("PUT", "/api/question-reviews",
                                  raw_body="{ не json")
        self.assertEqual(status, 400)

    def test_put_body_over_16kib_returns_400(self):
        big = {**self._key(), "verdict": "useful",
               "padding": "x" * (MAX_BODY + 100)}
        status, _ = self._request("PUT", "/api/question-reviews",
                                  raw_body=json.dumps(big))
        self.assertEqual(status, 400)


class DeleteReviewTests(_ApiHandlerFactoryHarness):
    def test_delete_existing_review_returns_null(self):
        self._request("PUT", "/api/question-reviews",
                      {**self._key(), "verdict": "useful"})
        status, body = self._request("DELETE", "/api/question-reviews",
                                     self._key())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"verdict": None})

    def test_delete_idempotent(self):
        # Удаление без предшествующего PUT — тоже 200 null.
        status, body = self._request("DELETE", "/api/question-reviews",
                                     self._key())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body), {"verdict": None})

    def test_delete_missing_field_returns_400(self):
        body = self._key()
        del body["question_idx"]
        status, _ = self._request("DELETE", "/api/question-reviews", body)
        self.assertEqual(status, 400)


class MethodPathRoutingTests(_ApiHandlerFactoryHarness):
    def test_post_to_reviews_returns_405(self):
        status, _ = self._request("POST", "/api/question-reviews",
                                  {**self._key(), "verdict": "useful"})
        self.assertEqual(status, 405)

    def test_unknown_api_path_get_returns_404(self):
        status, _ = self._request("GET", "/api/nope")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()

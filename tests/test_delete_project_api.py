"""issue #110 — локальный API удаления проекта (слой dashboard_server).

Контракт (тело #110):
- DELETE /api/projects/<name> — success 200 со структурой (reports/runs/artifacts);
  404 несуществующий проект (не частичный успех); 400 невалидное имя;
  409 при активном benchmark-прогоне; повторный запрос после успеха не повреждает
  другие данные (второй раз → 404).
- GET /api/capabilities → 200 включает delete_project:true.
- API локальный (127.0.0.1, same-origin, без CORS); на Pages эндпоинта нет.
- Файловая очистка data/result/<name>/ — после успешного commit БД.

Тесты инстанцируют production-Handler через make_dashboard_handler с временной
БД и временным data/result (патч WORK_ROOT), поднимают эфемерный loopback-сервер.
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

import artifacts
import dashboard_server
import db

REQUEST_TIMEOUT = 5.0


def _report(project, model="m", started_at="2026-01-01T00:00:00"):
    return {
        "project": project, "provider": "v", "model": model,
        "prompt": "t", "copies": 1, "started_at": started_at,
        "run_elapsed": 1.0, "summary": {"ok": 1, "timeout": 0, "error": 0},
        "runs": [{"index": 0, "port": 4000, "dir": "/x", "status": "готово",
                  "code": 0, "elapsed": 10.0}],
    }


def _seed(db_path, projects):
    """Сидирует по одному отчёту и строке библиотеки на каждый проект в списке."""
    conn = db.connect(db_path)
    try:
        db.init_schema(conn)
        with conn:
            for name in projects:
                conn.execute(
                    "INSERT INTO projects_library "
                    "(name, description, prompt, what_it_tests, raw_json) "
                    "VALUES (?, '', '', '[]', ?)",
                    (name, json.dumps({"name": name})))
                db.upsert_report(conn, _report(name), f"{name}.json",
                                 json.dumps({"p": name}))
    finally:
        conn.close()


class _DeleteApiHarness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(self._tmp, ignore_errors=True))
        self._docs = Path(self._tmp) / "docs"
        (self._docs / "data").mkdir(parents=True)
        (self._docs / "index.html").write_text("<html>dash</html>",
                                               encoding="utf-8")
        self._db_path = Path(self._tmp) / "main.db"
        self._result_root = Path(self._tmp) / "result"
        self._result_root.mkdir()
        _seed(self._db_path, ["alpha", "beta"])
        # data/result-каталоги обоих проектов на диске.
        for name in ("alpha", "beta"):
            (self._result_root / name / "v_m" / "1").mkdir(parents=True)
            (self._result_root / name / "v_m" / "1" / "run.log").write_text(
                "log", encoding="utf-8")

        # Патчим WORK_ROOT, на который смотрит файловая очистка API.
        self._orig_work_root = artifacts_work_root()
        set_artifacts_work_root(self._result_root)
        self.addCleanup(lambda: set_artifacts_work_root(self._orig_work_root))

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

    def _request(self, method, path, raw_body=None):
        url = f"http://127.0.0.1:{self._port}{path}"
        data = raw_body.encode("utf-8") if raw_body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
            return resp.status, resp.read(), dict(resp.headers)
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read(), dict(exc.headers)

    def _project_count(self, name):
        conn = db.connect(self._db_path)
        try:
            return conn.execute(
                "SELECT count(*) FROM reports WHERE project=?",
                (name,)).fetchone()[0]
        finally:
            conn.close()


# Хелперы патча WORK_ROOT: API-очистка читает artifacts.WORK_ROOT? Нет — WORK_ROOT
# живёт в opencode_runtime. dashboard_server использует свой источник каталога
# результатов. Держим доступ через dashboard_server, чтобы тест не знал деталей.
def artifacts_work_root():
    return dashboard_server.RESULT_ROOT


def set_artifacts_work_root(path):
    dashboard_server.RESULT_ROOT = path


class CapabilityTests(_DeleteApiHarness):
    def test_capabilities_includes_delete_project(self):
        status, body, _ = self._request("GET", "/api/capabilities")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertIs(payload.get("delete_project"), True)


class DeleteProjectSuccessTests(_DeleteApiHarness):
    def test_delete_success_returns_counts_and_removes_data(self):
        status, body, headers = self._request("DELETE", "/api/projects/alpha")
        self.assertEqual(status, 200)
        payload = json.loads(body)
        self.assertEqual(payload["project"], "alpha")
        self.assertEqual(payload["reports"], 1)
        self.assertIn("runs", payload)
        self.assertIn("artifacts", payload)
        # БД: alpha исчез, beta цел.
        self.assertEqual(self._project_count("alpha"), 0)
        self.assertEqual(self._project_count("beta"), 1)
        # Файлы alpha удалены после commit, beta на месте.
        self.assertFalse((self._result_root / "alpha").exists())
        self.assertTrue((self._result_root / "beta").exists())
        # без CORS
        for key in headers:
            self.assertFalse(key.lower().startswith("access-control-"), key)

    def test_delete_missing_data_dir_still_succeeds(self):
        # Проект без каталога на диске (артефакты только в БД) — удаляется штатно.
        shutil.rmtree(self._result_root / "alpha")
        status, _body, _ = self._request("DELETE", "/api/projects/alpha")
        self.assertEqual(status, 200)
        self.assertEqual(self._project_count("alpha"), 0)


class DeleteProjectErrorTests(_DeleteApiHarness):
    def test_delete_unknown_project_returns_404(self):
        status, _body, _ = self._request("DELETE", "/api/projects/ghost")
        self.assertEqual(status, 404)
        # ничего не удалено
        self.assertEqual(self._project_count("alpha"), 1)
        self.assertEqual(self._project_count("beta"), 1)

    def test_delete_invalid_name_returns_400(self):
        # Пустое имя → 400 ИЛИ 404 (роутинг /api/projects/ без сегмента —
        # зависит от диспетчера; оба недопустимы к удалению).
        status, _body, _ = self._request("DELETE", "/api/projects/")
        self.assertIn(status, (400, 404))
        # Path-обход после декодирования (%2e%2e/.., %2F/) обязан дать строго
        # 400: handler декодирует и видит слэш. 404 тут был бы регрессией
        # (не задекодировал → не сматчил роут → фолл-через к другому хендлеру).
        for bad in ("/api/projects/%2e%2e%2falpha", "/api/projects/a%2Fb"):
            status, _body, _ = self._request("DELETE", bad)
            self.assertEqual(status, 400, bad)
        # реальные проекты не пострадали
        self.assertEqual(self._project_count("alpha"), 1)

    def test_repeated_delete_returns_404_second_time(self):
        first, _b, _h = self._request("DELETE", "/api/projects/alpha")
        self.assertEqual(first, 200)
        second, _b2, _h2 = self._request("DELETE", "/api/projects/alpha")
        self.assertEqual(second, 404)
        # beta цел после обоих запросов
        self.assertEqual(self._project_count("beta"), 1)

    def test_delete_refused_when_active_run(self):
        # Живой .bench-active.json marker в alpha → 409, ничего не удалено.
        import os
        copy_dir = self._result_root / "alpha" / "v_m" / "1"
        artifacts.write_run_active_marker(copy_dir, pid=os.getpid())
        status, _body, _ = self._request("DELETE", "/api/projects/alpha")
        self.assertEqual(status, 409)
        self.assertEqual(self._project_count("alpha"), 1)
        self.assertTrue((self._result_root / "alpha").exists())


class MethodRoutingTests(_DeleteApiHarness):
    def test_get_on_projects_path_returns_404_or_405(self):
        status, _body, _ = self._request("GET", "/api/projects/alpha")
        self.assertIn(status, (404, 405))


if __name__ == "__main__":
    unittest.main()

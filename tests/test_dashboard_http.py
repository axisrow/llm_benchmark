"""HTTP-обработчик дашборда (issue #38, P0).

Закрывает дыру: для GET-пути `dashboard_server.serve` ранее не было ни одного
теста. Два уровня:
- `DashboardHttpHandlerTests` — контрактные тесты обвязки (`_maybe_rebuild` +
  `do_GET`/`do_HEAD`) на её копии `_build_handler` с инъекцией build_index/
  fingerprint: удобно гонять rebuild-логику изолированно;
- `DashboardServeProductionTests` — поднимает НАСТОЯЩУЮ `serve()` в потоке
  (PROJECT_ROOT→временный docs, build_index→фейк), чтобы регресс в самой
  `serve()` (path-guard, отдача из docs, стартовый build_index, cleanup
  снимка) был пойман, а не маскировался копией.
Запросы — реальные, через urllib, с таймаутом; сервер на эфемерном 127.0.0.1:0.

Проверяется:
- GET существующих файлов → 200 + корректный Content-Type (text/html, json);
- GET отсутствующего файла → 404;
- авто-пересборка: GET /data/index.json вызывает build_index ровно один раз,
  когда меняется отпечаток базы (`_db_fingerprint`), и не вызывает при
  неизменном отпечатке; запросы к другим путям пересборку не триггерят.

Сети наружу нет (только loopback), реальная data/main.db не трогается — индекс
генерируется настоящим build_index() против временной БД во временном docs-каталоге.
"""

import functools
import http.server
import json
import os
import shutil
import socketserver
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

import dashboard_server
import db
import index_builder

REQUEST_TIMEOUT = 5.0


def _sample_report():
    return {
        "project": "fast_sort", "provider": "zai", "model": "glm-5.1",
        "prompt": "task", "description": "desc", "what_it_tests": "сортировка",
        "copies": 1, "started_at": "2026-01-01T00:00:00", "run_elapsed": 12.0,
        "summary": {"ok": 1, "timeout": 0, "error": 0, "rate_limited": 0},
        "pricing": {"prompt_per_1m": 0.5, "completion_per_1m": 1.0},
        "usage_summary": {"input_tokens": 100, "output_tokens": 10,
                          "total_tokens": 110, "estimated_cost_usd": 0.001,
                          "runs_with_usage": 1, "runs_with_estimated_cost": 1},
        "artifact_summary": {"files": 0},
        "runs": [{"index": 0, "port": 4096, "dir": "/x", "status": "готово",
                  "code": 0, "elapsed": 12.0, "usage": None}],
    }


def _generate_index_json(out_root: Path) -> None:
    """Пишет out_root/docs/data/index.json настоящим build_index против врем. БД."""
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "main.db"
        conn = db.connect(db_path)
        try:
            db.init_schema(conn)
            with conn:
                rep = _sample_report()
                db.upsert_report(conn, rep, "data/result/r.json", json.dumps(rep))
        finally:
            conn.close()

        # index_builder мигрирован на db.session() (PR #39) — патчим db.connect
        # (его зовёт session) на временную базу; PROJECT_ROOT задаёт вывод docs.
        orig_connect = db.connect
        orig_root = index_builder.PROJECT_ROOT
        try:
            db.connect = lambda *a, **k: orig_connect(db_path)
            index_builder.PROJECT_ROOT = out_root
            index_builder.build_index()
        finally:
            db.connect = orig_connect
            index_builder.PROJECT_ROOT = orig_root


def _build_handler(docs_dir: Path, *, rebuild_hook=None, db_fingerprint=None):
    """Воспроизводит обвязку Handler из dashboard_server.serve().

    Логика _maybe_rebuild/do_GET/do_HEAD скопирована один-в-один; вместо
    модульных build_index/_db_fingerprint подставляются переданные коллбэки,
    чтобы наблюдать пересборку без реальной data/main.db.
    """
    state = {"last_fp": 0.0}

    def fingerprint():
        return db_fingerprint() if db_fingerprint else 0.0

    def rebuild():
        if rebuild_hook:
            rebuild_hook()

    class Handler(http.server.SimpleHTTPRequestHandler):
        def _maybe_rebuild(self):
            if self.path.split("?", 1)[0] != "/data/index.json":
                return
            fp = fingerprint()
            if fp == state["last_fp"]:
                return
            try:
                rebuild()
                state["last_fp"] = fp
            except Exception:  # noqa: BLE001 — как в проде: молча логируется
                pass

        def do_GET(self):
            self._maybe_rebuild()
            super().do_GET()

        def do_HEAD(self):
            self._maybe_rebuild()
            super().do_HEAD()

        def log_message(self, *args):
            pass  # тихо

    return functools.partial(Handler, directory=str(docs_dir)), state


class DashboardHttpHandlerTests(unittest.TestCase):
    """Реальные GET-запросы к эфемерному серверу с обвязкой как в serve()."""

    def _serve(self, docs_dir, *, rebuild_hook=None, db_fingerprint=None):
        handler, state = _build_handler(
            docs_dir, rebuild_hook=rebuild_hook, db_fingerprint=db_fingerprint)
        httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()

        def shutdown():
            httpd.shutdown()
            thread.join(timeout=5)
            httpd.server_close()

        self.addCleanup(shutdown)
        return port, state

    def _docs_with_index(self):
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(td, ignore_errors=True))
        work = Path(td)
        docs = work / "docs"
        (docs / "data").mkdir(parents=True)
        (docs / "index.html").write_text(
            "<!doctype html><title>dash</title>", encoding="utf-8")
        _generate_index_json(work)  # пишет docs/data/index.json настоящим build_index
        return docs

    def _get(self, port, path):
        url = f"http://127.0.0.1:{port}{path}"
        return urllib.request.urlopen(url, timeout=REQUEST_TIMEOUT)

    def test_get_index_html_returns_200_text_html(self):
        docs = self._docs_with_index()
        port, _ = self._serve(docs)
        with self._get(port, "/index.html") as resp:
            self.assertEqual(resp.status, 200)
            self.assertTrue(
                resp.headers.get_content_type().startswith("text/html"),
                resp.headers.get("Content-Type"))
            self.assertIn(b"dash", resp.read())

    def test_get_index_json_returns_200_application_json(self):
        docs = self._docs_with_index()
        port, _ = self._serve(docs)
        with self._get(port, "/data/index.json") as resp:
            self.assertEqual(resp.status, 200)
            self.assertEqual(resp.headers.get_content_type(), "application/json")
            payload = json.loads(resp.read())
        # Содержимое — настоящий индекс build_index с нашим отчётом.
        self.assertEqual(payload["total"], 1)
        self.assertIn("model_ranking", payload)

    def test_get_missing_file_returns_404(self):
        docs = self._docs_with_index()
        port, _ = self._serve(docs)
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._get(port, "/no-such-file.txt")
        self.assertEqual(ctx.exception.code, 404)

    def test_rebuild_runs_once_when_fingerprint_changes(self):
        docs = self._docs_with_index()
        calls = []
        fp_box = {"value": 1.0}
        port, state = self._serve(
            docs,
            rebuild_hook=lambda: calls.append(1),
            db_fingerprint=lambda: fp_box["value"])

        # Стартовый last_fp == 0.0; первый запрос видит fp=1.0 → одна пересборка.
        with self._get(port, "/data/index.json") as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(len(calls), 1)
        self.assertEqual(state["last_fp"], 1.0)

        # Отпечаток не изменился → повторных пересборок нет.
        with self._get(port, "/data/index.json") as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(len(calls), 1)

        # Отпечаток сменился → ровно одна новая пересборка.
        fp_box["value"] = 2.0
        with self._get(port, "/data/index.json") as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(len(calls), 2)
        self.assertEqual(state["last_fp"], 2.0)

    def test_rebuild_not_triggered_for_other_paths(self):
        docs = self._docs_with_index()
        calls = []
        port, _ = self._serve(
            docs,
            rebuild_hook=lambda: calls.append(1),
            db_fingerprint=lambda: 7.0)

        # Запрос к не-index пути не должен трогать пересборку, несмотря на fp != last_fp.
        with self._get(port, "/index.html") as resp:
            self.assertEqual(resp.status, 200)
        self.assertEqual(calls, [])

    def test_real_db_fingerprint_drives_rebuild(self):
        """Сквозной тест с настоящим dashboard_server._db_fingerprint против врем. БД."""
        docs = self._docs_with_index()
        td = tempfile.mkdtemp()
        self.addCleanup(lambda: shutil.rmtree(td, ignore_errors=True))
        db_file = Path(td) / "main.db"
        db_file.write_bytes(b"x")
        os.utime(db_file, (1000, 1000))

        calls = []
        orig_db_path = dashboard_server.DB_PATH
        dashboard_server.DB_PATH = db_file
        try:
            port, _ = self._serve(
                docs,
                rebuild_hook=lambda: calls.append(1),
                db_fingerprint=dashboard_server._db_fingerprint)

            with self._get(port, "/data/index.json"):
                pass
            self.assertEqual(len(calls), 1)  # mtime 1000 != стартовый 0.0

            with self._get(port, "/data/index.json"):
                pass
            self.assertEqual(len(calls), 1)  # mtime тот же → без пересборки

            os.utime(db_file, (9000, 9000))  # реально меняем mtime базы
            with self._get(port, "/data/index.json"):
                pass
            self.assertEqual(len(calls), 2)
        finally:
            dashboard_server.DB_PATH = orig_db_path


class DashboardServeProductionTests(unittest.TestCase):
    """Сквозной тест НАСТОЯЩЕЙ dashboard_server.serve() (не копии обвязки).

    serve() строит Handler внутри себя и блокируется на serve_forever(); тут он
    поднимается в фоновом потоке с PROJECT_ROOT→временный docs и build_index→
    фейк. Так реально исполняется продовая обвязка (path-guard do_GET/_maybe_
    rebuild, отдача из docs, стартовый build_index, cleanup_index_snapshot),
    а не её ручная копия из _build_handler — регресс в serve() будет пойман.
    """

    def test_serve_real_handler_serves_and_rebuilds(self):
        work = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(work, ignore_errors=True))
        (work / "docs" / "data").mkdir(parents=True)
        (work / "docs" / "index.html").write_text(
            "<html><body>dash</body></html>", encoding="utf-8")

        index_path = work / "docs" / "data" / "index.json"
        build_calls = []

        def fake_build_index():
            build_calls.append(1)
            index_path.write_text(json.dumps({"total": 0}), encoding="utf-8")
            return 0

        # Захватываем инстанс TCPServer, чтобы остановить serve() извне, и
        # принудительно слушаем эфемерный порт (serve() передаёт port=0).
        created = {}
        real_tcp_server = socketserver.TCPServer

        def capturing_tcp_server(addr, handler_cls):
            srv = real_tcp_server(addr, handler_cls)
            created["srv"] = srv
            created["port"] = srv.server_address[1]
            return srv

        orig_root = dashboard_server.PROJECT_ROOT
        orig_build = dashboard_server.build_index
        orig_fp = dashboard_server._db_fingerprint
        # Восстановление через addCleanup — отработает ДАЖЕ если assert в теле
        # упадёт; иначе fake build_index / temp PROJECT_ROOT протекут в другие
        # тесты (test_bench, test_coverage_gaps), захватив их как «оригиналы».
        self.addCleanup(setattr, dashboard_server, "PROJECT_ROOT", orig_root)
        self.addCleanup(setattr, dashboard_server, "build_index", orig_build)
        self.addCleanup(setattr, dashboard_server, "_db_fingerprint", orig_fp)
        dashboard_server.PROJECT_ROOT = work
        dashboard_server.build_index = fake_build_index
        dashboard_server._db_fingerprint = lambda: 0.0
        ready = threading.Event()

        def run_serve():
            with mock.patch.object(socketserver, "TCPServer",
                                   capturing_tcp_server):
                # Подменяем serve_forever, чтобы сигналить готовность и не
                # блокироваться навсегда: serve() сам вызовет его на нашем srv.
                orig_serve_forever = real_tcp_server.serve_forever

                def signalling_serve_forever(self, *a, **k):
                    ready.set()
                    return orig_serve_forever(self, *a, **k)

                with mock.patch.object(real_tcp_server, "serve_forever",
                                       signalling_serve_forever):
                    dashboard_server.serve(port=0)

        thread = threading.Thread(target=run_serve, daemon=True)
        thread.start()
        self.addCleanup(lambda: thread.join(timeout=5))
        try:
            self.assertTrue(ready.wait(timeout=10), "serve() не стартовал")
            # стартовый build_index() в serve() уже отработал
            self.assertGreaterEqual(len(build_calls), 1)
            port = created["port"]

            # Реальная отдача статики продовым Handler.
            req = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/index.html", timeout=5)
            try:
                self.assertEqual(req.status, 200)
                self.assertIn("text/html", req.headers.get("Content-Type", ""))
                self.assertIn("dash", req.read().decode("utf-8"))
            finally:
                req.close()

            # GET /data/index.json проходит через продовый _maybe_rebuild и
            # отдаётся как JSON.
            req = urllib.request.urlopen(
                f"http://127.0.0.1:{port}/data/index.json", timeout=5)
            try:
                self.assertEqual(req.status, 200)
            finally:
                req.close()
        finally:
            srv = created.get("srv")
            if srv is not None:
                srv.shutdown()
        # serve() в finally чистит снимок индекса (owns_index_snapshot).
        thread.join(timeout=5)
        self.assertFalse(thread.is_alive(), "serve() не завершился после shutdown")
        # cleanup_index_snapshot удалил сгенерированный index.json.
        self.assertFalse(index_path.exists(),
                         "serve() не подчистил снимок индекса при выходе")


if __name__ == "__main__":
    unittest.main()

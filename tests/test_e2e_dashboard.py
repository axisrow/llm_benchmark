"""Playwright E2E для дашборда (issue #38, P2).

Поднимает копию `docs/` со сгенерированным `index.json` на эфемерном порту
(обычным http.server, без сети и без data/main.db) и гоняет реальный браузер:
загрузка данных, переключение темы → localStorage, empty-state, битый index.json.

Пропускается целиком, если playwright/браузер недоступны (например, в окружении
без `playwright install chromium`) — чтобы не блокировать основной набор.
"""

import functools
import http.server
import json
import shutil
import socketserver
import tempfile
import threading
import unittest
from pathlib import Path

import db
import index_builder

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DOCS_SRC = PROJECT_ROOT / "docs"

try:
    from playwright.sync_api import sync_playwright
    _HAVE_PLAYWRIGHT = True
except ImportError:
    _HAVE_PLAYWRIGHT = False


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
    """Генерирует валидный docs/data/index.json через настоящий build_index."""
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

        orig_connect = index_builder.connect
        orig_root = index_builder.PROJECT_ROOT
        try:
            index_builder.connect = lambda: orig_connect(db_path)
            index_builder.PROJECT_ROOT = out_root
            index_builder.build_index()  # пишет out_root/docs/data/index.json
        finally:
            index_builder.connect = orig_connect
            index_builder.PROJECT_ROOT = orig_root


class _NoCacheHandler(http.server.SimpleHTTPRequestHandler):
    def end_headers(self):
        self.send_header("Cache-Control", "no-store")
        super().end_headers()

    def log_message(self, *args):
        pass  # тихо


@unittest.skipUnless(_HAVE_PLAYWRIGHT, "playwright не установлен")
class DashboardE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.mkdtemp()
        work = Path(cls._tmp)
        cls._docs = work / "docs"
        shutil.copytree(DOCS_SRC, cls._docs)
        _generate_index_json(work)
        cls._index_path = cls._docs / "data" / "index.json"

        handler = functools.partial(_NoCacheHandler, directory=str(cls._docs))
        cls._httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        cls._port = cls._httpd.server_address[1]
        cls._thread = threading.Thread(target=cls._httpd.serve_forever, daemon=True)
        cls._thread.start()

        try:
            cls._pw = sync_playwright().start()
            cls._browser = cls._pw.chromium.launch()
        except Exception as exc:  # браузер не скачан / нет системных библиотек
            cls._httpd.shutdown()
            raise unittest.SkipTest(f"playwright browser недоступен: {exc}")

    @classmethod
    def tearDownClass(cls):
        if getattr(cls, "_browser", None):
            cls._browser.close()
        if getattr(cls, "_pw", None):
            cls._pw.stop()
        cls._httpd.shutdown()
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        # Каждый тест может переписать index.json — восстанавливаем валидный.
        _generate_index_json(Path(self._tmp))
        self._ctx = self._browser.new_context()
        # CDN-бандл Bootstrap офлайн недоступен — глушим, чтобы не висел load.
        self._ctx.route("**/cdn.jsdelivr.net/**", lambda route: route.abort())
        self._page = self._ctx.new_page()

    def tearDown(self):
        self._ctx.close()

    @property
    def _url(self):
        return f"http://127.0.0.1:{self._port}/index.html"

    def test_dashboard_loads_data(self):
        page = self._page
        page.goto(self._url, wait_until="domcontentloaded")
        # Плейсхолдер "Загружаю данные..." заменяется отрендеренными данными.
        page.wait_for_function(
            "() => !document.getElementById('content').classList.contains('loading')")
        content = page.inner_text("#content")
        self.assertNotIn("Загружаю данные", content)
        self.assertNotIn("Ошибка загрузки данных", content)
        # Данные из index.json отрисованы (проект/рейтинг присутствуют).
        self.assertIn("Рейтинг моделей", content)
        self.assertIn("fast_sort", page.content())

    def test_theme_toggle_persists_to_localstorage(self):
        page = self._page
        page.goto(self._url, wait_until="domcontentloaded")
        page.wait_for_selector('[data-bs-theme-value="dark"]', state="attached")
        # Кнопка темы живёт в закрытом dropdown (Bootstrap офлайн не открывает его),
        # поэтому диспатчим click напрямую — обработчик навешан в app.js.
        page.locator('[data-bs-theme-value="dark"]').dispatch_event("click")
        page.wait_for_function(
            "() => document.documentElement.getAttribute('data-bs-theme') === 'dark'")
        stored = page.evaluate("() => localStorage.getItem('llm-benchmark-theme')")
        self.assertEqual(stored, "dark")
        self.assertEqual(
            page.evaluate(
                "() => document.documentElement.getAttribute('data-app-theme-choice')"),
            "dark")

    def test_empty_index_shows_empty_state(self):
        self._index_path.write_text(json.dumps({
            "generated_at": "2026-01-01T00:00:00", "total": 0, "total_models": 0,
            "dashboard_summary": {}, "model_ranking": [], "projects": [],
        }), encoding="utf-8")
        page = self._page
        page.goto(self._url, wait_until="domcontentloaded")
        page.wait_for_function(
            "() => !document.getElementById('content').classList.contains('loading')")
        self.assertIn("Нет данных для отображения", page.inner_text("#content"))

    def test_malformed_index_shows_error(self):
        self._index_path.write_text("{ это не валидный json", encoding="utf-8")
        page = self._page
        page.goto(self._url, wait_until="domcontentloaded")
        page.wait_for_selector("#content .error")
        self.assertIn("Ошибка загрузки данных", page.inner_text("#content"))


if __name__ == "__main__":
    unittest.main()

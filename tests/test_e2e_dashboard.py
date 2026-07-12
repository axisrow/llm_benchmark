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


def _sample_report(
    *,
    project="fast_sort",
    provider="zai",
    model="glm-5.1",
    started_at="2026-01-01T00:00:00",
    elapsed=12.0,
    prompt_per_1m=0.5,
    completion_per_1m=1.0,
    summary=None,
    run_code=0,
    run_status="готово",
):
    """Один отчёт. Параметры позволяют разводить модели/время/цену/статус
    для тестов рейтинга и сортировки; pricing всегда задан — значит
    load_reports не полезет в сеть за get_pricing (тесты офлайн)."""
    summary = summary or {"ok": 1, "timeout": 0, "error": 0, "rate_limited": 0}
    return {
        "project": project, "provider": provider, "model": model,
        "prompt": "task", "description": "desc", "what_it_tests": ["сортировка"],
        "copies": 1, "started_at": started_at, "run_elapsed": elapsed,
        "summary": summary,
        "pricing": {"prompt_per_1m": prompt_per_1m,
                    "completion_per_1m": completion_per_1m},
        "usage_summary": {"input_tokens": 100, "output_tokens": 10,
                          "total_tokens": 110, "estimated_cost_usd": 0.001,
                          "runs_with_usage": 1, "runs_with_estimated_cost": 1},
        "artifact_summary": {"files": 0},
        "runs": [{"index": 0, "port": 4096, "dir": "/x", "status": run_status,
                  "code": run_code, "elapsed": elapsed, "usage": None}],
    }


def _generate_index_json(out_root: Path, reports=None) -> None:
    """Генерирует валидный docs/data/index.json через настоящий build_index.

    reports — список отчётов (dict). По умолчанию — один _sample_report(),
    чтобы исходные 4 теста работали без изменений. Несколько отчётов с
    разными моделями/проектами прогоняются через тот же build_index, давая
    реалистичный индекс (рейтинг, сравнение моделей)."""
    if reports is None:
        reports = [_sample_report()]
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "main.db"
        conn = db.connect(db_path)
        try:
            db.init_schema(conn)
            with conn:
                for i, rep in enumerate(reports):
                    db.upsert_report(
                        conn, rep, f"data/result/r{i}.json", json.dumps(rep))
        finally:
            conn.close()

        # index_builder мигрирован на db.session() (PR #39) — у него больше нет
        # своего index_builder.connect. Патчим db.connect (его зовёт session)
        # на временную базу; PROJECT_ROOT по-прежнему задаёт каталог вывода.
        orig_connect = db.connect
        orig_root = index_builder.PROJECT_ROOT
        try:
            db.connect = lambda *a, **k: orig_connect(db_path)
            index_builder.PROJECT_ROOT = out_root
            index_builder.build_index()  # пишет out_root/docs/data/index.json
        finally:
            db.connect = orig_connect
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

    # --- issue #38 P2: рейтинг, сортировка проекта, форматирование ---

    def _seed_index(self, reports):
        """Перезаписывает index.json серией отчётов через настоящий build_index."""
        _generate_index_json(Path(self._tmp), reports)

    def test_ranking_expand_collapse(self):
        # > 10 чистых моделей → в рейтинге появляются .ranking-extra-row (idx>=10).
        # Разное avg_elapsed (через elapsed) даёт стабильный порядок рейтинга.
        reports = [
            _sample_report(model=f"model-{i:02d}", elapsed=1.0 + i)
            for i in range(13)
        ]
        self._seed_index(reports)

        page = self._page
        page.goto(self._url, wait_until="domcontentloaded")
        page.wait_for_function(
            "() => !document.getElementById('content').classList.contains('loading')")
        page.wait_for_selector("#rankingToggle")

        # Скрытость выражена классом .d-none (CDN Bootstrap офлайн заглушён, так
        # что фактическую невидимость через CSS не проверить — проверяем класс,
        # которым именно код управляет видимостью).
        def hidden_count():
            return page.locator(".ranking-extra-row.d-none").count()

        self.assertEqual(page.locator(".ranking-extra-row").count(), 3)  # 13-10
        # Изначально все extra-ряды скрыты.
        self.assertEqual(hidden_count(), 3)
        toggle = page.locator("#rankingToggle")
        self.assertEqual(toggle.get_attribute("aria-expanded"), "false")
        self.assertEqual(
            page.inner_text("[data-ranking-toggle-label]"), "Показать все")

        # Разворачиваем (Bootstrap офлайн — диспатчим click напрямую).
        toggle.dispatch_event("click")
        page.wait_for_function(
            "() => document.getElementById('rankingToggle')"
            ".getAttribute('aria-expanded') === 'true'")
        self.assertEqual(hidden_count(), 0)  # d-none снят со всех
        self.assertEqual(
            page.inner_text("[data-ranking-toggle-label]"), "Скрыть")

        # Сворачиваем обратно.
        toggle.dispatch_event("click")
        page.wait_for_function(
            "() => document.getElementById('rankingToggle')"
            ".getAttribute('aria-expanded') === 'false'")
        self.assertEqual(hidden_count(), 3)
        self.assertEqual(
            page.inner_text("[data-ranking-toggle-label]"), "Показать все")

    def _project_url(self, name):
        return f"http://127.0.0.1:{self._port}/project.html?p={name}"

    def _comparison_models(self, page):
        """Порядок строк сравнения по тексту модели (первая ячейка)."""
        return page.locator(
            "#comparisonBody tr td:first-child .model-name").all_inner_texts()

    def test_project_sort_modes(self):
        # Один проект, три модели с разным временем и ценой prompt.
        # elapsed (один run) → avg=min=max=elapsed, поэтому порядок по
        # avg/min/max совпадает; цена задаётся отдельно prompt_per_1m.
        reports = [
            _sample_report(model="m-fast", elapsed=5.0,
                           started_at="2026-01-03T00:00:00", prompt_per_1m=3.0),
            _sample_report(model="m-mid", elapsed=10.0,
                           started_at="2026-01-02T00:00:00", prompt_per_1m=1.0),
            _sample_report(model="m-slow", elapsed=20.0,
                           started_at="2026-01-01T00:00:00", prompt_per_1m=2.0),
        ]
        self._seed_index(reports)

        page = self._page
        page.goto(self._project_url("fast_sort"), wait_until="domcontentloaded")
        page.wait_for_selector("#comparisonBody tr")
        select = page.locator("#sortSelect")

        # avg ↑ — по возрастанию среднего времени.
        select.select_option("avg")
        asc = self._comparison_models(page)
        self.assertEqual(asc, ["m-fast", "m-mid", "m-slow"])

        # avg ↓ — обратный порядок.
        select.select_option("avg-desc")
        desc = self._comparison_models(page)
        self.assertEqual(desc, ["m-slow", "m-mid", "m-fast"])
        self.assertEqual(desc, list(reversed(asc)))

        # min ↑ и max ↑ — при одном run совпадают с avg ↑.
        select.select_option("min")
        self.assertEqual(self._comparison_models(page),
                         ["m-fast", "m-mid", "m-slow"])
        select.select_option("max")
        self.assertEqual(self._comparison_models(page),
                         ["m-fast", "m-mid", "m-slow"])

        # price ↑ — по возрастанию prompt_per_1m (m-mid=1, m-slow=2, m-fast=3).
        select.select_option("price")
        self.assertEqual(self._comparison_models(page),
                         ["m-mid", "m-slow", "m-fast"])

    def test_price_and_status_formatting(self):
        # Две модели: цена ниже порога (0.05 < 0.1 → 4 знака) и выше (1.5 → 2 знака).
        # Статусы: одна с таймаутом, одна с ошибкой — проверяем бейджи.
        reports = [
            _sample_report(
                project="fmt_proj", model="m-cheap-timeout",
                started_at="2026-01-02T00:00:00",
                prompt_per_1m=0.05, completion_per_1m=0.08,
                summary={"ok": 0, "timeout": 1, "error": 0, "rate_limited": 0},
                run_code=1, run_status="таймаут"),
            _sample_report(
                project="fmt_proj", model="m-pricey-error",
                started_at="2026-01-01T00:00:00",
                prompt_per_1m=1.5, completion_per_1m=2.5,
                summary={"ok": 0, "timeout": 0, "error": 1, "rate_limited": 0},
                run_code=2, run_status="ошибка"),
        ]
        self._seed_index(reports)

        page = self._page
        page.goto(self._project_url("fmt_proj"), wait_until="domcontentloaded")
        page.wait_for_selector("#comparisonBody tr")

        def price_cell(model):
            # 4-я колонка (Цена за 1M) строки, чья первая ячейка = model.
            row = page.locator(
                "#comparisonBody tr",
                has=page.locator(f".model-name:text-is('{model}')"))
            return row.locator("td").nth(3).inner_text()

        def status_label(model):
            row = page.locator(
                "#comparisonBody tr",
                has=page.locator(f".model-name:text-is('{model}')"))
            return row.locator("td").nth(2).locator(".badge").inner_text()

        # prompt 0.05 < 0.1 → 4 знака; completion 0.08 < 0.1 → 4 знака.
        self.assertEqual(price_cell("m-cheap-timeout"), "$0.0500 / $0.0800")
        # prompt 1.5 >= 0.1 → 2 знака; completion 2.5 → 2 знака.
        self.assertEqual(price_cell("m-pricey-error"), "$1.50 / $2.50")

        # summaryStatus: error важнее timeout, но у каждой модели свой summary.
        self.assertEqual(status_label("m-cheap-timeout"), "Таймаут")
        self.assertEqual(status_label("m-pricey-error"), "Ошибка")


# --- issue #83: рендер planning-секции + XSS-экранирование ---

# XSS-пayload кладётся в каждое LLM-поле записи вопроса. Если UI вставит его
# как сырой HTML — сработает onerror и поставит window.__planningXss=1, а в DOM
# появится элемент <img>. Оба признака проверяем как провал.
_XSS_PAYLOAD = '<img src=x onerror="window.__planningXss=1">'


def _planning_report(*, project="plan_proj", with_xss=False, started_at="2026-01-01T00:00:00"):
    """Planning-отчёт: planning=on → есть ключи planning/planning_summary и
    runs[].questions. Полная таксономия статусов: replied (с answer),
    fallback (recommended без match → ответ первым option), error (reply_error),
    captured (questions-only: ответ не отправлялся, answer пуст), и пустая копия
    (questions=[] → «Уточняющих вопросов не было» на уровне копии)."""
    def v(text):
        return _XSS_PAYLOAD if with_xss else text

    report = _sample_report(project=project, model="planner-1",
                            started_at=started_at)
    report["planning"] = {"enabled": True, "agent": "bench_planner",
                          "responder": "recommended"}
    # Две копии: у первой — два вопроса (replied+fallback) и error; у второй —
    # один captured (questions-only) и одна пустая (questions=[]).
    report["copies"] = 2
    report["runs"] = [
        {
            "index": 0, "port": 4096, "dir": "/c0", "status": "готово",
            "code": 0, "elapsed": 5.0, "usage": None,
            "questions": [
                {
                    "attempt_idx": 1, "session_id": "s0", "request_id": "q0",
                    "round_idx": 1, "question_idx": 1, "header": v("Заголовок A"),
                    "question": v("Какой формат?"), "multiple": False, "custom": True,
                    "options": [{"label": v("JSON")}, {"label": v("YAML recommended")}],
                    "answer": [v("YAML recommended")], "responder": "recommended",
                    "fallback_used": False, "reply_status": "replied",
                    "reply_error": None, "elapsed": 0.1,
                },
                {
                    "attempt_idx": 2, "session_id": "s0", "request_id": "q1",
                    "round_idx": 1, "question_idx": 1, "header": v("Заголовок B"),
                    "question": v("Fallback?"), "multiple": False, "custom": True,
                    "options": [{"label": v("A")}, {"label": v("B")}],
                    "answer": [v("A")], "responder": "recommended",
                    "fallback_used": True, "reply_status": "replied",
                    "reply_error": None, "elapsed": 0.2,
                },
                {
                    "attempt_idx": 2, "session_id": "s0", "request_id": "q2",
                    "round_idx": 1, "question_idx": 2, "header": v("Заголовок E"),
                    "question": v("Невалидный вопрос"), "multiple": False, "custom": True,
                    "options": [], "answer": [], "responder": "recommended",
                    "fallback_used": False, "reply_status": "error",
                    "reply_error": v("question has no options"), "elapsed": 0.3,
                },
            ],
        },
        {
            "index": 1, "port": 4097, "dir": "/c1", "status": "готово",
            "code": 0, "elapsed": 6.0, "usage": None,
            "questions": [
                {
                    "attempt_idx": 1, "session_id": "s1", "request_id": "q3",
                    "round_idx": 1, "question_idx": 1, "header": v("Только вопрос"),
                    "question": v("Ответ не отправлялся"), "multiple": False, "custom": True,
                    "options": [{"label": v("Opt1")}, {"label": v("Opt2")}],
                    "answer": [], "responder": "recommended",
                    "fallback_used": False, "reply_status": "captured",
                    "reply_error": None, "elapsed": 0.4,
                },
            ],
        },
    ]
    questions = [q for r in report["runs"] for q in r["questions"]]
    report["planning_summary"] = {
        "questions": len(questions),
        "runs_with_questions": sum(1 for r in report["runs"] if r["questions"]),
        "recommended_matches": sum(
            1 for q in questions
            if q.get("responder") == "recommended" and not q.get("fallback_used")),
        "fallbacks_to_first": sum(1 for q in questions if q.get("fallback_used")),
        "reply_errors": sum(1 for q in questions if q.get("reply_status") == "error"),
    }
    return report


@unittest.skipUnless(_HAVE_PLAYWRIGHT, "playwright не установлен")
class PlanningSectionE2ETests(DashboardE2ETests):
    """issue #83: рендер planning-секции. Наследует setUp/tearDown/URL-хелперы
    из DashboardE2ETests (тот же http-сервер docs/)."""

    def _open_project(self, name):
        page = self._page
        page.goto(self._project_url(name), wait_until="domcontentloaded")
        page.wait_for_function(
            "() => !document.getElementById('content').classList.contains('loading')")
        return page

    def test_planning_section_renders_summary_and_questions(self):
        self._seed_index([_planning_report()])
        page = self._open_project("plan_proj")

        # Карточка прогона содержит planning-секцию (по умолчанию свёрнута).
        details = page.locator("[data-planning-section]")
        self.assertEqual(details.count(), 1)

        # В summary всегда видны: число вопросов, responder, fallbacks, reply errors.
        summary_text = details.locator("summary").inner_text()
        self.assertIn("4", summary_text)            # questions
        self.assertIn("recommended", summary_text)  # responder
        self.assertIn("1", summary_text)            # fallbacks_to_first
        self.assertIn("1", summary_text)            # reply_errors

        # Разворачиваем (CDN Bootstrap офлайн — диспатчим toggle напрямую).
        details.evaluate("el => el.open = true")

        # Тексты вопросов и options видны как обычный текст.
        body_text = details.inner_text()
        self.assertIn("Какой формат?", body_text)
        self.assertIn("YAML recommended", body_text)
        self.assertIn("Fallback?", body_text)
        self.assertIn("Невалидный вопрос", body_text)

        # Фактический answer выделен существующим акцентным/badge-классом.
        # JSON не выбран, YAML recommended — выбран.
        highlighted = details.locator(".planning-option.is-selected").count()
        self.assertGreaterEqual(highlighted, 1)

        # Fallback-бейдж присутствует (на fallback-вопросе).
        self.assertGreaterEqual(details.locator(".badge-planning-fallback").count(), 1)
        # reply_status error — санитизированный reply_error как текст + error-бейдж.
        self.assertIn("question has no options", body_text)
        self.assertGreaterEqual(details.locator(".badge-planning-error").count(), 1)

        # questions-only (captured, пустой answer) → «Ответ не отправлялся».
        self.assertIn("Ответ не отправлялся", body_text)

    def test_planning_section_collapsed_by_default(self):
        self._seed_index([_planning_report()])
        page = self._open_project("plan_proj")
        details = page.locator("[data-planning-section]")
        # Свёрнут: open===false, при этом summary виден (контент скрыт).
        self.assertFalse(details.get_attribute("open"))
        self.assertIn("Уточняющие вопросы", details.locator("summary").inner_text())

    def test_planning_empty_state_when_no_questions(self):
        """planning=on, но ни в одной копии вопросов не было → «Уточняющих
        вопросов не было», секция всё равно присутствует."""
        report = _planning_report()
        for run in report["runs"]:
            run["questions"] = []
        report["planning_summary"] = {
            "questions": 0, "runs_with_questions": 0,
            "recommended_matches": 0, "fallbacks_to_first": 0, "reply_errors": 0,
        }
        self._seed_index([report])

        page = self._open_project("plan_proj")
        details = page.locator("[data-planning-section]")
        self.assertEqual(details.count(), 1)
        details.evaluate("el => el.open = true")
        self.assertIn("Уточняющих вопросов не было", details.inner_text())

    def test_coding_report_has_no_planning_section(self):
        """CODING-NO-CHANGE: отчёт без planning-ключей — planning-секции нет,
        карточка отчёта и плитки копий рендерятся как прежде."""
        self._seed_index([_sample_report()])  # coding-репорт без planning
        page = self._open_project("fast_sort")

        self.assertEqual(page.locator("[data-planning-section]").count(), 0)
        # Карточка прогона и плитки копий на месте.
        self.assertGreaterEqual(page.locator("article.premium-card").count(), 1)
        self.assertGreaterEqual(page.locator(".run-tile").count(), 1)

    def test_xss_payload_is_text_not_executed(self):
        """XSS E2E: payload во всех LLM-полях. Проверки: (1) виден как текст;
        (2) внутри planning-секции нет элемента <img> из payload; (3) onerror
        не выполнился — window.__planningXss остаётся undefined."""
        self._seed_index([_planning_report(with_xss=True)])
        page = self._open_project("plan_proj")

        details = page.locator("[data-planning-section]")
        details.evaluate("el => el.open = true")

        body_text = details.inner_text()
        # (1) payload присутствует как текст (не разрезан на HTML-тег).
        self.assertIn(_XSS_PAYLOAD, body_text)
        # (2) внутри секции нет созданного из payload <img>.
        self.assertEqual(details.locator("img").count(), 0)
        # (3) onerror не выполнился.
        self.assertIsNone(
            page.evaluate("() => window.__planningXss === undefined"
                          " ? null : window.__planningXss"))


if __name__ == "__main__":
    unittest.main()

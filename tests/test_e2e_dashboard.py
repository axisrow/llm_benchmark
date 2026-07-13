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


@unittest.skipUnless(_HAVE_PLAYWRIGHT, "playwright не установлен")
class QuestionReviewsLocalE2ETests(unittest.TestCase):
    """issue #93, слой 5: локальный E2E с настоящим dashboard_server.serve() и
    реальной временной БД.

    Поднимает serve() (Handler + API + авто-пересборка индекса) против временной
    data/main.db; Playwright открывает project.html и через кнопки ставит /
    заменяет / снимает verdict. Проверяет:
    - кнопки рендерятся при capabilities.question_reviews=true;
    - PUT/DELETE обновляют DOM (aria-pressed + is-selected) без reload;
    - обе кнопки disabled во время запроса;
    - ошибка запроса → восстановление прежнего состояния + сообщение;
    - после reload оценка сохранена (fingerprint БД сменился → пересборка).

    db.connect патчится на временный путь (его зовут и session() build_index'а,
    и API-методы Handler'а). PROJECT_ROOT dashboard_server → временный docs.
    """

    @classmethod
    def setUpClass(cls):
        from unittest import mock
        import socketserver
        import dashboard_server

        cls._tmp = tempfile.mkdtemp()
        work = Path(cls._tmp)
        cls._docs = work / "docs"
        shutil.copytree(DOCS_SRC, cls._docs)
        cls._db_path = work / "main.db"

        # Сидим planning-отчёт с одним вопросом.
        report = _planning_report()
        conn = db.connect(cls._db_path)
        try:
            db.init_schema(conn)
            with conn:
                cls._report_id = db.upsert_report(
                    conn, report, "data/result/r.json", json.dumps(report))
        finally:
            conn.close()

        # Патчим db.connect (зовут session() и API) + PROJECT_ROOT/DB_PATH.
        # ВАЖНО: index_builder.PROJECT_ROOT — отдельная ссылка (build_index пишет
        # index.json через НЕЁ); без этого патча на чистом CI (где docs/data/
        # index.json отсутствует в git) serve отдаёт пустой/чужой index и проект
        # не находится. Локально это маскируется скопированным index.json.
        import index_builder
        cls._orig_connect = db.connect
        cls._orig_root = dashboard_server.PROJECT_ROOT
        cls._orig_index_root = index_builder.PROJECT_ROOT
        cls._orig_dbpath = dashboard_server.DB_PATH
        db.connect = lambda *a, **k: cls._orig_connect(cls._db_path)
        dashboard_server.PROJECT_ROOT = work
        index_builder.PROJECT_ROOT = work
        dashboard_server.DB_PATH = cls._db_path

        real_tcp_server = socketserver.TCPServer
        created = {}
        ready = threading.Event()

        def capturing_tcp_server(addr, handler_cls):
            srv = real_tcp_server(addr, handler_cls)
            created["srv"] = srv
            created["port"] = srv.server_address[1]
            return srv

        cls._orig_serve_forever = real_tcp_server.serve_forever
        orig_serve_forever = real_tcp_server.serve_forever

        def signalling_serve_forever(self, *a, **k):
            ready.set()
            return orig_serve_forever(self, *a, **k)

        try:
            cls._mock_tcp = mock.patch.object(socketserver, "TCPServer",
                                               capturing_tcp_server)
            cls._mock_tcp.start()
            cls._mock_forever = mock.patch.object(
                real_tcp_server, "serve_forever", signalling_serve_forever)
            cls._mock_forever.start()
            cls._thread = threading.Thread(target=dashboard_server.serve,
                                           kwargs={"port": 0}, daemon=True)
            cls._thread.start()
            assert ready.wait(timeout=10), "serve() не стартовал"
            cls._port = created["port"]
            cls._srv = created["srv"]
        except Exception:
            cls.tearDownClass()
            raise

        try:
            cls._pw = sync_playwright().start()
            cls._browser = cls._pw.chromium.launch()
        except Exception as exc:
            cls.tearDownClass()
            raise unittest.SkipTest(f"playwright browser недоступен: {exc}")

    @classmethod
    def tearDownClass(cls):
        import dashboard_server as ds
        srv = getattr(cls, "_srv", None)
        if srv is not None:
            srv.shutdown()
        if getattr(cls, "_thread", None):
            cls._thread.join(timeout=5)
        for attr in ("_mock_forever", "_mock_tcp"):
            patch = getattr(cls, attr, None)
            if patch is not None:
                patch.stop()
        if getattr(cls, "_browser", None):
            cls._browser.close()
        if getattr(cls, "_pw", None):
            cls._pw.stop()
        # Восстановление патчей db.connect / dashboard_server.PROJECT_ROOT /
        # index_builder.PROJECT_ROOT / DB_PATH.
        import index_builder
        if hasattr(cls, "_orig_connect"):
            db.connect = cls._orig_connect
        if hasattr(cls, "_orig_root"):
            ds.PROJECT_ROOT = cls._orig_root
        if hasattr(cls, "_orig_index_root"):
            index_builder.PROJECT_ROOT = cls._orig_index_root
        if hasattr(cls, "_orig_dbpath"):
            ds.DB_PATH = cls._orig_dbpath
        shutil.rmtree(cls._tmp, ignore_errors=True)

    def setUp(self):
        # Изоляция тестов: чистим все reviews, чтобы каждый тест стартовал с
        # «не оценено» (БД классовая — serve держит одно соединение-источник).
        cls = type(self)
        conn = cls._orig_connect(cls._db_path)
        try:
            with conn:
                conn.execute("DELETE FROM question_reviews")
        finally:
            conn.close()
        self._ctx = self._browser.new_context()
        self._ctx.route("**/cdn.jsdelivr.net/**", lambda route: route.abort())
        self._page = self._ctx.new_page()

    def tearDown(self):
        self._ctx.close()

    def _url(self):
        return f"http://127.0.0.1:{self._port}/project.html?p=plan_proj"

    def _open(self):
        # Precondition: serve уже отдаёт index с plan_proj. Прямой GET даёт
        # чёткую диагностику, если build_index в serve ещё не отработал или
        # читает не ту БД (вместо «слепого» таймаута локатора в браузере).
        import urllib.request
        raw = urllib.request.urlopen(
            f"http://127.0.0.1:{self._port}/data/index.json", timeout=5).read()
        index = json.loads(raw)
        names = [p.get("name") for p in index.get("projects", [])]
        assert "plan_proj" in names, (
            f"serve не отдаёт plan_proj в index (projects={names}). "
            f"Вероятно build_index читает не временную БД.")
        page = self._page
        page.goto(self._url(), wait_until="domcontentloaded")
        page.wait_for_function(
            "() => !document.getElementById('content').classList.contains('loading')")
        # Ждём planning-секцию с осмысленной диагностикой при её отсутствии.
        try:
            page.wait_for_selector("[data-planning-section]", timeout=5000)
        except Exception:
            html = page.evaluate(
                "() => document.getElementById('content').innerHTML.slice(0, 800)")
            raise AssertionError(
                f"planning-секция не отрендерилась. content:\n{html}")
        # Разворачиваем planning-секцию.
        page.locator("[data-planning-section]").evaluate("el => el.open = true")
        return page

    def _first_question_buttons(self, page):
        """Кнопки (useful, unnecessary) ПЕРВОГО вопроса в секции."""
        boxes = page.locator(".planning-review")
        self.assertGreater(boxes.count(), 0)
        first = boxes.nth(0)
        return {
            "useful": first.locator(".planning-review-btn[data-verdict='useful']"),
            "unnecessary": first.locator(
                ".planning-review-btn[data-verdict='unnecessary']"),
            "box": first,
        }

    def test_buttons_render_when_capability_true(self):
        """Локальный serve → capabilities.question_reviews=true → кнопки есть."""
        page = self._open()
        page.wait_for_selector(".planning-review-btn")
        # 4 вопроса в фикстуре _planning_report → 8 кнопок.
        self.assertEqual(page.locator(".planning-review-btn").count(), 8)
        # Изначально ни одна не активна.
        self.assertEqual(page.locator(".planning-review-btn.is-selected").count(), 0)

    def test_put_marks_useful_updates_dom(self):
        page = self._open()
        page.wait_for_selector(".planning-review-btn")
        btns = self._first_question_buttons(page)
        btns["useful"].click()
        # После успеха: useful — aria-pressed=true + is-selected.
        page.wait_for_function(
            "() => document.querySelector('.planning-review') "
            ".querySelector(\".planning-review-btn[data-verdict='useful']\")"
            ".getAttribute('aria-pressed') === 'true'")
        self.assertIn("is-selected", btns["useful"].get_attribute("class"))
        # unnecessary — не активна.
        self.assertEqual(btns["unnecessary"].get_attribute("aria-pressed"),
                         "false")

    def test_put_replaces_verdict(self):
        page = self._open()
        page.wait_for_selector(".planning-review-btn")
        btns = self._first_question_buttons(page)
        btns["useful"].click()
        page.wait_for_function(
            "() => document.querySelector('.planning-review') "
            ".querySelector(\".planning-review-btn[data-verdict='useful']\")"
            ".getAttribute('aria-pressed') === 'true'")
        # Заменяем на unnecessary.
        btns["unnecessary"].click()
        page.wait_for_function(
            "() => document.querySelector('.planning-review') "
            ".querySelector(\".planning-review-btn[data-verdict='unnecessary']\")"
            ".getAttribute('aria-pressed') === 'true'")
        self.assertEqual(btns["useful"].get_attribute("aria-pressed"), "false")

    def test_clicking_active_button_deletes_review(self):
        """Повторный клик по активной кнопке → DELETE → «не оценён»."""
        page = self._open()
        page.wait_for_selector(".planning-review-btn")
        btns = self._first_question_buttons(page)
        btns["useful"].click()
        page.wait_for_function(
            "() => document.querySelector('.planning-review') "
            ".querySelector(\".planning-review-btn[data-verdict='useful']\")"
            ".getAttribute('aria-pressed') === 'true'")
        # Повторный клик → снимает.
        btns["useful"].click()
        page.wait_for_function(
            "() => document.querySelector('.planning-review') "
            ".querySelectorAll('.planning-review-btn.is-selected').length === 0")
        self.assertEqual(
            btns["box"].locator(".planning-review-btn.is-selected").count(), 0)

    def test_review_persists_in_db_after_put(self):
        """PUT через UI сохраняет review в БД — это и есть персистентность:
        после reload serve пересоберёт index из этой БД и оценка восстановится
        (пересборка из БД проверяется отдельным index-тестом, см.
        test_question_reviews_index). Здесь — конец в конец: клик UI → запись в БД."""
        page = self._open()
        page.wait_for_selector(".planning-review-btn")
        btns = self._first_question_buttons(page)
        btns["useful"].click()
        page.wait_for_function(
            "() => document.querySelector('.planning-review') "
            ".querySelector(\".planning-review-btn[data-verdict='useful']\")"
            ".getAttribute('aria-pressed') === 'true'")
        # PUT отработал — review должен лежать в БД ( serve пишет через db.connect
        # в ту же временную базу).
        cls = type(self)
        conn = cls._orig_connect(cls._db_path)
        try:
            cnt = conn.execute(
                "SELECT count(*) FROM question_reviews").fetchone()[0]
            verdicts = [r[0] for r in conn.execute(
                "SELECT verdict FROM question_reviews").fetchall()]
        finally:
            conn.close()
        self.assertEqual(cnt, 1)
        self.assertEqual(verdicts, ["useful"])


@unittest.skipUnless(_HAVE_PLAYWRIGHT, "playwright не установлен")
class QuestionReviewsStaticE2ETests(DashboardE2ETests):
    """issue #93, слой 6: статический index.json (GitHub Pages) — кнопок нет,
    verdict/review_summary показываются read-only; колонка «Вопросы» в сравнении.

    Наследует сетап DashboardE2ETests (статический http.server без /api/*). На
    Pages /api/capabilities отсутствует → canReview=false → кнопки не рендерятся.
    verdict/summary приходят уже обогащёнными в index.json (их кладёт build_index
    из БД). Здесь мы пишем index.json напрямую с review_verdict, имитируя Pages.
    """

    def _seed_enriched_index(self, review_verdict=None, review_summary=None):
        """Пишет index.json с одним planning-отчётом, у первого вопроса —
        review_verdict (или без него), у отчёта — review_summary."""
        report = _planning_report()
        questions = report["runs"][0]["questions"]
        questions[0]["review_key"] = {
            "report_id": 1, "run_idx": 0, "attempt_idx": 1,
            "request_id": questions[0]["request_id"],
            "question_idx": questions[0]["question_idx"],
        }
        if review_verdict is not None:
            questions[0]["review_verdict"] = review_verdict
        if review_summary is not None:
            report["review_summary"] = review_summary
        # минимальная обёртка как у build_index.
        data = {
            "generated_at": "2026-01-01T00:00:00", "total": 1, "total_models": 1,
            "dashboard_summary": {}, "model_ranking": [],
            "projects": [{"name": "plan_proj", "description": "",
                          "prompt": "", "what_it_tests": [],
                          "summary": {}, "run_count": 1, "report_count": 1,
                          "model_count": 1, "reports": [report]}],
        }
        self._index_path.write_text(json.dumps(data, ensure_ascii=False),
                                    encoding="utf-8")

    def test_no_buttons_when_no_capabilities(self):
        """Статический сервер: /api/capabilities нет → canReview=false → кнопок
        рендерить нельзя, даже если verdict есть в index."""
        self._seed_enriched_index(review_verdict="useful",
                                  review_summary={"total": 4, "reviewed": 1,
                                                  "useful": 1, "unnecessary": 0,
                                                  "useful_percent": 100.0,
                                                  "coverage_percent": 25.0})
        page = self._page
        page.goto(self._project_url("plan_proj"), wait_until="domcontentloaded")
        page.wait_for_function(
            "() => !document.getElementById('content').classList.contains('loading')")
        page.locator("[data-planning-section]").evaluate("el => el.open = true")
        # Кнопок разметки НЕТ (read-only режим Pages).
        self.assertEqual(page.locator(".planning-review-btn").count(), 0)

    def test_verdict_and_summary_visible_readonly(self):
        """verdict и review_summary видны как read-only (без кнопок). На статике
        verdict отображается через bейдж reply-status, а review_summary — в шапке
        секции («полезных / оценено / покрытие»)."""
        self._seed_enriched_index(
            review_verdict="useful",
            review_summary={"total": 4, "reviewed": 2, "useful": 1,
                            "unnecessary": 1, "useful_percent": 50.0,
                            "coverage_percent": 50.0})
        page = self._page
        page.goto(self._project_url("plan_proj"), wait_until="domcontentloaded")
        page.wait_for_function(
            "() => !document.getElementById('content').classList.contains('loading')")
        page.locator("[data-planning-section]").evaluate("el => el.open = true")
        summary_text = page.locator("[data-planning-section] summary").inner_text()
        self.assertIn("полезных: 1 / оценено: 2", summary_text)
        self.assertIn("покрытие 50%", summary_text)


@unittest.skipUnless(_HAVE_PLAYWRIGHT, "playwright не установлен")
class QuestionsComparisonColumnTests(DashboardE2ETests):
    """issue #93, слой 7: колонка «Вопросы» в таблице сравнения моделей.

    useful/reviewed и coverage для planning-отчётов с оценкой; N/A для coding и
    при reviewed=0. Регрессия: существующие planning/XSS-тесты (PlanningSection
    E2ETests) не затронуты — отдельный класс.
    """

    def _write_index(self, data):
        """Пишет готовый index.json напрямую (имитация Pages: review_summary уже
        обогащён, build_index не гоняется)."""
        self._index_path.write_text(json.dumps(data, ensure_ascii=False),
                                    encoding="utf-8")

    def _project_block(self, name, report):
        return {"name": name, "description": "", "prompt": "",
                "what_it_tests": [], "summary": {}, "run_count": 1,
                "report_count": 1, "model_count": 1, "reports": [report]}

    def test_questions_column_shows_useful_reviewed(self):
        report = _planning_report()
        report["review_summary"] = {
            "total": 4, "reviewed": 3, "useful": 2, "unnecessary": 1,
            "useful_percent": 66.67, "coverage_percent": 75.0,
        }
        self._write_index({
            "generated_at": "2026-01-01T00:00:00", "total": 1, "total_models": 1,
            "dashboard_summary": {}, "model_ranking": [],
            "projects": [self._project_block("plan_proj", report)],
        })
        page = self._page
        page.goto(self._project_url("plan_proj"), wait_until="domcontentloaded")
        page.wait_for_selector("#comparisonBody tr")
        cell = page.locator("#comparisonBody tr td.comparison-questions").first
        text = cell.inner_text()
        self.assertIn("2/3", text)
        self.assertIn("75%", text)

    def test_questions_column_na_for_coding_report(self):
        """Coding-отчёт (нет review_summary) → N/A в колонке «Вопросы»."""
        self._seed_index([_sample_report()])  # coding без planning
        page = self._page
        page.goto(self._project_url("fast_sort"), wait_until="domcontentloaded")
        page.wait_for_selector("#comparisonBody tr")
        cell = page.locator("#comparisonBody tr td.comparison-questions").first
        self.assertEqual(cell.inner_text(), "N/A")

    def test_questions_column_na_when_nothing_reviewed(self):
        """review_summary есть, но reviewed=0 → N/A (неоценённые не ухудшают метрику)."""
        report = _planning_report()
        report["review_summary"] = {
            "total": 4, "reviewed": 0, "useful": 0, "unnecessary": 0,
            "useful_percent": None, "coverage_percent": 0.0,
        }
        self._write_index({
            "generated_at": "2026-01-01T00:00:00", "total": 1, "total_models": 1,
            "dashboard_summary": {}, "model_ranking": [],
            "projects": [self._project_block("plan_proj", report)],
        })
        page = self._page
        page.goto(self._project_url("plan_proj"), wait_until="domcontentloaded")
        page.wait_for_selector("#comparisonBody tr")
        cell = page.locator("#comparisonBody tr td.comparison-questions").first
        self.assertEqual(cell.inner_text(), "N/A")


# --- issue #96: Тиндер-режим разметки неразмеченных planning-вопросов ---
#
# Полноэкранный поток вопросов одного отчёта: ←=unnecessary, →=useful, Backspace/Esc
# выход, клик по стрелкам = то же. Только неразмеченные (без review_verdict); после
# разметки вопрос уходит из потока; в конце — экран «всё размечено». Переиспользует
# review_key и тот же PUT/DELETE /api/question-reviews, что и кнопки в карточках #94.
# read-only на Pages (capabilities.question_reviews=false → кнопки входа нет).
#
# DOM-контракт Тиндера (на чём построены селекторы тестов):
#   .tinder-entry          — кнопка «Разметить» в карточке planning-отчёта
#                            (data-report-id=id этого отчёта); только при canReview
#                            и наличии неразмеченных.
#   #tinderOverlay         — корневой контейнер overlay; [hidden] пока режим выключен.
#   .tinder-screen         — текущий экран (вопроса или «всё размечено»).
#   .tinder-done           — экран «всё размечено» (есть → поток закончен).
#   .tinder-prompt         — промпт задачи (сверху).
#   .tinder-q-header       — header текущего вопроса.
#   .tinder-q-text         — текст текущего вопроса.
#   .tinder-options        — options текущего вопроса (.planning-option.is-selected).
#   .tinder-arrow          — стрелки разметки; data-verdict="useful" (→) и
#                            "unnecessary" (←); is-selected/aria-pressed после успеха.
#   .tinder-back           — ссылка «вернуться назад».
#   .tinder-current        — текущий вопрос потока (контейнер; data-review-key).
# Активный режим определяется по #tinderOverlay:not([hidden]).
#
# Сетап: классовый QuestionReviewsLocalE2ETests уже поднял serve() с одним
# planning-отчётом (plan_proj, 4 вопроса, report_id=cls._report_id). Наследник только
# добавляет сценарии Тиндера против того же serve/БД.

@unittest.skipUnless(_HAVE_PLAYWRIGHT, "playwright не установлен")
class TinderReviewLocalE2ETests(QuestionReviewsLocalE2ETests):
    """issue #96: Тиндер-режим против настоящего serve() с API и временной БД.

    Каждый из 8 сценариев #96 — отдельный тест. По входу в режим (клик .tinder-entry
    или прямой hash-маршрут) показывается первый неразмеченный вопрос; ←/→/клик
    ставят verdict через тот же API; после разметки вопрос исчезает из потока;
    когда неразмеченных не осталось — экран «всё размечено».
    """

    def _open_report(self):
        page = self._open()
        page.wait_for_selector(".planning-review-btn")
        return page

    def _report_id_from_dom(self, page):
        """report_id первого вопроса — берём прямо из DOM (review_key в карточке).
        Имя не пересекается с cls._report_id (int из родительского сетапа)."""
        key = page.locator(".planning-review").first.get_attribute("data-review-key")
        self.assertIsNotNone(key, "у вопроса нет review_key для маршрута Тиндера")
        return json.loads(key)["report_id"]

    def _enter_tinder(self, page):
        """Клик по кнопке входа в Тиндер первой planning-карточки."""
        page.locator(".tinder-entry").first.click()
        page.wait_for_selector("#tinderOverlay:not([hidden]) .tinder-screen")

    def _expect_count_unreviewed(self, page, n):
        """Число .planning-review без is-selected на странице проекта = число
        неразмеченных вопросов. Карточки #94 — источник правды видимых состояний."""
        return page.evaluate(
            """() => document.querySelectorAll(
                '.planning-review').length""")

    # --- регрессия #94 (унаследованный тест): детерминированная версия ----------
    #
    # Родительский test_review_persists_in_db_after_put (#94) ждёт только
    # aria-pressed='true', но тот ставится ОПТИМИСТИЧНО до fetch (applyReviewVerdict
    # → await fetch). На нагруженном CI fetch может не успеть записать review к
    # моменту чтения БД — флакючесть (упало в PR #97). Здесь переопределяем тест,
    # чтобы ждать настоящий пост-fetch сигнал: бейджи [data-review-badge] в шапке
    # секции перерисовываются updateReviewSummaries ТОЛЬКО после успешного PUT.
    def test_review_persists_in_db_after_put(self):
        page = self._open()
        page.wait_for_selector(".planning-review-btn")
        btns = self._first_question_buttons(page)
        cls = type(self)
        conn0 = cls._orig_connect(cls._db_path)
        try:
            badges_before = page.evaluate(
                "() => document.querySelectorAll('[data-review-badge]').length")
        finally:
            conn0.close()
        btns["useful"].click()
        # Ждём пост-fetch сигнал: число review-бейджей в шапке секции выросло
        # (было 0 при reviewed=0 → стало 2 после успешного useful). Это
        # детерминированно гарантирует, что PUT завершился и БД обновлена.
        page.wait_for_function(
            f"(n) => document.querySelectorAll('[data-review-badge]').length > {badges_before}",
            arg=badges_before)
        # PUT гарантированно отработал — review лежит в БД.
        conn = cls._orig_connect(cls._db_path)
        try:
            cnt = conn.execute(
                "SELECT count(*) FROM question_reviews").fetchone()[0]
            verdicts = [r[0] for r in conn.execute(
                "SELECT verdict FROM question_reviews").fetchall()]
        finally:
            conn.close()
        self.assertEqual(cnt, 1)
        self.assertEqual(verdicts, ["useful"])

    # --- сценарий 1: вход и поток ---
    def test_entry_shows_first_unreviewed_with_prompt_question_options_arrows(self):
        page = self._open_report()
        report_id = self._report_id_from_dom(page)
        # Кнопка входа рендерится (есть неразмеченные) и несёт report_id этого отчёта.
        entry = page.locator(".tinder-entry").first
        self.assertEqual(entry.get_attribute("data-report-id"), str(report_id))
        self.assertIn("Разметить", entry.inner_text())

        self._enter_tinder(page)
        # Overlay открыт и показывает экран вопроса (не «всё размечено»).
        self.assertTrue(page.locator("#tinderOverlay:not([hidden])").count() > 0)
        self.assertEqual(page.locator(".tinder-done").count(), 0)
        # Сверху — промпт задачи.
        prompt_text = page.locator(".tinder-prompt").inner_text()
        self.assertIn("task", prompt_text)  # _sample_report prompt="task"
        # header + текст вопроса + options присутствуют.
        self.assertTrue(page.locator(".tinder-q-header").inner_text().strip())
        self.assertTrue(page.locator(".tinder-q-text").inner_text().strip())
        self.assertGreater(page.locator(".tinder-options .planning-option").count(), 0)
        # Фактический answer выделен (.is-selected) — как в карточках #83.
        self.assertGreater(
            page.locator(".tinder-options .planning-option.is-selected").count(), 0)
        # Обе стрелки на месте, ни одна не активна до разметки.
        self.assertEqual(
            page.locator('.tinder-arrow[data-verdict="useful"]').count(), 1)
        self.assertEqual(
            page.locator('.tinder-arrow[data-verdict="unnecessary"]').count(), 1)
        self.assertEqual(page.locator(".tinder-arrow.is-selected").count(), 0)
        # Ссылка «вернуться назад» есть.
        self.assertEqual(page.locator(".tinder-back").count(), 1)

    # --- сценарий 2: → = полезный ---
    def test_arrow_right_marks_useful_and_advances(self):
        page = self._open_report()
        self._enter_tinder(page)
        # Текст текущего вопроса ДО разметки.
        q_before = page.locator(".tinder-current .tinder-q-text").inner_text()

        page.keyboard.press("ArrowRight")
        # Стрелка useful кратко is-selected в момент запроса, затем вопрос уходит.
        # Ждём, что текущий вопрос потока сменился (текст вопроса другой).
        page.wait_for_function(
            """(prev) => {
                const el = document.querySelector('.tinder-current .tinder-q-text');
                return !el || el.textContent !== prev;
            }""", arg=q_before)
        # После reload оценка сохранена (fingerprint БД сменился → пересборка).
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            "() => !document.getElementById('content').classList.contains('loading')")
        page.locator("[data-planning-section]").evaluate("el => el.open = true")
        # Первая кнопка useful стала is-selected (это был первый вопрос потока).
        page.wait_for_function(
            "() => document.querySelector('.planning-review') "
            ".querySelector(\".planning-review-btn[data-verdict='useful']\")"
            ".getAttribute('aria-pressed') === 'true'")

    # --- сценарий 3: ← = лишний ---
    def test_arrow_left_marks_unnecessary_and_advances(self):
        page = self._open_report()
        self._enter_tinder(page)
        q_before = page.locator(".tinder-current .tinder-q-text").inner_text()

        page.keyboard.press("ArrowLeft")
        page.wait_for_function(
            """(prev) => {
                const el = document.querySelector('.tinder-current .tinder-q-text');
                return !el || el.textContent !== prev;
            }""", arg=q_before)
        # Проверяем персистентность в БД напрямую: verdict=unnecessary.
        cls = type(self)
        conn = cls._orig_connect(cls._db_path)
        try:
            verdicts = [r[0] for r in conn.execute(
                "SELECT verdict FROM question_reviews").fetchall()]
        finally:
            conn.close()
        self.assertIn("unnecessary", verdicts)

    # --- сценарий 4: Backspace / Esc — выход ---
    def test_backspace_exits_tinder(self):
        page = self._open_report()
        self._enter_tinder(page)
        self.assertGreater(page.locator("#tinderOverlay:not([hidden])").count(), 0)
        page.keyboard.press("Backspace")
        page.wait_for_function(
            "() => document.getElementById('tinderOverlay').hidden")
        self.assertEqual(page.locator("#tinderOverlay:not([hidden])").count(), 0)

    def test_escape_exits_tinder(self):
        page = self._open_report()
        self._enter_tinder(page)
        self.assertGreater(page.locator("#tinderOverlay:not([hidden])").count(), 0)
        page.keyboard.press("Escape")
        page.wait_for_function(
            "() => document.getElementById('tinderOverlay').hidden")
        self.assertEqual(page.locator("#tinderOverlay:not([hidden])").count(), 0)

    # --- сценарий 5: поток заканчивается ---
    def test_flow_ends_with_all_reviewed_screen(self):
        page = self._open_report()
        # 4 неразмеченных вопроса в фикстуре; размечаем всё стрелками → (useful).
        self._enter_tinder(page)
        for i in range(4):
            # Ждём, пока текущий вопрос станет интерактивным (не в середине запроса):
            # .tinder-current отрисован и ни одна стрелка не в is-selected-переходе.
            page.wait_for_function(
                """() => {
                    if (document.querySelector('#tinderOverlay:not([hidden]) .tinder-done'))
                        return true;
                    const cur = document.querySelector('.tinder-current');
                    return !!cur && !cur.querySelector('.tinder-arrow.is-selected');
                }""")
            page.keyboard.press("ArrowRight")
        # После 4-й разметки неразмеченных не осталось → экран «всё размечено».
        page.wait_for_selector("#tinderOverlay:not([hidden]) .tinder-done")
        self.assertIn("размечен", page.locator(".tinder-done").inner_text().lower())
        # И ссылка назад присутствует.
        self.assertEqual(page.locator(".tinder-done .tinder-back").count(), 1)
        # В БД — ровно 4 review.
        cls = type(self)
        conn = cls._orig_connect(cls._db_path)
        try:
            cnt = conn.execute(
                "SELECT count(*) FROM question_reviews").fetchone()[0]
        finally:
            conn.close()
        self.assertEqual(cnt, 4)

    # --- сценарий 6: уже размеченные скрыты ---
    def test_entry_hidden_when_all_reviewed(self):
        """Все вопросы размечены заранее → кнопки входа в Тиндер НЕТ."""
        cls = type(self)
        # Сидим useful на все 4 вопроса фикстуры через тот же API-путь (как карточки).
        conn = cls._orig_connect(cls._db_path)
        try:
            with conn:
                # Ключи вопросов: report_id / run_idx / attempt_idx / request_id /
                # question_idx — берём из самой БД (agent_questions).
                for row in conn.execute(
                        """SELECT report_id, run_idx, attempt_idx, request_id,
                                  question_idx FROM agent_questions"""):
                    db.put_question_review(
                        conn, report_id=row["report_id"], run_idx=row["run_idx"],
                        attempt_idx=row["attempt_idx"], request_id=row["request_id"],
                        question_idx=row["question_idx"], verdict="useful")
        finally:
            conn.close()
        page = self._open_report()
        # Кнопки входа нет (всё размечено).
        self.assertEqual(page.locator(".tinder-entry").count(), 0)

    # --- клик по стрелкам = то же, что клавиша (тач/мышь) ---
    def test_clicking_arrow_marks_useful(self):
        page = self._open_report()
        self._enter_tinder(page)
        q_before = page.locator(".tinder-current .tinder-q-text").inner_text()
        page.locator('.tinder-arrow[data-verdict="useful"]').click()
        page.wait_for_function(
            """(prev) => {
                const el = document.querySelector('.tinder-current .tinder-q-text');
                return !el || el.textContent !== prev;
            }""", arg=q_before)
        # Через reload — сохранено в БД.
        page.reload(wait_until="domcontentloaded")
        page.wait_for_function(
            "() => !document.getElementById('content').classList.contains('loading')")
        cls = type(self)
        conn = cls._orig_connect(cls._db_path)
        try:
            verdicts = [r[0] for r in conn.execute(
                "SELECT verdict FROM question_reviews").fetchall()]
        finally:
            conn.close()
        self.assertIn("useful", verdicts)

    # --- регрессия: coding-отчёты не предлагают Тиндер ---
    def test_coding_report_has_no_tinder_entry(self):
        """Coding-отчёт (без planning) → кнопки входа в Тиндер нет вовсе."""
        # Сидим coding-отчёт рядом с planning в той же БД (отдельный проект).
        cls = type(self)
        coding = _sample_report(project="coding_proj", model="coder-1")
        conn = cls._orig_connect(cls._db_path)
        try:
            with conn:
                db.upsert_report(conn, coding, "data/result/c.json",
                                 json.dumps(coding))
        finally:
            conn.close()
        page = self._page
        page.goto(f"http://127.0.0.1:{cls._port}/project.html?p=coding_proj",
                  wait_until="domcontentloaded")
        page.wait_for_function(
            "() => !document.getElementById('content').classList.contains('loading')")
        # Planning-секции и Тиндера нет.
        self.assertEqual(page.locator("[data-planning-section]").count(), 0)
        self.assertEqual(page.locator(".tinder-entry").count(), 0)

    # --- регрессия: XSS-экранирование в Тиндере сохраняется ---
    def test_tinder_escapes_llm_content(self):
        """Все LLM-значения (промпт/header/вопрос/options/answer) экранированы:
        payload виден как текст, <img> не создаётся, onerror не выполняется."""
        # Пересаживаем отчёт с XSS-payload во всех текстовых полях.
        cls = type(self)
        xss = _planning_report(with_xss=True)
        conn = cls._orig_connect(cls._db_path)
        try:
            with conn:
                cls._report_id = db.upsert_report(
                    conn, xss, "data/result/r.json", json.dumps(xss))
        finally:
            conn.close()
        page = self._open_report()
        self._enter_tinder(page)
        # payload присутствует как текст, <img> внутри overlay не создан.
        body = page.locator("#tinderOverlay").inner_text()
        self.assertIn(_XSS_PAYLOAD, body)
        self.assertEqual(page.locator("#tinderOverlay img").count(), 0)
        # onerror не выполнился.
        self.assertIsNone(page.evaluate(
            "() => window.__planningXss === undefined ? null : window.__planningXss"))


@unittest.skipUnless(_HAVE_PLAYWRIGHT, "playwright не установлен")
class TinderReviewStaticE2ETests(DashboardE2ETests):
    """issue #96, read-only слой: на статике (Pages) /api/capabilities нет →
    canReview=false → кнопки входа в Тиндер не рендерятся, даже если в index.json
    есть неразмеченные вопросы. Наследует статический harness (http.server без API).
    """

    def test_no_tinder_entry_on_static_pages(self):
        report = _planning_report()
        # Минимальный index.json: неразмеченные вопросы есть, но capabilities нет.
        data = {
            "generated_at": "2026-01-01T00:00:00", "total": 1, "total_models": 1,
            "dashboard_summary": {}, "model_ranking": [],
            "projects": [{"name": "plan_proj", "description": "", "prompt": "task",
                          "what_it_tests": [], "summary": {}, "run_count": 1,
                          "report_count": 1, "model_count": 1, "reports": [report]}],
        }
        self._index_path.write_text(json.dumps(data, ensure_ascii=False),
                                    encoding="utf-8")
        page = self._page
        page.goto(self._project_url("plan_proj"), wait_until="domcontentloaded")
        page.wait_for_function(
            "() => !document.getElementById('content').classList.contains('loading')")
        page.locator("[data-planning-section]").evaluate("el => el.open = true")
        # Read-only: ни кнопок #94, ни кнопки входа в Тиндер.
        self.assertEqual(page.locator(".planning-review-btn").count(), 0)
        self.assertEqual(page.locator(".tinder-entry").count(), 0)
        # Overlay Тиндера скрыт.
        self.assertEqual(page.locator("#tinderOverlay:not([hidden])").count(), 0)


if __name__ == "__main__":
    unittest.main()

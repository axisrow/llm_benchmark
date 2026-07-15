"""Функциональная оценка HTML-калькуляторов проекта library_fine (#119).

Каждая модель генерирует автономный HTML-калькулятор библиотечного штрафа с
функцией расчёта, независимой от интерфейса (требование задания). Модуль
оценивает САМУ формулу: извлекает встроенный <script> из HTML-артефакта,
исполняет его во встраиваемом JS-движке (без браузера и без Node), вызывает
функцию расчёта на детерминированной матрице тестовых комбинаций и сравнивает
итоги с Python-эталоном. Формат результата: «X комбинаций из Y».

Дополнительно проверяется автономность («без внешних библиотек»): HTML не должен
подключать внешние <script src>/CDN/import-с-URL. Путь импорта XLSX сознательно
НЕ тестируется — оценивается только формула и автономность.

Интерпретации неоднозначностей задания (эталон следует им строго; источник
правды — вручную выверенная матрица 34 кейсов data/library_fine_matrix.json,
sync-тест сверяет эталон с её ожиданиями, #126):
  I1: округление вверх до десятков (правило 9) применяется к результату
      КАЖДОГО шага расчёта: к ставке после студенческого ×0.77
      (25 × 0.77 = 19.25 → 20), к каждому дневному штрафу и к сумме после
      пенсионерского ×0.46. Базовые ставки 25/75 — константы задания, а не
      результат расчёта, они не округляются.
  I2: +240 за повторное нарушение (правило 5) добавляется к сумме дневных
      штрафов ДО пенсионерского ×0.46 и до правила 10 («до применения всех
      скидок» — скидка применяется к сумме вместе с надбавкой).
  I3: n льготного дня (правило 6) — номер ЗАЧТЁННОГО (рабочего) дня просрочки;
      выходные льготные дни не потребляют.
  I4: у студента «редкая считается обычной» (правило 7) меняет базовую ставку
      на 25, добавляет множитель ×0.77 и задаёт льготный период 8 дней.
  I5: студент+пенсионер (правило 8): сначала базовая ставка обычной книги
      меняет знак (25 → −25), затем студенческое ×0.77 и округление ставки
      (I1): ceil10(−25 × 0.77) = −10; дальше расчёт идёт по ставке −10.
      Пенсионерский множитель ×0.46 отменяется; альтернативная ставка −75 не
      используется.
  I6: при нулевой просрочке (0 зачтённых дней) штрафа нет вовсе — итог 0,
      надбавка за повтор и правило 10 не применяются. При наличии просрочки
      правило 10 действует: сначала max(·, 20), затем min(·, залог) — при
      залоге < 20 итог равен залогу; после правила 10 округления нет (иначе
      итог мог бы снова превысить залог). Первоначальное прочтение («правило
      10 безусловно, min 20 даже без просрочки») отвергнуто калибровкой:
      консенсус 9 из 13 реализаций на всех zero-day комбинациях — 0.
  I7: ceil10 для отрицательных значений — математический ceil, к +∞
      (−4.04 → 0, −11.74 → −10).
  I8: фактическая дата ≤ контрольной — 0 дней просрочки (не ошибка записи).
  I9: дни ПОЛНОГО тарифа после льготного периода начисляются одним шагом:
      ⌈k × ставка⌉₁₀ за k дней, а не ⌈ставка⌉₁₀ за каждый день отдельно.

Эталон считает в точной арифметике (Fraction): float-шум реализаций на границах
округления (напр. 500 × 0.46 = 230.00000000000003 → ceil10 = 240 вместо 230) —
дефект реализации, а не эталона.

Статусы оценки одного HTML:
  graded      — функция найдена, адаптер выбран, счёт passed/total валиден;
  no_function — скрипт исполнен, функция расчёта не найдена;
  parse_error — нет вызываемой независимой функции расчёта либо ни одно
                представление входов/результата не дало числовой оценки;
  exec_error  — нет встроенных скриптов / движок упал фатально;
  unavailable — ни один JS-движок не установлен (mini-racer / quickjs).

Имена функций и полей результата не являются частью задания и не хардкодятся.
Грейдер находит объявленные пользователем функции, пробует представления восьми
полей в заданном заданием порядке и рекурсивно сравнивает числовые листья
результата. Обращение к DOM во время вызова кандидата означает, что функция не
независима от интерфейса, и такой кандидат не оценивается.
"""

import datetime as dt
import json
import math
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from fractions import Fraction
from html.parser import HTMLParser
from pathlib import Path

from artifacts import ARTIFACT_KIND_AGENT_FILE, RunArtifact

# Каноническое имя проекта в базе; используется CLI и гейтом pipeline-оценки.
PROJECT_NAME = "library_fine"

# --- константы задания ---------------------------------------------------------

BASE_RATE_REGULAR = 25
BASE_RATE_RARE = 75
REPEAT_SURCHARGE = 240
GRACE_REGULAR = 8
GRACE_RARE = 5
STUDENT_RATE_MULT = Fraction(77, 100)
PENSIONER_TOTAL_MULT = Fraction(46, 100)
MIN_FINE = 20

GRADE_STATUS_GRADED = "graded"
GRADE_STATUS_NO_FUNCTION = "no_function"
GRADE_STATUS_PARSE_ERROR = "parse_error"
# Совместимость импортов старых клиентов; новые результаты это значение не
# используют и в отчётах всегда пишут parse_error.
GRADE_STATUS_NO_ADAPTER = GRADE_STATUS_PARSE_ERROR
GRADE_STATUS_EXEC_ERROR = "exec_error"
GRADE_STATUS_UNAVAILABLE = "unavailable"

# Статусы уровня копии — строки совпадают с контрактом lint_metrics (#100/#101),
# чтобы будущая интеграция в _summarize/raw_json была единообразной.
FINE_STATUS_CHECKED = "checked"
FINE_STATUS_NA = "na"
FINE_STATUS_UNAVAILABLE = "unavailable"
FINE_STATUS_PARSE_ERROR = "parse_error"

_HTML_SUFFIXES = (".html", ".htm")

# Таймаут ОДНОГО eval в движке: защита от while(true) в коде модели. Один eval
# гоняет всю матрицу для одного кандидата-адаптера, так что это же — потолок
# времени кандидата.
_EVAL_TIMEOUT_SEC = 10.0
# Жёсткий потолок кучи V8 (mini-racer) на ОДИН eval: артефакты — недоверенный код
# моделей, и без лимита бесконечная аллокация убивает процесс OOM-киллером ОС
# ДО того, как сработает таймаут (V8 isolate по умолчанию растёт до ~1.5 ГБ).
# Лимит поднимается как JSOOMException (подкласс Exception) и гасится общей
# границей grade_html → статус no_adapter/exec_error, а не крашем прогона.
# Это лишь defense in depth: V8 external buffers (TypedArray/ArrayBuffer) его
# обходят, поэтому основная защита — отдельный процесс (см. grade_html/_run_isolated).
_EVAL_MAX_MEMORY_BYTES = 256 * 1024 * 1024
# Верхняя оценка числа кандидатов-адаптеров (для wall-clock дедлайна дочернего
# процесса: каждый адаптер — один eval с потолком eval_timeout_sec). Реальное
# множество меньше (зависит от арности), это безопасный потолок.
_MAX_ADAPTERS = 20
_MAX_CANDIDATE_FUNCTIONS = 40


# --- эталон: правила 1–10 ------------------------------------------------------


def ceil10(value: float | Fraction) -> int:
    """Округление вверх до десятков (правило 9); отрицательные — к +∞ (I7)."""
    return math.ceil(Fraction(value) / 10) * 10


def overdue_days(control: dt.date, actual: dt.date) -> int:
    """Число дней просрочки: со следующего дня после контрольной даты по день
    возврата включительно (правило 2), сб/вс не считаются (правило 3), возврат
    не позже срока — 0 дней (I8)."""
    if actual <= control:
        return 0
    days = 0
    day = control + dt.timedelta(days=1)
    while day <= actual:
        if day.weekday() < 5:
            days += 1
        day += dt.timedelta(days=1)
    return days


@dataclass(frozen=True)
class FineCase:
    """Одна каноническая тестовая комбинация входных данных."""

    name: str
    fio: str
    control_date: dt.date
    actual_date: dt.date
    category: int  # 0 — обычная, 1 — редкая
    deposit: int
    student: int  # 0/1
    pensioner: int  # 0/1
    repeat: int  # 0/1
    tags: tuple[str, ...]


def reference_fine(case: FineCase) -> int:
    """Эталонный расчёт штрафа по правилам 1–10 (интерпретации I1–I9).

    Вся арифметика — точная (Fraction), чтобы границы округления не зависели от
    float-представления (см. docstring модуля).
    """
    days = overdue_days(case.control_date, case.actual_date)
    if days == 0:
        return 0  # I6: нет просрочки — нет штрафа (ни +240, ни min 20)
    rate = Fraction(BASE_RATE_RARE if case.category else BASE_RATE_REGULAR)
    grace = GRACE_RARE if case.category else GRACE_REGULAR
    pensioner_discount = bool(case.pensioner)
    if case.student:
        # I4: редкая считается обычной, льготный период — 8 дней.
        base = Fraction(BASE_RATE_REGULAR)
        grace = GRACE_REGULAR
        if case.pensioner:
            # I5: сначала −25, затем ×0.77; ×0.46 отменяется.
            base = -base
            pensioner_discount = False
        rate = Fraction(ceil10(base * STUDENT_RATE_MULT))  # I1: ставка — шаг

    total = Fraction(0)
    for n in range(1, min(days, grace) + 1):
        daily = rate * Fraction(21 + 10 * (n - 1), 100)
        total += ceil10(daily)  # I1: дневное округление
    if days > grace:
        total += ceil10((days - grace) * rate)  # I9: полные дни одним шагом
    if case.repeat:
        total += REPEAT_SURCHARGE  # I2: до суммовых скидок и правила 10
    if pensioner_discount:
        total = Fraction(ceil10(total * PENSIONER_TOTAL_MULT))  # I1
    fine = max(int(total), MIN_FINE)  # I6: безусловно, сначала минимум...
    return min(fine, case.deposit)  # ...затем потолок залогом


# --- тестовая матрица: 34 курируемых кейса --------------------------------------
#
# Источник правды (#126) — data/library_fine_matrix.json: вход и ВРУЧНУЮ
# выверенный ожидаемый итог каждого кейса. Эталон reference_fine обязан
# воспроизводить все ожидания матрицы — sync-тест в
# tests/test_library_fine_grading.py сверяет их напрямую по JSON.

MATRIX_PATH = Path(__file__).resolve().parent / "data" / "library_fine_matrix.json"

_CASE_FIO = "Иванов Иван"


def _load_matrix(path: Path = MATRIX_PATH) -> tuple[FineCase, ...]:
    """Читает кейсы из calculations.rows: имя — mNN по полю id, теги — источник
    комбинации и её условия (для калибровки/отладки)."""
    rows = json.loads(path.read_text(encoding="utf-8"))["calculations"]["rows"]
    return tuple(
        FineCase(
            name=f"m{row['id']:02d}",
            fio=_CASE_FIO,
            control_date=dt.date.fromisoformat(row["input"]["dueDate"]),
            actual_date=dt.date.fromisoformat(row["input"]["returnDate"]),
            category=row["input"]["category"],
            deposit=row["input"]["pledgeAmount"],
            student=row["input"]["student"],
            pensioner=row["input"]["pensioner"],
            repeat=row["input"]["repeat"],
            tags=(row["source"], *row["conditions"]),
        )
        for row in rows
    )


TEST_MATRIX: tuple[FineCase, ...] = _load_matrix()


def expected_vector(matrix: tuple[FineCase, ...] = TEST_MATRIX) -> dict[str, int]:
    """Эталонные значения всей матрицы: {имя кейса → штраф}."""
    return {case.name: reference_fine(case) for case in matrix}


# --- извлечение скриптов и проверка автономности --------------------------------


_JS_SCRIPT_TYPES = ("", "text/javascript", "application/javascript", "module")


class _ScriptCollector(HTMLParser):
    """Собирает встроенные JS-скрипты и src/href внешних ресурсов."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.inline_scripts: list[str] = []
        self.script_srcs: list[str] = []
        self.stylesheet_hrefs: list[str] = []
        self._buffer: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attrs_dict = {k.lower(): (v or "") for k, v in attrs}
        if tag == "script":
            src = attrs_dict.get("src")
            if src is not None:
                self.script_srcs.append(src)
                return
            if (attrs_dict.get("type") or "").strip().lower() in _JS_SCRIPT_TYPES:
                self._buffer = []
        elif tag == "link":
            rel = (attrs_dict.get("rel") or "").strip().lower()
            href = attrs_dict.get("href")
            if "stylesheet" in rel and href:
                self.stylesheet_hrefs.append(href)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script" and self._buffer is not None:
            self.inline_scripts.append("".join(self._buffer))
            self._buffer = None

    def handle_data(self, data: str) -> None:
        if self._buffer is not None:
            self._buffer.append(data)


def _collect(html: str) -> _ScriptCollector:
    collector = _ScriptCollector()
    collector.feed(html)
    collector.close()
    return collector


def extract_inline_scripts(html: str) -> list[str]:
    """Тексты встроенных <script> без src (JS-типы, включая module)."""
    return [s for s in _collect(html).inline_scripts if s.strip()]


# import ... from "url" / import("url") / importScripts("url") во встроенном JS
_JS_REMOTE_IMPORT_RE = re.compile(
    r"""(?:\bimport\s*(?:\(\s*|(?:[\w{}\s,*$]+\s+from\s*)?)
        |\bimportScripts\s*\(\s*)
        ['"](?:https?:)?//[^'"]+['"]""",
    re.VERBOSE,
)


def check_autonomy(html: str) -> tuple[str, ...]:
    """Нарушения автономности («в одном HTML-файле, без внешних библиотек»).

    Пустой кортеж — файл автономен. Любой <script src> (внешний или локальный)
    и любая внешняя таблица стилей нарушают требование одного файла; data:-URI
    допустимы. Дополнительно ловятся динамические импорты по URL внутри
    встроенного JS.
    """
    collector = _collect(html)
    violations: list[str] = []
    for src in collector.script_srcs:
        if not src.strip().lower().startswith("data:"):
            violations.append(f"внешний <script src>: {src}")
    for href in collector.stylesheet_hrefs:
        stripped = href.strip().lower()
        if not stripped.startswith("data:"):
            violations.append(f"внешняя таблица стилей: {href}")
    for script in collector.inline_scripts:
        for match in _JS_REMOTE_IMPORT_RE.finditer(script):
            violations.append(f"импорт по URL во встроенном JS: {match.group(0)[:120]}")
    return tuple(violations)


# --- встраиваемые JS-движки -----------------------------------------------------


class JsEngine:
    """Общий контракт бэкендов: run() исполняет код, eval_json() вычисляет
    выражение, ОБЯЗАННОЕ вернуть JSON-строку (протокол Python↔JS — только
    строки, без маршалинга объектов)."""

    name = "?"

    def _eval(self, code: str) -> object:  # pragma: no cover - переопределяется
        raise NotImplementedError

    def run(self, code: str) -> None:
        self._eval(code)

    def eval_json(self, expr: str) -> object:
        raw = self._eval(expr)
        if not isinstance(raw, str):
            raise RuntimeError(
                f"движок {self.name} вернул {type(raw).__name__} вместо JSON-строки")
        return json.loads(raw)


class _MiniRacerEngine(JsEngine):
    name = "mini-racer"

    def __init__(self, eval_timeout_sec: float,
                 max_memory_bytes: int = _EVAL_MAX_MEMORY_BYTES) -> None:
        from py_mini_racer import MiniRacer

        self._ctx = MiniRacer()
        self._timeout = eval_timeout_sec
        self._max_memory = max_memory_bytes

    def _eval(self, code: str) -> object:
        # max_memory — потолок кучи V8 на этот eval (см. _EVAL_MAX_MEMORY_BYTES):
        # недоверенный код артефакта иначе может уйти в OOM-килл ДО таймаута.
        return self._ctx.eval(code, timeout_sec=self._timeout,
                              max_memory=self._max_memory)


class _QuickJsEngine(JsEngine):
    name = "quickjs"

    def __init__(self, eval_timeout_sec: float) -> None:
        import quickjs

        self._ctx = quickjs.Context()
        # set_time_limit принимает целые секунды; минимум 1
        self._ctx.set_time_limit(max(1, math.ceil(eval_timeout_sec)))
        self._ctx.set_memory_limit(256 * 1024 * 1024)

    def _eval(self, code: str) -> object:
        return self._ctx.eval(code)


def create_engine(prefer: str = "auto",
                  eval_timeout_sec: float = _EVAL_TIMEOUT_SEC,
                  max_memory_bytes: int = _EVAL_MAX_MEMORY_BYTES,
                  ) -> Callable[[], JsEngine] | None:
    """Фабрика JS-контекстов: mini-racer (V8) первым — модели пишут код под
    Chrome, V8 даёт браузерную семантику Date/Proxy; quickjs — fallback.
    Ни один не установлен → None (статус unavailable). Контекст создаётся
    заново на каждый артефакт — определения разных решений не пересекаются."""
    order = {
        "auto": ("mini-racer", "quickjs"),
        "mini-racer": ("mini-racer",),
        "quickjs": ("quickjs",),
    }.get(prefer)
    if order is None:
        raise ValueError(f"неизвестный движок: {prefer!r}")
    for name in order:
        try:
            if name == "mini-racer":
                import py_mini_racer  # noqa: F401

                return lambda: _MiniRacerEngine(eval_timeout_sec, max_memory_bytes)
            import quickjs  # noqa: F401

            return lambda: _QuickJsEngine(eval_timeout_sec)
        except ImportError:
            continue
    return None


# --- DOM-заглушка и JS-харнесс ---------------------------------------------------

# Один «поглощающий» Proxy: любое свойство/вызов/конструирование возвращает его
# же, приведение к примитиву — пустую строку/0, then отсутствует (не thenable).
# Назначается только на имена, которых нет в globalThis движка. Цель — пережить
# top-level код артефакта (document.getElementById(...).addEventListener и т.п.)
# до определения функций; колбэки (DOMContentLoaded, setTimeout) НЕ вызываются.
DOM_STUB_PRELUDE_JS = """
(function (g) {
  g.__gradeDomTracker = { active: false, touched: false };
  function touch() {
    if (g.__gradeDomTracker.active) g.__gradeDomTracker.touched = true;
  }
  var stub = new Proxy(function () {}, {
    get: function (target, prop) {
      touch();
      if (prop === Symbol.toPrimitive) return function () { return ""; };
      if (prop === "toString") return function () { return ""; };
      if (prop === "valueOf") return function () { return 0; };
      if (prop === "then") return undefined;
      return stub;
    },
    set: function () { touch(); return true; },
    has: function () { touch(); return true; },
    deleteProperty: function () { touch(); return true; },
    apply: function () { touch(); return stub; },
    construct: function () { touch(); return stub; }
  });
  var names = ["document", "window", "self", "navigator", "location", "history",
    "screen", "localStorage", "sessionStorage", "alert", "confirm", "prompt",
    "FileReader", "Blob", "File", "FileList", "DataTransfer", "URL",
    "URLSearchParams", "XLSX", "fetch", "XMLHttpRequest", "TextDecoder",
    "TextEncoder", "DecompressionStream", "CompressionStream", "Response",
    "Request", "Headers", "Worker", "importScripts", "requestAnimationFrame",
    "cancelAnimationFrame", "setTimeout", "setInterval", "clearTimeout",
    "clearInterval", "queueMicrotask", "addEventListener", "removeEventListener",
    "dispatchEvent", "getComputedStyle", "matchMedia", "MutationObserver",
    "ResizeObserver", "IntersectionObserver", "CustomEvent", "Event",
    "HTMLElement", "Element", "Node", "console"];
  for (var i = 0; i < names.length; i++) {
    if (!(names[i] in g)) {
      try { g[names[i]] = stub; } catch (e) {}
    }
  }
})(globalThis);
"""

# Харнесс вызова: построение аргументов по конвенции адаптера, извлечение числа
# из результата, прогон всей матрицы одним вызовом. Функция расчёта ищется через
# косвенный eval — он видит и function-декларации, и глобальные const/let.
GRADE_HARNESS_JS = r"""
function __gradePad2(n) { return (n < 10 ? "0" : "") + n; }

function __gradeMakeDate(rep, d) {
  if (rep === "date_local") return new Date(d.y, d.m - 1, d.d);
  if (rep === "date_utc") return new Date(Date.UTC(d.y, d.m - 1, d.d));
  if (rep === "iso") return d.y + "-" + __gradePad2(d.m) + "-" + __gradePad2(d.d);
  if (rep === "dmy") return __gradePad2(d.d) + "." + __gradePad2(d.m) + "." + d.y;
  throw new Error("unknown date rep: " + rep);
}

function __gradeUnique(items) {
  var out = [];
  for (var i = 0; i < items.length; i++) {
    if (out.indexOf(items[i]) < 0) out.push(items[i]);
  }
  return out;
}

function __gradeObjectKeys(fn) {
  var src = Function.prototype.toString.call(fn)
    .replace(/\/\*[\s\S]*?\*\//g, " ").replace(/\/\/[^\n\r]*/g, " ");
  var match = src.match(/^[^(]*\(([^)]*)\)/);
  if (!match) match = src.match(/^\s*(?:async\s+)?([^=()\s,]+)\s*=>/);
  if (!match) return [];
  var params = String(match[1]).trim();
  if (params.charAt(0) === "{") {
    var closing = params.indexOf("}");
    var destructured = closing >= 0 ? params.slice(1, closing) : params.slice(1);
    return __gradeUnique(destructured.split(",").map(function (part) {
      return part.trim().split(/[:=]/)[0].trim();
    }).filter(Boolean));
  }
  var first = params.split(",")[0].trim();
  if (!/^[A-Za-z_$][\w$]*$/.test(first)) return [];
  var escaped = first.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  var re = new RegExp("\\b" + escaped + "\\s*(?:\\.\\s*([A-Za-z_$][\\w$]*)|\\[\\s*['\\\"]([^'\\\"]+)['\\\"]\\s*\\])", "g");
  var keys = [], found;
  while ((found = re.exec(src))) keys.push(found[1] || found[2]);
  var destructure = new RegExp("\\{([^}]+)\\}\\s*=\\s*" + escaped, "g");
  while ((found = destructure.exec(src))) {
    found[1].split(",").forEach(function (part) {
      var key = part.trim().split(/[:=]/)[0].trim();
      if (key) keys.push(key);
    });
  }
  return __gradeUnique(keys);
}

function __gradeArgs(conv, rep, c, fn) {
  var control = __gradeMakeDate(rep, c.control);
  var actual = __gradeMakeDate(rep, c.actual);
  var ordered = [c.fio, control, actual, c.category, c.deposit, c.student,
                 c.pensioner, c.repeat];
  if (conv === "object8" || conv === "object7") {
    var keys = __gradeObjectKeys(fn);
    var values = conv === "object7" ? ordered.slice(1) : ordered;
    if (!keys.length || keys.length !== values.length) {
      throw new Error("cannot derive object fields");
    }
    var record = {};
    var mapping = conv === "object7" || conv === "object8"
      ? (arguments[4] || keys.map(function (_, idx) { return idx; })) : [];
    for (var i = 0; i < keys.length; i++) record[keys[i]] = values[mapping[i]];
    return [record];
  }
  if (conv === "positional7") {
    return [control, actual, c.category, c.deposit, c.student, c.pensioner, c.repeat];
  }
  if (conv === "positional8") {
    return [c.fio, control, actual, c.category, c.deposit, c.student,
            c.pensioner, c.repeat];
  }
  if (conv === "row") return [ordered];
  if (conv === "batch") {
    return [[ordered]];
  }
  throw new Error("unknown convention: " + conv);
}

function __gradeExtract(v, depth, path, seen, out) {
  if (typeof v === "number" && isFinite(v)) {
    out.push({ p: path, v: v }); return;
  }
  if (typeof v === "string" && v.trim() !== "" && isFinite(Number(v))) {
    out.push({ p: path, v: Number(v) }); return;
  }
  if (v && typeof v === "object" && depth < 5) {
    if (typeof v.then === "function") return;
    if (seen.indexOf(v) >= 0) return;
    seen.push(v);
    var keys = Object.keys(v);
    for (var i = 0; i < keys.length; i++) {
      var key = keys[i];
      __gradeExtract(v[key], depth + 1, path + "." + key, seen, out);
    }
  }
}

function __gradeResolve(name) {
  if (globalThis.__gradeFunctionRegistry
      && globalThis.__gradeFunctionRegistry[name]) {
    return globalThis.__gradeFunctionRegistry[name];
  }
  try {
    var fn = (0, eval)(name);
    if (typeof fn === "function") return fn;
  } catch (e) {}
  return null;
}

function __gradeDiscover(namesJson) {
  var names = JSON.parse(namesJson);
  var baseline = globalThis.__gradeBaselineNames || [];
  Object.getOwnPropertyNames(globalThis).forEach(function (name) {
    if (baseline.indexOf(name) < 0) names.push(name);
  });
  names = __gradeUnique(names);
  var found = [];
  globalThis.__gradeFunctionRegistry = {};
  function inspect(value, label, depth) {
    if (found.length >= 40) return;
    if (typeof value === "function") {
      if (label.indexOf("__grade") !== 0
          && !globalThis.__gradeFunctionRegistry[label]) {
        globalThis.__gradeFunctionRegistry[label] = value;
        found.push({ name: label, arity: value.length });
      }
      return;
    }
    if (!value || typeof value !== "object" || depth >= 2) return;
    Object.getOwnPropertyNames(value).forEach(function (key) {
      if (found.length >= 40) return;
      var descriptor;
      try { descriptor = Object.getOwnPropertyDescriptor(value, key); }
      catch (e) { return; }
      if (!descriptor || !("value" in descriptor)) return;
      inspect(descriptor.value, label + "." + key, depth + 1);
    });
  }
  for (var i = 0; i < names.length; i++) {
    var value;
    try { value = (0, eval)(names[i]); } catch (e) { continue; }
    inspect(value, names[i], 0);
  }
  return JSON.stringify(found);
}

function __gradeFindObjectMap(fnName, adapter, casesJson, expectedJson) {
  var fn = __gradeResolve(fnName);
  var cases = JSON.parse(casesJson);
  var expected = JSON.parse(expectedJson);
  var keys = __gradeObjectKeys(fn);
  var width = adapter.conv === "object7" ? 7 : 8;
  if (keys.length !== width) return JSON.stringify(null);
  var used = new Array(width).fill(false), mapping = [], best = null;
  function visit() {
    if (mapping.length < width) {
      for (var n = 0; n < width; n++) if (!used[n]) {
        used[n] = true; mapping.push(n); visit(); mapping.pop(); used[n] = false;
      }
      return;
    }
    var pathStats = {};
    // Для выбора перестановки достаточно компактной разнообразной выборки;
    // полный счёт всё равно считается ниже отдельным __gradeRun по всей матрице.
    var sampleSize = Math.min(cases.length, 8);
    for (var i = 0; i < sampleSize; i++) {
      globalThis.__gradeDomTracker.touched = false;
      globalThis.__gradeDomTracker.active = true;
      var value;
      try {
        value = fn.apply(null, __gradeArgs(adapter.conv, adapter.rep,
                                           cases[i], fn, mapping));
      } catch (e) {
        globalThis.__gradeDomTracker.active = false; return;
      } finally {
        globalThis.__gradeDomTracker.active = false;
      }
      if (globalThis.__gradeDomTracker.touched) return;
      var leaves = [];
      __gradeExtract(value, 0, "$", [], leaves);
      for (var j = 0; j < leaves.length; j++) {
        var stat = pathStats[leaves[j].p] || (pathStats[leaves[j].p] = [0, 0]);
        stat[1] += 1;
        if (Math.abs(leaves[j].v - expected[i]) < 1e-6) stat[0] += 1;
      }
    }
    Object.keys(pathStats).forEach(function (path) {
      var stat = pathStats[path];
      if (!best || stat[0] > best.rank[0]
          || (stat[0] === best.rank[0] && stat[1] > best.rank[1])) {
        best = { rank: stat, mapping: mapping.slice() };
      }
    });
  }
  visit();
  return JSON.stringify(best && best.mapping);
}

function __gradeRun(fnName, adapter, casesJson) {
  var fn = __gradeResolve(fnName);
  var cases = JSON.parse(casesJson);
  var out = [];
  for (var i = 0; i < cases.length; i++) {
    try {
      globalThis.__gradeDomTracker.touched = false;
      globalThis.__gradeDomTracker.active = true;
      var v;
      try {
        v = fn.apply(null, __gradeArgs(adapter.conv, adapter.rep, cases[i], fn,
                                       adapter.mapping));
      } finally {
        globalThis.__gradeDomTracker.active = false;
      }
      if (globalThis.__gradeDomTracker.touched) {
        out.push({ e: "interface-dependent function" });
        continue;
      }
      var values = [];
      __gradeExtract(v, 0, "$", [], values);
      out.push(values.length ? { values: values } : { e: "non-numeric result" });
    } catch (e) {
      globalThis.__gradeDomTracker.active = false;
      out.push({ e: String((e && e.message) || e) });
    }
  }
  return JSON.stringify(out);
}
"""


# --- адаптеры вызова -------------------------------------------------------------


@dataclass(frozen=True)
class AdapterSpec:
    """Кандидат-адаптер: конвенция вызова × представление дат."""

    name: str
    convention: str  # object7 | object8 | positional7 | positional8 | row | batch
    date_rep: str  # date_local | date_utc | iso | dmy


_DATE_REPS_FULL = ("date_local", "date_utc", "iso", "dmy")
_DATE_REPS_STRINGS = ("dmy", "iso")  # batch — сырые строки, даты только текстом


def _adapters(convention: str, reps: tuple[str, ...]) -> list[AdapterSpec]:
    return [AdapterSpec(f"{convention}+{rep}", convention, rep) for rep in reps]


def candidate_adapters(arity: int) -> list[AdapterSpec]:
    """Множество кандидатов по арности функции расчёта. Неожиданная арность —
    пробуем всё (benefit of the doubt)."""
    one_arg = (
        _adapters("row", _DATE_REPS_FULL)
        + _adapters("batch", _DATE_REPS_STRINGS)
        + _adapters("object8", _DATE_REPS_FULL)
        + _adapters("object7", _DATE_REPS_FULL)
    )
    pos7 = _adapters("positional7", _DATE_REPS_FULL)
    pos8 = _adapters("positional8", _DATE_REPS_FULL)
    if arity <= 1:
        return one_arg
    if arity == 7:
        return pos7
    if arity == 8:
        return pos8
    return one_arg + pos7 + pos8


_JS_FUNCTION_DECL_RE = re.compile(
    r"\b(?:async\s+)?function\s*\*?\s*([A-Za-z_$][\w$]*)\s*\("
)
_JS_BINDING_RE = re.compile(
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*="
)


def _candidate_function_names(scripts: Iterable[str]) -> list[str]:
    """Имена объявленных функций без словаря предметных имён.

    Статический список дополняется в JS новыми function-свойствами globalThis.
    Здесь нужны прежде всего lexical const/let, которых нет среди global props.
    Ложные совпадения безвредны: __gradeResolve оставит только функции.
    """
    names: list[str] = []
    for script in scripts:
        for regex in (_JS_FUNCTION_DECL_RE, _JS_BINDING_RE):
            names.extend(match.group(1) for match in regex.finditer(script))
    return list(dict.fromkeys(names))[:_MAX_CANDIDATE_FUNCTIONS]


def _case_payload(case: FineCase) -> dict[str, object]:
    def date_parts(d: dt.date) -> dict[str, int]:
        return {"y": d.year, "m": d.month, "d": d.day}

    return {
        "fio": case.fio,
        "control": date_parts(case.control_date),
        "actual": date_parts(case.actual_date),
        "category": case.category,
        "deposit": case.deposit,
        "student": case.student,
        "pensioner": case.pensioner,
        "repeat": case.repeat,
    }


# --- оценка одного HTML -----------------------------------------------------------


@dataclass(frozen=True)
class ComboOutcome:
    """Итог одной комбинации: эталон, факт, совпадение либо текст ошибки."""

    case_name: str
    expected: int
    actual: float | None
    match: bool
    error: str | None


@dataclass(frozen=True)
class HtmlGrade:
    """Оценка одного HTML-блоба (без привязки к отчёту)."""

    status: str
    function_name: str | None
    fn_arity: int | None
    adapter: str | None
    passed: int
    total: int
    outcomes: tuple[ComboOutcome, ...]
    autonomy_violations: tuple[str, ...]
    engine: str | None
    exec_warning: str | None
    error: str | None


def _grade_stub(status: str, matrix: tuple[FineCase, ...],
                autonomy: tuple[str, ...], *, engine: str | None = None,
                function_name: str | None = None, fn_arity: int | None = None,
                exec_warning: str | None = None, error: str | None = None,
                ) -> HtmlGrade:
    return HtmlGrade(status=status, function_name=function_name,
                     fn_arity=fn_arity, adapter=None, passed=0,
                     total=len(matrix), outcomes=(), autonomy_violations=autonomy,
                     engine=engine, exec_warning=exec_warning, error=error)


def _outcomes_from_raw(raw: object, matrix: tuple[FineCase, ...],
                       expected: list[int]) -> list[tuple[ComboOutcome, ...]]:
    """Числовые пути результата харнесса → варианты ComboOutcome.

    Один объект может содержать несколько чисел. Сравниваем один и тот же путь
    на всей матрице и отдаём все варианты вызывающему коду, который выбирает
    лучший. Так ключ результата не является частью контракта и не хардкодится.
    """
    if not isinstance(raw, list) or len(raw) != len(matrix):
        return []
    paths: list[str] = []
    values_by_case: list[dict[str, float]] = []
    errors: list[str] = []
    for entry in raw:
        values: dict[str, float] = {}
        if isinstance(entry, dict):
            direct = entry.get("v")
            if isinstance(direct, (int, float)) and math.isfinite(direct):
                values["$"] = float(direct)
                if "$" not in paths:
                    paths.append("$")
            for item in entry.get("values") or ():
                if not isinstance(item, dict):
                    continue
                path, value = item.get("p"), item.get("v")
                if (isinstance(path, str)
                        and isinstance(value, (int, float))
                        and math.isfinite(value)):
                    values[path] = float(value)
                    if path not in paths:
                        paths.append(path)
        values_by_case.append(values)
        error_raw = entry.get("e") if isinstance(entry, dict) else None
        errors.append(str(error_raw)[:200] if error_raw else "non-numeric result")

    variants: list[tuple[ComboOutcome, ...]] = []
    for path in paths:
        outcomes = []
        for case, exp, values, error in zip(matrix, expected,
                                             values_by_case, errors):
            actual = values.get(path)
            outcomes.append(ComboOutcome(
                case_name=case.name,
                expected=exp,
                actual=actual,
                match=actual is not None and abs(actual - exp) < 1e-6,
                error=None if actual is not None else error,
            ))
        variants.append(tuple(outcomes))
    return variants


class _IsolationTimeout(Exception):
    """Дочерний процесс оценки не уложился в wall-clock дедлайн."""


def _run_isolated(payload: dict, deadline_sec: float) -> HtmlGrade:
    """Запускает _isolated_grade(payload) в дочернем процессе (spawn) с
    wall-clock дедлайном.

    Превышение дедлайна или гибель дочернего процесса (OOM-килл под RLIMIT_AS,
    краш V8) → дочерний процесс терминируется, поднимается _IsolationTimeout /
    переупаковывается в исключение. Родительский процесс при этом не страдает.
    """
    import multiprocessing as mp

    ctx = mp.get_context("spawn")
    recv, send = ctx.Pipe(duplex=False)
    child = ctx.Process(target=_isolated_entrypoint, args=(payload, send))
    child.daemon = True
    child.start()
    send.close()  # родительский конец записи; EOF в дочернем при выходе родителя
    try:
        if recv.poll(max(0.0, deadline_sec)):
            result = recv.recv()
        else:
            child.terminate()
            raise _IsolationTimeout()
    finally:
        child.join(timeout=_CHILD_TIMEOUT_GRACE_SEC)
        if child.is_alive():
            child.kill()
            child.join(timeout=_CHILD_TIMEOUT_GRACE_SEC)
        recv.close()
    if child.exitcode != 0:
        raise RuntimeError(f"дочерний процесс вышел с кодом {child.exitcode}")
    if isinstance(result, _ChildError):
        raise RuntimeError(result.message)
    return result


@dataclass(frozen=True)
class _ChildError:
    """Маркер исключения дочернего процесса (не HtmlGrade)."""

    message: str


def _isolated_entrypoint(payload: dict, send) -> None:
    """Обёртка-тело дочернего процесса: ловит ВСЁ, пишет результат в pipe.

    multiprocessing.Process проглатывает исключения тела молча; мы перехватываем
    их сами, чтобы родитель узнал о крахе (OOM под RLIMIT_AS, ошибка движка), а
    не получил «процесс молча умер». MemoryError под RLIMIT_AS попадёт сюда.
    """
    try:
        result = _isolated_grade(payload)
    except BaseException as exc:  # noqa: BLE001 — цель: любой сбой → родитель
        result = _ChildError(f"{type(exc).__name__}: {str(exc)[:200]}")
    try:
        send.send(result)
    except Exception:
        pass


def _grade_html_inproc(html: str, matrix: tuple[FineCase, ...],
                       prefer_engine: str, eval_timeout_sec: float,
                       max_memory_bytes: int) -> HtmlGrade:
    """Внутрипроцессная оценка HTML (см. grade_html) — без изоляции.

    Выделена отдельно, чтобы grade_html мог запустить её в дочернем процессе с
    OS-лимитами: недоверенный JS артефакта не должен иметь возможности утащить
    грейдер в OOM-килл (см. _isolated_grade).
    """
    autonomy = check_autonomy(html)

    factory = create_engine(prefer_engine, eval_timeout_sec, max_memory_bytes)
    if factory is None:
        return _grade_stub(GRADE_STATUS_UNAVAILABLE, matrix, autonomy,
                           error="JS-движок не установлен (mini-racer/quickjs)")

    scripts = extract_inline_scripts(html)
    if not scripts:
        return _grade_stub(GRADE_STATUS_EXEC_ERROR, matrix, autonomy,
                           error="в HTML нет встроенных <script>")

    exec_warning: str | None = None
    try:
        engine = factory()
        engine.run(DOM_STUB_PRELUDE_JS)
        engine.run("var __gradeBaselineNames = Object.getOwnPropertyNames(globalThis);")
        for idx, script in enumerate(scripts, start=1):
            try:
                engine.run(script)
            except Exception as exc:
                # function-декларации хойстятся и переживают падение top-level —
                # фиксируем предупреждение и продолжаем оценку
                note = f"скрипт #{idx}: {str(exc)[:200]}"
                exec_warning = f"{exec_warning}; {note}" if exec_warning else note
        engine.run(GRADE_HARNESS_JS)
        candidate_names = _candidate_function_names(scripts)
        found = engine.eval_json(
            f"__gradeDiscover({json.dumps(json.dumps(candidate_names))})")
    except Exception as exc:
        return _grade_stub(GRADE_STATUS_EXEC_ERROR, matrix, autonomy,
                           exec_warning=exec_warning, error=str(exc)[:300])

    if not isinstance(found, list) or not found:
        return _grade_stub(GRADE_STATUS_NO_FUNCTION, matrix, autonomy,
                           engine=engine.name, exec_warning=exec_warning,
                           error="вызываемая функция расчёта не найдена")

    expected = [reference_fine(case) for case in matrix]
    cases_json = json.dumps([_case_payload(case) for case in matrix],
                            ensure_ascii=False)

    best: tuple[
        tuple[int, int], str, int, AdapterSpec, tuple[ComboOutcome, ...]
    ] | None = None
    for candidate in found[:_MAX_CANDIDATE_FUNCTIONS]:
        if not isinstance(candidate, dict) or not candidate.get("name"):
            continue
        fn_name = str(candidate["name"])
        arity = int(candidate.get("arity") or 0)
        for adapter in candidate_adapters(arity):
            adapter_payload: dict[str, object] = {
                "conv": adapter.convention,
                "rep": adapter.date_rep,
            }
            if adapter.convention.startswith("object"):
                map_call = (
                    f"__gradeFindObjectMap({json.dumps(fn_name)}, "
                    f"{json.dumps(adapter_payload)}, {json.dumps(cases_json)}, "
                    f"{json.dumps(json.dumps(expected))})"
                )
                try:
                    mapping = engine.eval_json(map_call)
                except Exception:
                    continue
                if not isinstance(mapping, list):
                    continue
                adapter_payload["mapping"] = mapping
            call = (f"__gradeRun({json.dumps(fn_name)}, "
                    f"{json.dumps(adapter_payload)}, "
                    f"{json.dumps(cases_json)})")
            try:
                raw = engine.eval_json(call)
            except Exception:
                # таймаут/крах этого кандидата — не приговор остальным
                continue
            for outcomes in _outcomes_from_raw(raw, matrix, expected):
                numeric = sum(1 for o in outcomes if o.actual is not None)
                if numeric == 0:
                    continue
                passed = sum(1 for o in outcomes if o.match)
                rank = (passed, numeric)
                if best is None or rank > best[0]:
                    best = (rank, fn_name, arity, adapter, outcomes)

    if best is None:
        return _grade_stub(
            GRADE_STATUS_PARSE_ERROR, matrix, autonomy,
            engine=engine.name, exec_warning=exec_warning,
            error=("нет вызываемой независимой от интерфейса функции расчёта, "
                   "которая вернула числовой результат"),
        )

    (passed, _), fn_name, arity, adapter, outcomes = best
    return HtmlGrade(status=GRADE_STATUS_GRADED, function_name=fn_name,
                     fn_arity=arity, adapter=adapter.name, passed=passed,
                     total=len(matrix), outcomes=outcomes,
                     autonomy_violations=autonomy, engine=engine.name,
                     exec_warning=exec_warning, error=None)


# --- изоляция недоверенного JS в дочернем процессе ------------------------------
#
# Артефакты — недоверенный код моделей. Лимит кучи движка (max_memory /
# set_memory_limit) — defense in depth, но НЕ достаточен: V8 external buffers
# (TypedArray/ArrayBuffer backing storage) обходят max_memory, а quickjs-лимит
# накапливается. Поэтому оценка каждого артефакта идёт в одноразовом дочернем
# процессе с жёстким RLIMIT_AS (всё адресное пространство — куча И external
# buffers) и wall-clock дедлайном; превышение убивает дочерний процесс, родитель
# получает exec_error, а не OOM-килл всего грейдера.

_CHILD_TIMEOUT_GRACE_SEC = 5.0


def _isolated_grade(payload: dict) -> HtmlGrade:
    """Точка входа ДОЧЕРНЕГО процесса: считает оценку. Top-level + picklable-
    аргумент — требования multiprocessing spawn. HtmlGrade (frozen dataclass из
    строк/чисел/tuple) сериализуется.

    Никакого RLIMIT_AS намеренно НЕ ставится: V8 (mini-racer) через
    PartitionAlloc резервирует гигабайты виртуального адресного пространства ещё
    до реальной аллокации, и ограничение адресного пространства убивает его
    фаталом (`partition_address_space.cc: Check failed`) на Linux. Поэтому
    защита от разрастания недоверенного JS — НЕ лимит адресного пространства, а:
      (1) отдельный процесс — внешний ArrayBuffer, разрастаясь, убивает OOM-
          киллером только ДОЧЕРНИЙ, родитель ловит ненулевой exit/EOF → exec_error;
      (2) лимит кучи движка (max_memory у V8, set_memory_limit у quickjs) —
          defense in depth для самой кучи;
      (3) wall-clock дедлайн _run_isolated — от зависания.
    """
    return _grade_html_inproc(
        html=payload["html"],
        matrix=payload["matrix"],
        prefer_engine=payload["prefer_engine"],
        eval_timeout_sec=payload["eval_timeout_sec"],
        max_memory_bytes=payload["max_memory_bytes"],
    )


def grade_html(content: bytes, *, matrix: tuple[FineCase, ...] = TEST_MATRIX,
               prefer_engine: str = "auto",
               eval_timeout_sec: float = _EVAL_TIMEOUT_SEC,
               max_memory_bytes: int = _EVAL_MAX_MEMORY_BYTES,
               isolated: bool = True) -> HtmlGrade:
    """Оценивает один HTML-артефакт: формула («X из Y») + автономность.

    По умолчанию (isolated=True) оценка исполняется в одноразовом дочернем
    процессе с wall-clock дедлайном — недоверенный JS артефакта не может утащить
    грейдер в OOM: внешний ArrayBuffer, разрастаясь, убивает только дочерний
    процесс (OOM-киллером ОС), родитель ловит exec_error. isolated=False — внутри
    текущего процесса (для тестов/отладки, БЕЗ защиты). В обоих случаях лимит
    кучи движка (max_memory / set_memory_limit) остаётся defense in depth.

    Внимание: намеренно НЕ используется RLIMIT_AS — V8 через PartitionAlloc
    резервирует гигабайты виртуального адресного пространства до реальной
    аллокации, и ограничение адресного пространства убивает его фаталом на Linux.
    """
    html = content.decode("utf-8", errors="replace")
    if not isolated:
        return _grade_html_inproc(html, matrix, prefer_engine,
                                  eval_timeout_sec, max_memory_bytes)

    payload = {
        "html": html,
        "matrix": matrix,
        "prefer_engine": prefer_engine,
        "eval_timeout_sec": eval_timeout_sec,
        "max_memory_bytes": max_memory_bytes,
    }
    deadline = eval_timeout_sec * _MAX_ADAPTERS + _CHILD_TIMEOUT_GRACE_SEC
    try:
        return _run_isolated(payload, deadline)
    except _IsolationTimeout:
        return _grade_stub(GRADE_STATUS_EXEC_ERROR, matrix, check_autonomy(html),
                           error=f"дочерний процесс превысил дедлайн {deadline:.0f}s")
    except Exception as exc:  # noqa: BLE001 — крах/убийство дочернего процесса
        return _grade_stub(GRADE_STATUS_EXEC_ERROR, matrix, check_autonomy(html),
                           error=f"дочерний процесс упал: {str(exc)[:200]}")


# --- оценка артефактов из базы ----------------------------------------------------


@dataclass(frozen=True)
class ArtifactGrade:
    """HtmlGrade + координаты артефакта в базе."""

    report_id: int
    run_idx: int
    path: str
    sha256: str
    grade: HtmlGrade


def grade_report(conn: sqlite3.Connection, report_id: int, *,
                 matrix: tuple[FineCase, ...] = TEST_MATRIX,
                 prefer_engine: str = "auto",
                 cache: dict[str, HtmlGrade] | None = None) -> list[ArtifactGrade]:
    """Оценивает все HTML-артефакты отчёта. cache (по sha256) избавляет от
    повторной оценки одинаковых блобов между отчётами."""
    from db import list_artifacts, read_artifact

    grades: list[ArtifactGrade] = []
    for row in list_artifacts(conn, report_id):
        if row["kind"] != ARTIFACT_KIND_AGENT_FILE:
            continue
        if not row["path"].lower().endswith(_HTML_SUFFIXES):
            continue
        sha = row["sha256"]
        grade = cache.get(sha) if cache is not None else None
        if grade is None:
            content = read_artifact(conn, report_id, row["run_idx"], row["path"])
            grade = grade_html(content, matrix=matrix, prefer_engine=prefer_engine)
            if cache is not None:
                cache[sha] = grade
        grades.append(ArtifactGrade(report_id=report_id, run_idx=row["run_idx"],
                                    path=row["path"], sha256=sha, grade=grade))
    return grades


def calibrate(grades: list[ArtifactGrade],
              matrix: tuple[FineCase, ...] = TEST_MATRIX,
              ) -> list[dict[str, object]]:
    """Калибровка эталона: по каждой комбинации — эталон, значения всех graded
    реализаций и модальный консенсус. Инструмент проверки интерпретаций I1–I8:
    если консенсус систематически расходится с эталоном на ambig-кейсах,
    интерпретацию стоит пересмотреть."""
    graded = [g for g in grades
              if g.grade.status == GRADE_STATUS_GRADED
              and len(g.grade.outcomes) == len(matrix)]
    rows: list[dict[str, object]] = []
    for i, case in enumerate(matrix):
        expected = reference_fine(case)
        values: dict[str, float] = {}
        errors: dict[str, str] = {}
        for ag in graded:
            outcome = ag.grade.outcomes[i]
            key = f"{ag.report_id}/{ag.run_idx}:{ag.sha256[:8]}"
            if outcome.actual is not None:
                values[key] = outcome.actual
            else:
                errors[key] = outcome.error or "?"
        counts = Counter(values.values())
        consensus, consensus_count = (counts.most_common(1)[0]
                                      if counts else (None, 0))
        rows.append({
            "case": case.name,
            "tags": list(case.tags),
            "reference": expected,
            "consensus": consensus,
            "consensus_count": consensus_count,
            "n_values": len(values),
            "values": values,
            "errors": errors,
        })
    return rows


# --- метрика копии (вызывается из benchmark_report._summarize, #126) --------------


@dataclass(frozen=True)
class RunFineGradeResult:
    """Результат функциональной оценки ОДНОЙ копии (зеркало RunLintResult).

    passed/total имеют смысл только при status == "checked". errors содержит
    пригодные для публичного HTML-отчёта нарушения контракта.
    """

    status: str
    passed: int | None
    total: int | None
    autonomous: bool | None
    errors: tuple[str, ...] = ()


def grade_copy_artifacts(artifacts: Iterable[RunArtifact]) -> RunFineGradeResult:
    """Оценивает копию по собранным артефактам: лучший из её HTML.

    Контракт отказоустойчивости — как у lint_metrics._lint_one: нет HTML → na;
    ЛЮБОЙ сбой (включая отсутствие движка) → unavailable, прогон бенчмарка не
    валится. BaseException не глотается.
    """
    try:
        html_artifacts = [
            a for a in artifacts
            if a.kind == ARTIFACT_KIND_AGENT_FILE
            and a.path.lower().endswith(_HTML_SUFFIXES)
        ]
        if not html_artifacts:
            return RunFineGradeResult(FINE_STATUS_NA, None, None, None, ())
        best: HtmlGrade | None = None
        for artifact in html_artifacts:
            grade = grade_html(bytes(artifact.content))
            if grade.status == GRADE_STATUS_UNAVAILABLE:
                return RunFineGradeResult(
                    FINE_STATUS_UNAVAILABLE, None, None, None,
                    (grade.error or "JS-движок недоступен",),
                )
            rank = (grade.status == GRADE_STATUS_GRADED, grade.passed)
            if best is None or rank > (best.status == GRADE_STATUS_GRADED, best.passed):
                best = grade
        if best is None or best.status != GRADE_STATUS_GRADED:
            errors = []
            if best is not None:
                errors.append(f"Ошибка парсера: {best.error or best.status}")
                errors.extend(
                    f"Нарушение автономности: {item}"
                    for item in best.autonomy_violations
                )
            return RunFineGradeResult(
                FINE_STATUS_PARSE_ERROR, None, None,
                None if best is None else not best.autonomy_violations,
                tuple(errors),
            )
        errors = tuple(
            f"Нарушение автономности: {item}"
            for item in best.autonomy_violations
        )
        return RunFineGradeResult(FINE_STATUS_CHECKED, best.passed, best.total,
                                  not best.autonomy_violations, errors)
    except Exception as exc:
        return RunFineGradeResult(
            FINE_STATUS_UNAVAILABLE, None, None, None,
            (f"Ошибка грейдера: {str(exc)[:200]}",),
        )


def summarize_fine(runs: Iterable[dict]) -> dict:
    """Сводка функциональной оценки по копиям отчёта: счётчики статусов и
    суммарный счёт passed/total по checked-копиям. Копии без оценки
    (неуспешные, fine=None) в сводку не входят — как в summarize_lint."""
    counts = {FINE_STATUS_CHECKED: 0, FINE_STATUS_NA: 0,
              FINE_STATUS_UNAVAILABLE: 0, FINE_STATUS_PARSE_ERROR: 0}
    passed = total = 0
    autonomy_errors = 0
    for run in runs:
        fine = run.get("fine")
        if fine is None:
            continue
        counts[fine.status] += 1
        if fine.autonomous is False:
            autonomy_errors += 1
        if fine.status == FINE_STATUS_CHECKED:
            passed += fine.passed
            total += fine.total
    return {**counts, "autonomy_errors": autonomy_errors,
            "passed": passed, "total": total}

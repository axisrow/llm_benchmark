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

Интерпретации неоднозначностей задания (эталон следует им строго; проверяются
режимом калибровки по консенсусу реальных реализаций):
  I1: округление вверх до десятков (правило 9) применяется к денежным итогам
      шагов: к каждому дневному штрафу и к сумме после пенсионерского ×0.46.
      Ставка после студенческого ×0.77 (19.25) НЕ округляется — это параметр
      расчёта, а не «расчёт».
  I2: +240 за повторное нарушение (правило 5) добавляется к сумме дневных
      штрафов ДО пенсионерского ×0.46 и до правила 10 («до применения всех
      скидок» — скидка применяется к сумме вместе с надбавкой).
  I3: n льготного дня (правило 6) — номер ЗАЧТЁННОГО (рабочего) дня просрочки;
      выходные льготные дни не потребляют.
  I4: у студента «редкая считается обычной» (правило 7) меняет базовую ставку
      на 25, добавляет множитель ×0.77 и задаёт льготный период 8 дней.
  I5: студент+пенсионер (правило 8): сначала базовая ставка обычной книги
      меняет знак (25 → −25), затем применяются студенческий ×0.77 и дневной
      льготный коэффициент, после чего дневной итог округляется. Пенсионерский
      множитель ×0.46 отменяется. Для расчёта дня это
      ceil10(−25 × 0.77 × grace_factor); альтернативная ставка −75 не
      используется. Различающие комбинации с repeat=1 присутствуют в матрице.
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

Эталон считает в точной арифметике (Fraction): float-шум реализаций на границах
округления (напр. 500 × 0.46 = 230.00000000000003 → ceil10 = 240 вместо 230) —
дефект реализации, а не эталона.

Статусы оценки одного HTML:
  graded      — функция найдена, адаптер выбран, счёт passed/total валиден;
  no_function — скрипт исполнен, функция расчёта не найдена;
  no_adapter  — функция есть, но ни один адаптер не дал ни одного числа
                (НЕ то же самое, что graded 0/Y);
  exec_error  — нет встроенных скриптов / движок упал фатально;
  unavailable — ни один JS-движок не установлен (mini-racer / quickjs).

Сигнатуры функций расчёта у моделей разные (объект-запись с произвольными
именами полей, 7/8 позиционных аргументов, массив сырых строк), поэтому вызов
идёт через перебор небольшого множества кандидатов-адаптеров; выбирается
кандидат с максимумом совпадений (benefit of the doubt), его имя фиксируется
в результате.
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

from artifacts import ARTIFACT_KIND_AGENT_FILE, RunArtifact

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
GRADE_STATUS_NO_ADAPTER = "no_adapter"
GRADE_STATUS_EXEC_ERROR = "exec_error"
GRADE_STATUS_UNAVAILABLE = "unavailable"

# Статусы уровня копии — строки совпадают с контрактом lint_metrics (#100/#101),
# чтобы будущая интеграция в _summarize/raw_json была единообразной.
FINE_STATUS_CHECKED = "checked"
FINE_STATUS_NA = "na"
FINE_STATUS_UNAVAILABLE = "unavailable"

_HTML_SUFFIXES = (".html", ".htm")

# Таймаут ОДНОГО eval в движке: защита от while(true) в коде модели. Один eval
# гоняет всю матрицу для одного кандидата-адаптера, так что это же — потолок
# времени кандидата.
_EVAL_TIMEOUT_SEC = 10.0


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
    """Эталонный расчёт штрафа по правилам 1–10 (интерпретации I1–I8).

    Вся арифметика — точная (Fraction), чтобы границы округления не зависели от
    float-представления (см. docstring модуля).
    """
    days = overdue_days(case.control_date, case.actual_date)
    if days == 0:
        return 0  # I6: нет просрочки — нет штрафа (ни +240, ни min 20)
    base_rate = Fraction(BASE_RATE_RARE if case.category else BASE_RATE_REGULAR)
    student_discount = Fraction(1)
    grace = GRACE_RARE if case.category else GRACE_REGULAR
    pensioner_discount = bool(case.pensioner)
    if case.student:
        # I4: редкая считается обычной; ×0.77 применяется отдельно к ставке.
        base_rate = Fraction(BASE_RATE_REGULAR)
        student_discount = STUDENT_RATE_MULT
        grace = GRACE_REGULAR
    if case.student and case.pensioner:
        # I5: сначала −25, затем ×0.77 и дневная льгота; ×0.46 отменяется.
        base_rate = -base_rate
        pensioner_discount = False

    total = Fraction(0)
    for n in range(1, days + 1):
        mult = Fraction(21 + 10 * (n - 1), 100) if n <= grace else Fraction(1)
        daily = base_rate * student_discount * mult
        total += ceil10(daily)  # I1/I5: скидки, затем дневное округление
    if case.repeat:
        total += REPEAT_SURCHARGE  # I2: до суммовых скидок и правила 10
    if pensioner_discount:
        total = Fraction(ceil10(total * PENSIONER_TOTAL_MULT))  # I1
    fine = max(int(total), MIN_FINE)  # I6: безусловно, сначала минимум...
    return min(fine, case.deposit)  # ...затем потолок залогом


# --- тестовая матрица: полный факторный перебор --------------------------------
#
# Пространство оценки — взаимодействия правил, поэтому матрица порождается как
# полное произведение дискретных измерений (категория × студент × пенсионер ×
# повтор × корзина просрочки × корзина залога), а не подбирается руками.
# Все даты фиксированы в июне 2025 (1-е — воскресенье) — матрица детерминирована.

_DEPOSIT_TINY = 15
_DEPOSIT_BIG = 100_000

_BUCKET_KEYS = (
    "on_time",  # возврат в контрольный день
    "early",  # возврат раньше срока (I8)
    "weekend_only",  # просрочка целиком на сб/вс (правило 3)
    "one_day",  # один рабочий день
    "grace_minus_1",  # льготный период: граница снизу
    "grace_exact",  # ровно льготный период
    "grace_plus_1",  # первый день полной ставки
    "far_beyond",  # далеко за льготным (диапазон через ≥2 уикенда)
)


def _d(day: int) -> dt.date:
    return dt.date(2025, 6, day)


def _add_working_days(start: dt.date, count: int) -> dt.date:
    """Дата, до которой от start накапливается ровно count рабочих дней."""
    day, added = start, 0
    while added < count:
        day += dt.timedelta(days=1)
        if day.weekday() < 5:
            added += 1
    return day


def _bucket_dates(key: str, grace: int) -> tuple[dt.date, dt.date]:
    if key == "on_time":
        return _d(10), _d(10)
    if key == "early":
        return _d(10), _d(5)
    if key == "weekend_only":
        return _d(13), _d(15)  # пятница → воскресенье: 0 рабочих дней
    if key == "one_day":
        return _d(2), _d(3)
    offsets = {"grace_minus_1": -1, "grace_exact": 0, "grace_plus_1": 1, "far_beyond": 6}
    return _d(2), _add_working_days(_d(2), grace + offsets[key])


def _span_has_weekend(control: dt.date, actual: dt.date) -> bool:
    day = control + dt.timedelta(days=1)
    while day <= actual:
        if day.weekday() >= 5:
            return True
        day += dt.timedelta(days=1)
    return False


def _case_tags(student: int, pensioner: int, repeat: int,
               bucket: str, dep_key: str,
               control: dt.date, actual: dt.date) -> tuple[str, ...]:
    tags = {"r2", "r4", "r9", "r10"}
    if bucket == "weekend_only" or _span_has_weekend(control, actual):
        tags.add("r3")
    if bucket in ("grace_minus_1", "grace_exact", "grace_plus_1"):
        tags.update({"r6", "r6-boundary"})
    elif bucket in ("one_day", "far_beyond"):
        tags.add("r6")
    if repeat:
        tags.add("r5")
    if student or pensioner:
        tags.add("r7")
    if student and pensioner:
        tags.update({"r8", "ambig-i5"})
    if dep_key == "tiny":
        tags.add("r10-edge")
    if dep_key == "binding":
        tags.add("r10-cap")
    if bucket in ("on_time", "early", "weekend_only"):
        tags.add("ambig-i6")  # нулевая просрочка: применяется ли min 20
    return tuple(sorted(tags))


_CASE_FIO = "Иванов Иван"


def _special_cases() -> list[FineCase]:
    """Ручные особые кейсы, не выражаемые корзинами факторного перебора."""

    def case(name: str, control: dt.date, actual: dt.date, *, category: int = 0,
             deposit: int = _DEPOSIT_BIG, tags: tuple[str, ...] = ()) -> FineCase:
        return FineCase(name=name, fio=_CASE_FIO, control_date=control,
                        actual_date=actual, category=category, deposit=deposit,
                        student=0, pensioner=0, repeat=0,
                        tags=tuple(sorted({"special", "r2", "r9", "r10", *tags})))

    return [
        # возврат в субботу: зачтён только пятничный день
        case("special_return_saturday", _d(12), _d(14), tags=("r3", "r6")),
        # пятница → понедельник: выходные не считаются, 1 день
        case("special_fri_to_mon", _d(13), _d(16), tags=("r3", "r6")),
        # сумма ровно 20 — граница минимума без клампа
        case("special_fine_exactly_20", _d(2), _d(4), tags=("r6",)),
        # кэп ровно равен залогу (редкая, 6 дней: 20+30+40+40+50+80 = 260)
        case("special_cap_exact", _d(2), _add_working_days(_d(2), 6),
             category=1, deposit=260, tags=("r3", "r6", "r6-boundary", "r10-cap")),
        # залог меньше минимума при нулевой просрочке (I6): штрафа нет — 0
        case("special_deposit_below_min_no_overdue", _d(10), _d(10),
             deposit=10, tags=("ambig-i6", "r10-edge")),
    ]


def _build_matrix() -> tuple[FineCase, ...]:
    cases: list[FineCase] = []
    for category in (0, 1):
        grace = GRACE_RARE if category else GRACE_REGULAR
        for student in (0, 1):
            for pensioner in (0, 1):
                for repeat in (0, 1):
                    for bucket in _BUCKET_KEYS:
                        control, actual = _bucket_dates(bucket, grace)

                        def make(name: str, deposit: int,
                                 tags: tuple[str, ...]) -> FineCase:
                            return FineCase(
                                name=name, fio=_CASE_FIO, control_date=control,
                                actual_date=actual, category=category,
                                deposit=deposit, student=student,
                                pensioner=pensioner, repeat=repeat, tags=tags)

                        uncapped = reference_fine(make("", _DEPOSIT_BIG, ()))
                        for dep_key in ("big", "binding", "tiny"):
                            if dep_key == "binding":
                                # «зажимающий» залог осмыслен, только если без
                                # него штраф заметно больше минимума
                                if uncapped < MIN_FINE + 10:
                                    continue
                                deposit = uncapped - 10
                            else:
                                deposit = _DEPOSIT_TINY if dep_key == "tiny" else _DEPOSIT_BIG
                            name = (f"cat{category}_stu{student}_pen{pensioner}"
                                    f"_rep{repeat}_{bucket}_{dep_key}")
                            tags = _case_tags(student, pensioner, repeat,
                                              bucket, dep_key, control, actual)
                            cases.append(make(name, deposit, tags))
    cases.extend(_special_cases())
    return tuple(cases)


TEST_MATRIX: tuple[FineCase, ...] = _build_matrix()


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

    def __init__(self, eval_timeout_sec: float) -> None:
        from py_mini_racer import MiniRacer

        self._ctx = MiniRacer()
        self._timeout = eval_timeout_sec

    def _eval(self, code: str) -> object:
        return self._ctx.eval(code, timeout_sec=self._timeout)


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

                return lambda: _MiniRacerEngine(eval_timeout_sec)
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
  var stub = new Proxy(function () {}, {
    get: function (target, prop) {
      if (prop === Symbol.toPrimitive) return function () { return ""; };
      if (prop === "toString") return function () { return ""; };
      if (prop === "valueOf") return function () { return 0; };
      if (prop === "then") return undefined;
      return stub;
    },
    set: function () { return true; },
    has: function () { return true; },
    deleteProperty: function () { return true; },
    apply: function () { return stub; },
    construct: function () { return stub; }
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
GRADE_HARNESS_JS = """
function __gradePad2(n) { return (n < 10 ? "0" : "") + n; }

function __gradeMakeDate(rep, d) {
  if (rep === "date_local") return new Date(d.y, d.m - 1, d.d);
  if (rep === "date_utc") return new Date(Date.UTC(d.y, d.m - 1, d.d));
  if (rep === "iso") return d.y + "-" + __gradePad2(d.m) + "-" + __gradePad2(d.d);
  if (rep === "dmy") return __gradePad2(d.d) + "." + __gradePad2(d.m) + "." + d.y;
  throw new Error("unknown date rep: " + rep);
}

function __gradeArgs(conv, rep, c) {
  var control = __gradeMakeDate(rep, c.control);
  var actual = __gradeMakeDate(rep, c.actual);
  if (conv === "object") {
    // запись со ВСЕМИ синонимами полей сразу: какое бы имя ни читала
    // реализация, она найдёт значение
    return [{
      name: c.fio, fio: c.fio, fullName: c.fio, readerName: c.fio,
      controlDate: control, dueDate: control, due: control,
      actualDate: actual, returnDate: actual, factDate: actual,
      actualReturnDate: actual,
      category: c.category, bookCategory: c.category, bookType: c.category,
      deposit: c.deposit, depositCost: c.deposit, pledge: c.deposit,
      pledgeValue: c.deposit, collateral: c.deposit,
      student: c.student, isStudent: c.student,
      pensioner: c.pensioner, isPensioner: c.pensioner, retiree: c.pensioner,
      repeat: c.repeat, isRepeat: c.repeat, repeatViolation: c.repeat,
      isRepeatedViolation: c.repeat, hasRepeatViolation: c.repeat,
      repeatedViolation: c.repeat
    }];
  }
  if (conv === "positional7") {
    return [control, actual, c.category, c.deposit, c.student, c.pensioner, c.repeat];
  }
  if (conv === "positional8") {
    return [c.fio, control, actual, c.category, c.deposit, c.student,
            c.pensioner, c.repeat];
  }
  if (conv === "batch") {
    return [[[c.fio, control, actual, c.category, c.deposit, c.student,
              c.pensioner, c.repeat]]];
  }
  throw new Error("unknown convention: " + conv);
}

function __gradeExtract(v, depth) {
  if (typeof v === "number" && isFinite(v)) return { v: v };
  if (typeof v === "string" && v.trim() !== "" && isFinite(Number(v))) {
    return { v: Number(v) };
  }
  if (v && typeof v === "object" && depth < 3) {
    if (typeof v.then === "function") return { e: "async result" };
    if (v.skipped || v.error) {
      return { e: "skipped: " + String(v.reason || v.error) };
    }
    if (Array.isArray(v.results)) {
      if (v.results.length === 1) return __gradeExtract(v.results[0], depth + 1);
      if (v.results.length === 0) return { e: "skipped: empty results" };
    }
    var keys = ["fine", "finalFine", "totalFine", "fineAmount", "total",
                "amount", "sum", "penalty", "result", "value"];
    for (var i = 0; i < keys.length; i++) {
      if (v[keys[i]] !== undefined) {
        var r = __gradeExtract(v[keys[i]], depth + 1);
        if (r.v !== undefined) return r;
      }
    }
  }
  return { e: "non-numeric result" };
}

function __gradeResolve(name) {
  try {
    var fn = (0, eval)(name);
    if (typeof fn === "function") return fn;
  } catch (e) {}
  return null;
}

function __gradeDiscover() {
  var preferred = ["calculateFine", "calcFine"];
  for (var i = 0; i < preferred.length; i++) {
    var fn = __gradeResolve(preferred[i]);
    if (fn) return JSON.stringify({ name: preferred[i], arity: fn.length });
  }
  var props = Object.getOwnPropertyNames(globalThis);
  for (var j = 0; j < props.length; j++) {
    var p = props[j];
    if (!/fine|penalty/i.test(p)) continue;
    if (/overdue|days|round|ceil|render|display|format|show|parse/i.test(p)) continue;
    if (typeof globalThis[p] === "function") {
      return JSON.stringify({ name: p, arity: globalThis[p].length });
    }
  }
  return JSON.stringify(null);
}

function __gradeRun(fnName, adapter, casesJson) {
  var fn = __gradeResolve(fnName);
  var cases = JSON.parse(casesJson);
  var out = [];
  for (var i = 0; i < cases.length; i++) {
    try {
      var v = fn.apply(null, __gradeArgs(adapter.conv, adapter.rep, cases[i]));
      out.push(__gradeExtract(v, 0));
    } catch (e) {
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
    convention: str  # object | positional7 | positional8 | batch
    date_rep: str  # date_local | date_utc | iso | dmy


_DATE_REPS_FULL = ("date_local", "date_utc", "iso", "dmy")
_DATE_REPS_STRINGS = ("dmy", "iso")  # batch — сырые строки, даты только текстом


def _adapters(convention: str, reps: tuple[str, ...]) -> list[AdapterSpec]:
    return [AdapterSpec(f"{convention}+{rep}", convention, rep) for rep in reps]


def candidate_adapters(arity: int) -> list[AdapterSpec]:
    """Множество кандидатов по арности функции расчёта. Неожиданная арность —
    пробуем всё (benefit of the doubt)."""
    obj = _adapters("object", _DATE_REPS_FULL) + _adapters("batch", _DATE_REPS_STRINGS)
    pos7 = _adapters("positional7", _DATE_REPS_FULL)
    pos8 = _adapters("positional8", _DATE_REPS_FULL)
    if arity <= 1:
        return obj
    if arity == 7:
        return pos7
    if arity == 8:
        return pos8
    return obj + pos7 + pos8


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
                       expected: list[int]) -> tuple[ComboOutcome, ...] | None:
    """Список результатов харнесса → ComboOutcome; None при нарушении формата."""
    if not isinstance(raw, list) or len(raw) != len(matrix):
        return None
    outcomes = []
    for case, exp, entry in zip(matrix, expected, raw):
        actual = entry.get("v") if isinstance(entry, dict) else None
        if isinstance(actual, (int, float)) and math.isfinite(actual):
            match = abs(actual - exp) < 1e-6
            error = None
        else:
            actual = None
            match = False
            error_raw = entry.get("e") if isinstance(entry, dict) else None
            error = str(error_raw)[:200] if error_raw else "non-numeric result"
        outcomes.append(ComboOutcome(case_name=case.name, expected=exp,
                                     actual=actual, match=match, error=error))
    return tuple(outcomes)


def grade_html(content: bytes, *, matrix: tuple[FineCase, ...] = TEST_MATRIX,
               prefer_engine: str = "auto",
               eval_timeout_sec: float = _EVAL_TIMEOUT_SEC) -> HtmlGrade:
    """Оценивает один HTML-артефакт: формула («X из Y») + автономность.

    Исполняет встроенные скрипты целиком (функции расчёта зовут глобальные
    хелперы), затем перебирает кандидатов-адаптеров и берёт лучший счёт.
    """
    html = content.decode("utf-8", errors="replace")
    autonomy = check_autonomy(html)

    factory = create_engine(prefer_engine, eval_timeout_sec)
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
        for idx, script in enumerate(scripts, start=1):
            try:
                engine.run(script)
            except Exception as exc:
                # function-декларации хойстятся и переживают падение top-level —
                # фиксируем предупреждение и продолжаем оценку
                note = f"скрипт #{idx}: {str(exc)[:200]}"
                exec_warning = f"{exec_warning}; {note}" if exec_warning else note
        engine.run(GRADE_HARNESS_JS)
        found = engine.eval_json("__gradeDiscover()")
    except Exception as exc:
        return _grade_stub(GRADE_STATUS_EXEC_ERROR, matrix, autonomy,
                           exec_warning=exec_warning, error=str(exc)[:300])

    if not isinstance(found, dict) or not found.get("name"):
        return _grade_stub(GRADE_STATUS_NO_FUNCTION, matrix, autonomy,
                           engine=engine.name, exec_warning=exec_warning,
                           error="функция расчёта не найдена")

    fn_name = str(found["name"])
    arity = int(found.get("arity") or 0)
    expected = [reference_fine(case) for case in matrix]
    cases_json = json.dumps([_case_payload(case) for case in matrix],
                            ensure_ascii=False)

    best: tuple[tuple[int, int], AdapterSpec, tuple[ComboOutcome, ...]] | None = None
    for adapter in candidate_adapters(arity):
        call = (f"__gradeRun({json.dumps(fn_name)}, "
                f"{json.dumps({'conv': adapter.convention, 'rep': adapter.date_rep})}, "
                f"{json.dumps(cases_json)})")
        try:
            raw = engine.eval_json(call)
        except Exception:
            # таймаут/крах этого кандидата — не приговор остальным
            continue
        outcomes = _outcomes_from_raw(raw, matrix, expected)
        if outcomes is None:
            continue
        numeric = sum(1 for o in outcomes if o.actual is not None)
        if numeric == 0:
            continue
        passed = sum(1 for o in outcomes if o.match)
        rank = (passed, numeric)
        if best is None or rank > best[0]:
            best = (rank, adapter, outcomes)

    if best is None:
        return _grade_stub(GRADE_STATUS_NO_ADAPTER, matrix, autonomy,
                           engine=engine.name, function_name=fn_name,
                           fn_arity=arity, exec_warning=exec_warning,
                           error="ни один адаптер не дал числового результата")

    (passed, _), adapter, outcomes = best
    return HtmlGrade(status=GRADE_STATUS_GRADED, function_name=fn_name,
                     fn_arity=arity, adapter=adapter.name, passed=passed,
                     total=len(matrix), outcomes=outcomes,
                     autonomy_violations=autonomy, engine=engine.name,
                     exec_warning=exec_warning, error=None)


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


# --- задел под метрику копии (benchmark_report._summarize, пока не подключено) ----


@dataclass(frozen=True)
class RunFineGradeResult:
    """Результат функциональной оценки ОДНОЙ копии (зеркало RunLintResult).

    passed/total/autonomous имеют смысл только при status == "checked".
    """

    status: str
    passed: int | None
    total: int | None
    autonomous: bool | None


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
            return RunFineGradeResult(FINE_STATUS_NA, None, None, None)
        best: HtmlGrade | None = None
        for artifact in html_artifacts:
            grade = grade_html(bytes(artifact.content))
            if grade.status == GRADE_STATUS_UNAVAILABLE:
                return RunFineGradeResult(FINE_STATUS_UNAVAILABLE, None, None, None)
            rank = (grade.status == GRADE_STATUS_GRADED, grade.passed)
            if best is None or rank > (best.status == GRADE_STATUS_GRADED, best.passed):
                best = grade
        if best is None or best.status != GRADE_STATUS_GRADED:
            return RunFineGradeResult(FINE_STATUS_UNAVAILABLE, None, None, None)
        return RunFineGradeResult(FINE_STATUS_CHECKED, best.passed, best.total,
                                  not best.autonomy_violations)
    except Exception:
        return RunFineGradeResult(FINE_STATUS_UNAVAILABLE, None, None, None)

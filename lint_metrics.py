"""Метрика качества кода: число diagnostics линтеров по языкам (#100 → #101).

Самостоятельный объективный тест результата копии. Считает diagnostics линтеров в
СОБРАННЫХ артефактах конкретной успешно завершившейся копии (code==0). Не
затрагивает весь репозиторий и не лезет в старые data/result.

#100 добавила первый линтер — Ruff для Python. #101 обобщает метрику до РЕЕСТРА
линтеров и расширяет её на не-Python языки, сохранив единый формат результата,
хранение, агрегацию и отображение. Первый набор не-Python инструментов утверждён
ПО ФАКТУ накопленных в data/main.db артефактов (требование #101):
  - .py        → Ruff  (`ruff check --isolated --no-cache --output-format=json`);
  - .html/.htm → HTML Tidy (`tidy`): реальные веб-страницы (проект library_fine);
  - .json      → jq: конфиги (проект stock_downloader).
Отдельных .js/.ts/.css файлов в базе на момент #101 нет — они вне первого набора
(follow-up). Расширить набор = добавить ``LinterSpec`` в ``LINTERS``.

Единый контракт (наследуется от #100 для каждого линтера отдельно):
  - запуск БЕЗ автоисправления и без проектной конфигурации — линтеры только
    диагностируют, файлы модели не меняют;
  - статусы:
      * ``checked``     — есть подходящие файлы, инструмент отработал,
                          ``errors`` = число diagnostics;
      * ``na``          — подходящих файлов в копии нет (это НЕ ноль);
      * ``unavailable`` — инструмент отсутствует/упал/дал некорректный вывод;
                          бенчмарк при этом продолжает работу.

Раздельные счётчики: копия может нести файлы нескольких языков сразу
(``.py`` + ``.html`` + ``.json``). ``lint_copy_artifacts`` возвращает
``dict[имя_линтера → RunLintResult]`` — diagnostics разных инструментов НЕ
смешиваются, имя линтера всегда сохраняется как ключ. Запускаются ТОЛЬКО линтеры,
для которых в копии есть подходящие артефакты.

Отказоустойчивость и изоляция (наследуются от #100 для каждого линтера):
  - контент пишется во ВРЕМЕННУЮ папку, инструмент зовётся с явным списком путей —
    он не видит ни репозиторий, ни соседние копии, ни старые data/result;
  - ВЕСЬ lifecycle одного линтера обёрнут в границу: сбой ФС (ENOSPC, нет tmp,
    права), timeout или падение subprocess → ``unavailable`` ТОЛЬКО для этого
    линтера. Отсутствие/техошибка одного инструмента не валит бенчмарк и НЕ
    скрывает результаты остальных.

Логически метрика независима от cleanup (#99); физически она запускается ДО
``cleanup_collected_artifacts`` (линтерам нужны исходники), но любой её сбой
гасится в ``unavailable`` и не валит прогон.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from artifacts import ARTIFACT_KIND_AGENT_FILE, RunArtifact


# Таймаут одного вызова линтера: ограничивает зависший процесс, чтобы метрика не
# висела на одной копии. Метрика не критична для прогона — timeout → unavailable.
_LINT_TIMEOUT_SEC = 60

LINT_STATUS_CHECKED = "checked"
LINT_STATUS_NA = "na"
LINT_STATUS_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RunLintResult:
    """Результат ОДНОГО линтера по ОДНОЙ копии.

    ``errors`` имеет смысл только при status == "checked"; в остальных случаях
    это ``None`` (N/A и unavailable — не числа, а отсутствие оценки).
    """

    status: str
    errors: int | None


@dataclass(frozen=True)
class LinterSpec:
    """Описание одного линтера в реестре.

    ``name``       — стабильное имя инструмента (ключ в результатах и raw_json);
    ``suffixes``   — расширения артефактов (нижний регистр, с точкой), которые
                     инструмент анализирует;
    ``binary``     — имя executable в PATH (проверяется ``shutil.which``);
    ``run``        — функция (binary_path, staged_paths) → число diagnostics.
                     Кидает исключение при техническом сбое (его ловит граница и
                     переводит в ``unavailable``).
    ``per_file``   — если True, инструмент зовётся отдельно на каждый файл, а
                     diagnostics суммируются (нужно для jq: он останавливается на
                     первой ошибке при списке файлов).
    """

    name: str
    suffixes: tuple[str, ...]
    binary: str
    run: Callable[[str, list[Path]], int]
    per_file: bool = False


# --- парсеры вывода конкретных инструментов -----------------------------------


# Ruff: --isolated игнорирует любую проектную конфигурацию (pyproject/ruff.toml),
# --no-cache — без кэша на диск, --output-format=json — машинный вывод. Без --fix:
# никаких автоисправлений, только диагностика. Каждая diagnostic из JSON = ошибка.
_RUFF_BASE_ARGS = ("check", "--isolated", "--no-cache", "--output-format=json")


def _run_ruff(binary: str, paths: list[Path]) -> int:
    """Гоняет Ruff по явному списку .py и возвращает число diagnostics."""
    cmd = [binary, *_RUFF_BASE_ARGS, *[str(p) for p in paths]]
    completed = subprocess.run(  # noqa: S603 — cmd собран из литералов + путей
        cmd, capture_output=True, check=False, timeout=_LINT_TIMEOUT_SEC,
    )
    # exit=0 → чисто ([]), exit=1 → есть diagnostics ([...]) — оба дают валидный
    # JSON. Любой другой код или не-JSON → техсбой (ValueError → unavailable).
    diagnostics = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    if not isinstance(diagnostics, list):
        raise ValueError("ruff вернул не JSON-массив")
    return len(diagnostics)


# tidy печатает построчно `line N column M - Error|Warning: <текст>` в stderr.
# Формат стабилен между версиями tidy (историч. Apple 2006 и HTACG 5.x). Каждая
# такая строка = одна diagnostic; итоговая сводка ("Tidy found N warnings") и Info
# не считаются. -q тише, -e показывает только сообщения (не переписывает HTML).
# --input-encoding=utf8 фиксирует кодировку текстовых артефактов: старый Apple
# Tidy иначе трактует UTF-8 кириллицу как однобайтную и создаёт сотни ложных
# invalid-character diagnostics. Длинная форма работает и в HTACG Tidy.
# --show-errors снимает ДЕФОЛТНЫЙ лимит tidy в 6 показанных ошибок (иначе на файле
# с 10 неизвестными тегами мы считали бы 17 вместо 31 — не все diagnostics видны).
# --gnu-emacs=no форсирует парсимый построчный формат (emacs-формат ":N:M: Error:"
# наш regex не матчит и даст 0; флаг явно отключает его, если внешняя конфигурация
# его включила). Чистый env (без HTML_TIDY/языка) исключает подхват чужого конфига.
_TIDY_BASE_ARGS = (
    "-q", "-e", "--input-encoding", "utf8",
    "--show-errors", "1000", "--gnu-emacs", "no",
)
_TIDY_DIAG_RE = re.compile(r"^line \d+ column \d+ - (Error|Warning):", re.MULTILINE)


def _clean_lint_env() -> dict[str, str]:
    """Окружение для линтеров: чистое, без прокси- и конфиг-переменных.

    Убираем HTTP(S)_PROXY/ALL_PROXY (линтеры локальные, прокси ломает их), и
    инструмент-специфичные конфиг-переменные (HTML_TIDY у tidy может включить
    emacs-формат/сменить язык — тогда наш парсер не опознает diagnostics).
    """
    env = {k: v for k, v in os.environ.items()
           if not k.lower().endswith("_proxy")
           and k not in ("HTML_TIDY", "TIDY")}
    return env


def _run_tidy(binary: str, paths: list[Path]) -> int:
    """Гоняет tidy по .html/.htm и считает строки-diagnostics в stderr.

    tidy на невалидном HTML выходит ненулевым кодом и печатает сообщения в stderr;
    на чистом — код 0 и пустой stderr. Мы НЕ полагаемся на код (он различается
    между версиями и не отличает warnings от ошибок), а считаем строки формата
    diagnostic — это устойчивый контракт. Если tidy вышел ненулём, но не дал ни
    одной парсимой diagnostic-строки (нестандартный формат/локаль/упал), это
    техсбой — кидаем исключение, граница переведёт его в ``unavailable``, а не в
    ложный ``checked=0``.
    """
    cmd = [binary, *_TIDY_BASE_ARGS, *[str(p) for p in paths]]
    completed = subprocess.run(  # noqa: S603 — cmd собран из литералов + путей
        cmd, capture_output=True, check=False, timeout=_LINT_TIMEOUT_SEC,
        env=_clean_lint_env(),
    )
    stderr = completed.stderr.decode("utf-8", errors="replace")
    count = len(_TIDY_DIAG_RE.findall(stderr))
    if count == 0 and completed.returncode != 0:
        # Ненулевой exit без парсимых diagnostics — вывод нестандартный (emacs-
        # формат, локаль, краш). Не маскируем это под «чистый файл».
        raise RuntimeError(f"tidy вышел с кодом {completed.returncode}, но diagnostics "
                           f"не распознаны: {stderr.strip()[:200]}")
    return count


# jq exit-коды: 0 — вход распарсен; 2 — ошибка использования/системная; 3 — ошибка
# компиляции фильтра; 5 — ошибка парсинга JSON (то, что мы считаем diagnostic).
_JQ_INVALID_JSON_CODES = frozenset({1, 4, 5})


def _run_jq(binary: str, paths: list[Path]) -> int:
    """Проверяет валидность ОДНОГО .json через jq: ровно один JSON-документ = 0,
    иначе 1 diagnostic.

    jq вызывается per_file (см. ``LinterSpec.per_file``): на списке файлов он
    останавливается на первой ошибке, поэтому реестр зовёт нас по одному файлу, а
    diagnostics суммируются снаружи. ``-s 'length==1'`` (slurp + проверка числа
    значений) требует РОВНО один документ: пустой файл, пробелы и мульти-документ
    ('{}{}') jq как поток парсит успешно, но это не один JSON — теперь это 1
    diagnostic. Файлы модели не меняются.

    Exit-коды разделяем: 0 → чисто; парсинг JSON (1/4/5) → 1 diagnostic;
    использование/система (2) и компиляция фильтра (3) → техсбой (исключение →
    граница переведёт в ``unavailable``), НЕ JSON-ошибка.
    """
    # paths здесь всегда ровно один элемент (per_file=True).
    cmd = [binary, "-e", "-s", "length==1", *[str(p) for p in paths]]
    completed = subprocess.run(  # noqa: S603 — cmd собран из литералов + путей
        cmd, capture_output=True, check=False, timeout=_LINT_TIMEOUT_SEC,
        env=_clean_lint_env(),
    )
    code = completed.returncode
    if code == 0:
        return 0
    if code in _JQ_INVALID_JSON_CODES:
        return 1
    # 2 (usage/system) или 3 (filter compile) — техсбой, не невалидный JSON.
    raise RuntimeError(f"jq завершился с кодом {code} (техошибка, не парсинг)")


# node --check: статический синтаксис-чек JS (не запускает код). Ловит SyntaxError
# в сгенерированном моделью JS — самая частая болезнь library_fine (битый `<script>`
# ломает всю функцию расчёта). node v25+ печатает `SyntaxError: ...` в stderr при
# exit 1; exit 0 = чисто. ``--check`` безопасен: не исполняет, только парсит.
# Для .html/.htm экстрактим встроенные <script> (исполняемые) — отдельные .js
# у моделей пока не встречаются, но контракт покрыт.
# Пропускаем: внешние (src=) и НЕ-исполняемые типы (application/json,
# application/ld+json, text/template, text/html) — иначе node --check флагает их
# содержимое как SyntaxError (false positive: JSON-LD/шаблоны — не JS).
_JS_SCRIPT_RE = re.compile(
    r"<script(?![^>]*\bsrc=)"           # не внешний (без src=)
    r"(?![^>]*\btype\s*=\s*['\"]?"      # и НЕ не-исполняемый type:
    r"(?:application/(?:json|ld\+json)|text/(?:template|html)))"
    r"[^>]*>(.*?)</script>",
    re.IGNORECASE | re.DOTALL)


def _extract_inline_js(html_text: str) -> str:
    """Склеивает встроенные ИСПОЛНЯЕМЫЕ <script>-блоки HTML.

    Берём: ``<script>`` без атрибута type (дефолт = JS), либо с явным
    исполняемым типом (text/javascript, application/javascript, module).
    Пропускаем: внешние (``src=``) и не-исполняемые (application/json,
    application/ld+json, text/template, text/html) — это данные/шаблоны,
    node --check флагал бы их как SyntaxError.
    """
    blocks = _JS_SCRIPT_RE.findall(html_text)
    return "\n".join(blocks)


def _run_js(binary: str, paths: list[Path]) -> int:
    """Гоняет ``node --check`` по .js напрямую и по извлечённому из .html <script>.

    Диагностика: 0 на чистом файле; число SyntaxError-блоков на битом. ``node
    --check`` печатает один SyntaxError и выходит (exit 1) при первой синтаксической
    ошибке — несколько ошибок в одном файле дают 1 diagnostic (контракт аналогичен
    jq: «1 невалидный файл = 1 diagnostic»). Нестандартный вывод/exit → техсбой
    (исключение → unavailable), не ложный ``checked=0``.
    """
    total = 0
    with tempfile.TemporaryDirectory(prefix="lint-js-") as tmp:
        tmp_path = Path(tmp)
        check_files: list[Path] = []
        for path in paths:
            text = path.read_text(encoding="utf-8", errors="replace")
            if path.suffix.lower() in (".html", ".htm"):
                js = _extract_inline_js(text)
                if not js.strip():
                    continue  # нет встроенного JS — не диагностим (мимо счётчика)
                # Стейджим по ОТНОСИТЕЛЬНОМУ пути артефакта (как _stage_and_run),
                # меняя суффикс на .js — иначе два same-stem .html (напр.
                # sub1/index.html + sub2/index.html) перезапишут друг друга в
                # tmp/stem.js и дадут неверный счёт (collision).
                rel = path.relative_to(path.anchor) if path.is_absolute() else path
                staged = (tmp_path / rel).with_suffix(".js")
                staged.parent.mkdir(parents=True, exist_ok=True)
                staged.write_text(js, encoding="utf-8")
                check_files.append(staged)
            else:  # .js напрямую
                check_files.append(path)
        for check in check_files:
            completed = subprocess.run(  # noqa: S603 — cmd собран из литералов
                [binary, "--check", str(check)],
                capture_output=True, check=False, timeout=_LINT_TIMEOUT_SEC,
                env=_clean_lint_env(),
            )
            code = completed.returncode
            if code == 0:
                continue
            stderr = completed.stderr.decode("utf-8", errors="replace")
            if code == 1 and "SyntaxError" in stderr:
                total += 1
                continue
            # Нестандартный exit/вывод — техсбой, не маскируем под «чисто».
            raise RuntimeError(f"node --check вышел с кодом {code}, вывод не опознан: "
                               f"{stderr.strip()[:200]}")
    return total


# --- реестр линтеров ----------------------------------------------------------
# Порядок стабилен: Ruff первым (историч. #100), не-Python по факту накопленного.

LINTERS: dict[str, LinterSpec] = {
    "ruff": LinterSpec("ruff", (".py",), "ruff", _run_ruff),
    "tidy": LinterSpec("tidy", (".html", ".htm"), "tidy", _run_tidy),
    "jq": LinterSpec("jq", (".json",), "jq", _run_jq, per_file=True),
    # JS: .html/.htm (экстракт встроенного <script>) + .js напрямую. node --check —
    # статический синтаксис-чекер (не исполняет). Ловит SyntaxError в битом JS
    # библиотечных калькуляторов; дублирует tidy по суффиксу .html умышленно
    # (tidy=HTML-валидность, js=JS-синтаксис — разные аспекты одного артефакта).
    "js": LinterSpec("js", (".html", ".htm", ".js"), "node", _run_js),
}


def _artifacts_for(spec: LinterSpec, artifacts: list[RunArtifact]) -> list[RunArtifact]:
    """Агентские файлы копии с подходящим суффиксом (логи и чужие типы — мимо)."""
    return [
        a for a in artifacts
        if a.kind == ARTIFACT_KIND_AGENT_FILE
        and a.path.lower().endswith(spec.suffixes)
    ]


def _stage_and_run(spec: LinterSpec, matched: list[RunArtifact]) -> RunLintResult:
    """Выгружает контент подходящих артефактов во временную папку и гоняет линтер.

    По content (байты из артефакта), а не по source_path: к моменту метрики путь
    копии может быть уже несвежим (напр. при backfill из БД), а контент —
    единственный источник правды. Относительный путь артефакта сохраняется, чтобы
    линтер не схлопнул одноимённые файлы из разных подпапок.

    Вся работа с ФС и subprocess выполняется внутри одной границы — см.
    ``_lint_one`` (он ловит всё, что может здесь вылететь: ENOSPC, нет tmp, права,
    timeout, падение subprocess).
    """
    binary = shutil.which(spec.binary)
    if binary is None:
        return RunLintResult(LINT_STATUS_UNAVAILABLE, None)
    with tempfile.TemporaryDirectory(prefix=f"lint-{spec.name}-") as tmp:
        # root resolve'им ОДИН раз: tempdir на macOS живёт под /var → /private/var
        # (symlink). resolve(dest) даёт /private/...; сравнение с не-resolve'ённым
        # root ложно отвергало бы даже нормальные пути. resolve'ённый корень — база
        # для containment-проверки каждого destination.
        root = Path(tmp).resolve()
        staged: list[Path] = []
        for artifact in matched:
            dest = root / artifact.path
            # Сдерживание пути (ревью Codex cycle 2 / PR #154): root / "/abs"
            # отбрасывает root, root / "../x" убегает из tmp → write_bytes мог бы
            # перезаписать произвольный файл хоста. Артефактные пути приходят из БД
            # (schema их не ограничивает), а backfill_lint массово гоняет этот путь
            # по всем отчётам — поэтому проверяем, что resolve'd destination
            # остаётся ВНУТРИ resolve'd корня. Битый путь (abs/..) → пропускаем
            # артефакт, не роняя весь линтер (контракт отказоустойчивости #100/#101:
            # один артефакт не валит метрику копии).
            try:
                contained = dest.resolve().is_relative_to(root)
            except OSError:
                contained = False  # битый symlink/путь — безопасно skip
            if not contained:
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(bytes(artifact.content))
            staged.append(dest)
        if spec.per_file:
            # jq останавливается на первой ошибке при списке файлов — зовём по
            # одному и суммируем diagnostics, чтобы битые файлы не маскировали
            # друг друга.
            total = sum(spec.run(binary, [path]) for path in staged)
        else:
            total = spec.run(binary, staged)
        return RunLintResult(LINT_STATUS_CHECKED, total)


def _lint_one(spec: LinterSpec, artifacts: list[RunArtifact]) -> RunLintResult:
    """Прогоняет ОДИН линтер по копии.

    В копии нет подходящих файлов → ``na`` (это НЕ ноль и НЕ пропуск): так
    счётчик ``na`` каждого инструмента корректен, и для ruff сохраняется
    поведение #100 (``lint_copy_py_artifacts([]) == na``), в т.ч. когда у копии
    есть, напр., только .html. ``na`` требует, чтобы линтер присутствовал в
    результате копии — поэтому он запускается «логически» (без executable).

    Отказоустойчивость (контракт #100/#101, для каждого линтера отдельно): ВЕСЬ
    lifecycle staging+инструмент обёрнут в границу — сбой ФС, timeout, падение
    subprocess или некорректный вывод переводятся в ``unavailable`` ТОЛЬКО для
    этого линтера. Иначе исключение вылетело бы из ``_summarize`` ДО
    ``save_report`` и потеряло бы отчёт законченного прогона, либо техсбой одного
    инструмента скрыл бы результаты остальных.
    """
    matched = _artifacts_for(spec, artifacts)
    if not matched:
        return RunLintResult(LINT_STATUS_NA, None)
    try:
        return _stage_and_run(spec, matched)
    except Exception:
        # Контракт: ANY сбой линтера гасится в unavailable и не валит прогон.
        # Ловим широкий Exception, а не узкий перечень: staging/cleanup/subprocess
        # могут поднять не-OSError (TimeoutExpired, RuntimeError от
        # TemporaryDirectory.__exit__, RecursionError при глубоком пути). Узкий
        # except пропустил бы их. BaseException (KeyboardInterrupt/SystemExit) НЕ
        # глотаем — это сигнал остановки прогона.
        return RunLintResult(LINT_STATUS_UNAVAILABLE, None)


def lint_copy_artifacts(artifacts: Iterable[RunArtifact]) -> dict[str, RunLintResult]:
    """Гоняет ВСЕ линтеры реестра по собранным артефактам одной копии.

    Возвращает ``{имя_линтера → RunLintResult}`` для КАЖДОГО инструмента реестра:
    ``checked`` (есть файлы, отработал), ``na`` (нет подходящих файлов), либо
    ``unavailable`` (сбой/отсутствие). Так счётчики ``na``/``unavailable`` верны
    по всем инструментам, и для ruff сохраняется поведение #100 (включая
    ``result['lint'] == na`` для копии без .py). Diagnostics разных инструментов
    не смешиваются: каждый линтер — своя запись; сбой одного не влияет на другие.
    """
    materialized = list(artifacts)
    return {name: _lint_one(spec, materialized) for name, spec in LINTERS.items()}


def lint_copy_py_artifacts(artifacts: Iterable[RunArtifact]) -> RunLintResult:
    """Ruff-метрика одной копии (историч. точка входа #100).

    Тонкая обёртка над реестром: результат линтера ``ruff`` — ``checked``,
    ``unavailable`` или ``na`` (нет .py-артефактов). Сохранена для обратной
    совместимости с кодом и тестами #100; новый код использует
    ``lint_copy_artifacts``.
    """
    return _lint_one(LINTERS["ruff"], list(artifacts))


# --- агрегация ----------------------------------------------------------------


def _as_lint_result(value: object) -> RunLintResult | None:
    """Нормализует lint-значение: RunLintResult, dict {'status','errors'} из
    raw_json, либо None (нет оценки)."""
    if isinstance(value, RunLintResult):
        return value
    if isinstance(value, dict) and "status" in value:
        return RunLintResult(value["status"], value.get("errors"))
    return None


def _empty_linter_summary() -> dict:
    return {"checked": 0, "na": 0, "unavailable": 0, "total_errors": 0}


def _accumulate(target: dict, code: object, lint: RunLintResult) -> None:
    """Складывает результат одного линтера одной копии в накопитель инструмента.

    В average (числитель/знаменатель) идут ТОЛЬКО успешно завершившиеся (code==0)
    и фактически проверенные (checked) копии: провальная копия не должна влиять на
    среднее качество, даже если у неё случайно есть lint. na/unavailable считаются
    по всем копиям с lint-оценкой, но в среднее не идут.
    """
    if lint.status == LINT_STATUS_CHECKED:
        if code == 0:
            target["checked"] += 1
            target["total_errors"] += int(lint.errors or 0)
    elif lint.status == LINT_STATUS_NA:
        target["na"] += 1
    elif lint.status == LINT_STATUS_UNAVAILABLE:
        target["unavailable"] += 1


def _finalize_avg(summary: dict) -> dict:
    summary["avg_errors"] = (
        round(summary["total_errors"] / summary["checked"], 2)
        if summary["checked"] else None
    )
    return summary


def summarize_linters(runs: Iterable[dict]) -> dict[str, dict]:
    """Сводка метрики по КАЖДОМУ инструменту отдельно (#101).

    ``runs`` — строки вида {'index','code','linters': {имя → RunLintResult|dict}}.
    Возвращает ``{имя_линтера → сводка}`` с раздельными counters — diagnostics
    разных инструментов не смешиваются. Инструмент попадает в сводку, только если
    встретился хотя бы в одной копии.
    """
    summaries: dict[str, dict] = {}
    for run in runs:
        code = run.get("code")
        linters = run.get("linters")
        if not isinstance(linters, dict):
            continue
        for name, raw in linters.items():
            lint = _as_lint_result(raw)
            if lint is None:
                continue
            summary = summaries.setdefault(name, _empty_linter_summary())
            _accumulate(summary, code, lint)
    return {name: _finalize_avg(summary) for name, summary in summaries.items()}


def summarize_lint(runs: Iterable[dict]) -> dict:
    """Ruff-сводка по копиям одного отчёта (историч. точка входа #100).

    В агрегат идут ТОЛЬКО успешно завершившиеся (``code == 0``) и фактически
    проверенные (``checked``) копии: среднее число ошибок = сумма ошибок checked /
    число checked. na/unavailable и неуспешные копии не входят ни в числитель, ни
    в знаменатель. Если checked нет — ``avg_errors`` = None (нечего усреднять).

    Читает Ruff-результат из ``run['lint']`` (RunLintResult|dict|None) — форма
    #100 — либо из ``run['linters']['ruff']`` (форма #101). Сохранена, чтобы
    ``ruff_summary`` в raw_json/индексе оставалась байт-в-байт воспроизводимой.
    """
    summary = _empty_linter_summary()
    for run in runs:
        code = run.get("code")
        lint = _as_lint_result(run.get("lint"))
        if lint is None:
            linters = run.get("linters")
            if isinstance(linters, dict):
                lint = _as_lint_result(linters.get("ruff"))
        if lint is None:
            continue
        _accumulate(summary, code, lint)
    return _finalize_avg(summary)

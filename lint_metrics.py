"""Метрика качества Python-кода: количество diagnostics Ruff (#100).

Самостоятельный объективный тест результата копии. Считает diagnostics Ruff в
СОБРАННЫХ .py-артефактах конкретной успешно завершившейся копии (code==0). Не
затрагивает весь репозиторий и не лезет в старые data/result.

Контракт (#100, первая версия — только Python + Ruff):
  - запуск без автоисправления и без проектной конфигурации:
    `ruff check --isolated --no-cache --output-format=json`;
  - каждая diagnostic из JSON = одна ошибка, независимо от кода правила;
  - статусы:
      * ``checked``     — есть .py, Ruff отработал, ``errors`` = число diagnostics;
      * ``na``          — .py-артефактов в копии нет (это НЕ ноль);
      * ``unavailable`` — Ruff отсутствует/упал без корректного JSON; бенчмарк
                          при этом продолжает работу.

Логически и по обработке ошибок метрика полностью независима от cleanup (#99):
физически она запускается ДО ``cleanup_collected_artifacts`` (Ruff нужны исходники),
но любой её сбой гасится в ``unavailable`` и не валит прогон.
"""

import json
import shutil
import subprocess
import tempfile
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from artifacts import ARTIFACT_KIND_AGENT_FILE, RunArtifact


# Суффикс артефактов, которые Ruff анализирует. Только Python в первой версии;
# JS/TS/HTML/CSS — отдельный follow-up (#101).
_PY_SUFFIX = ".py"
# Бинарник Ruff; ищем в PATH единожды (имя стабильно, кэш не нужен).
_RUFF_BINARY = "ruff"
# Аргументы Ruff: --isolated игнорирует любую проектную конфигурацию (pyproject/
# ruff.toml), --no-cache — без кэша на диск, --output-format=json — машинный вывод.
# Без --fix: никаких автоисправлений, только диагностика.
_RUFF_BASE_ARGS = ("--isolated", "--no-cache", "--output-format=json")

LINT_STATUS_CHECKED = "checked"
LINT_STATUS_NA = "na"
LINT_STATUS_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class RunLintResult:
    """Результат метрики одной копии.

    ``errors`` имеет смысл только при status == "checked"; в остальных случаях
    это ``None`` (N/A и unavailable — не числа, а отсутствие оценки).
    """

    status: str
    errors: int | None


def _is_py_artifact(artifact: RunArtifact) -> bool:
    """Только .py-агентские файлы. Логи (kind=='log') и не-.py — мимо."""
    return artifact.kind == ARTIFACT_KIND_AGENT_FILE and artifact.path.lower().endswith(_PY_SUFFIX)


def _run_ruff(file_paths: list[Path]) -> RunLintResult:
    """Гоняет Ruff по явному списку путей и считает diagnostics.

    На любой технической ошибке (нет бинарника, subprocess упал, stdout — не JSON)
    возвращает ``unavailable``: бенчмарк не должен падать из-за метрики.
    """
    if shutil.which(_RUFF_BINARY) is None:
        return RunLintResult(LINT_STATUS_UNAVAILABLE, None)
    cmd = [_RUFF_BINARY, "check", *_RUFF_BASE_ARGS, *[str(p) for p in file_paths]]
    try:
        completed = subprocess.run(  # noqa: S603 — cmd собран из литералов + путей
            cmd,
            capture_output=True,
            check=False,
        )
    except OSError:
        # FileNotFoundError/PermissionError и пр. — Ruff формально есть, но упал.
        return RunLintResult(LINT_STATUS_UNAVAILABLE, None)
    # exit=0 → чисто ([]), exit=1 → есть diagnostics ([...]) — оба дают валидный
    # JSON. Любой другой код или не-JSON → технический сбой.
    try:
        diagnostics = json.loads(completed.stdout.decode("utf-8", errors="replace"))
    except (ValueError, UnicodeDecodeError):
        return RunLintResult(LINT_STATUS_UNAVAILABLE, None)
    if not isinstance(diagnostics, list):
        return RunLintResult(LINT_STATUS_UNAVAILABLE, None)
    return RunLintResult(LINT_STATUS_CHECKED, len(diagnostics))


def lint_copy_py_artifacts(artifacts: Iterable[RunArtifact]) -> RunLintResult:
    """Считает diagnostics Ruff в собранных .py-артефактах одной копии.

    Изоляция (#100, «запуск не анализирует посторонние .py»): .py-контент пишется
    во ВРЕМЕННУЮ папку, и Ruff зовётся с явным списком путей в ней — он не видит
    ни репозиторий, ни соседние копии, ни старые data/result. По content (байты из
    артефакта), а не по source_path: к моменту метрики путь копии может быть уже
    несвежим (напр. при backfill из БД), а контент — единственный источник правды.

    ``artifacts`` — RunArtifact из artifacts.py (структурный доступ по атрибутам).
    """
    py_artifacts = [a for a in artifacts if _is_py_artifact(a)]
    if not py_artifacts:
        return RunLintResult(LINT_STATUS_NA, None)

    with tempfile.TemporaryDirectory(prefix="ruff-metric-") as tmp:
        root = Path(tmp)
        staged: list[Path] = []
        for artifact in py_artifacts:
            # Сохраняем относительный путь (вложенные папки), чтобы Ruff не схлопнул
            # одноимённые файлы из разных подпапок в один. path — posix-относительный.
            dest = root / artifact.path
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(bytes(artifact.content))
            staged.append(dest)
        return _run_ruff(staged)


def _run_lint_value(run: dict) -> RunLintResult | None:
    """Достаёт lint-результат из строки копии: либо готовый RunLintResult, либо
    dict {'status','errors'} из raw_json, либо None (нет оценки)."""
    lint = run.get("lint")
    if isinstance(lint, RunLintResult):
        return lint
    if isinstance(lint, dict) and "status" in lint:
        return RunLintResult(lint["status"], lint.get("errors"))
    return None


def summarize_lint(runs: Iterable[dict]) -> dict:
    """Сводка метрики по копиям одного отчёта.

    В агрегат идут ТОЛЬКО успешно завершившиеся (``code == 0``) и фактически
    проверенные (``checked``) копии: среднее число ошибок = сумма ошибок checked /
    число checked. na/unavailable и неуспешные копии не входят ни в числитель, ни
    в знаменатель. Если checked нет — ``avg_errors`` = None (нечего усреднять).

    ``runs`` — строки вида {'index','code','lint': RunLintResult|dict|None}.
    """
    checked = na = unavailable = 0
    total_errors = 0
    for run in runs:
        code = run.get("code")
        lint = _run_lint_value(run)
        if lint is None:
            continue
        if lint.status == LINT_STATUS_CHECKED:
            # Числим checked только по успешным копиям: провальная копия не должна
            # влиять на среднее качество даже если у неё случайно есть lint.
            if code == 0:
                checked += 1
                total_errors += int(lint.errors or 0)
            # na/unavailable считаем по всем копиям, у которых есть lint-оценка,
            # но в average они не идут.
        elif lint.status == LINT_STATUS_NA:
            na += 1
        elif lint.status == LINT_STATUS_UNAVAILABLE:
            unavailable += 1
    return {
        "checked": checked,
        "na": na,
        "unavailable": unavailable,
        "total_errors": total_errors,
        "avg_errors": round(total_errors / checked, 2) if checked else None,
    }

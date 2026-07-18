#!/usr/bin/env python3
"""Перегенерирует числовую детализацию expected в data/library_fine_matrix.json
под текущий эталон `library_fine_grading.reference_fine` (правило 9 «каждый день
отдельно», grace редкой=4, ставка студента НЕ округляется).

В `reference_fine` уже пересчитаны `fine`/`result` (через правки 2026-07), но
детализация `expected` (effectiveRate, dailyCharges[].rawAmount/unitRate,
dailySubtotal/afterSurcharge/beforeLimits) осталась под старый расчёт. Этот
скрипт пересчитывает её детерминированно из тела `reference_fine` — клонирует
расчёт, сохраняя промежуточные значения, и вписывает их в JSON.

НЕ трогает: `fine`, `result`, `graceDays`, `overdueDays`, `originalRate`,
`repeatSurcharge`, `pensionerMultiplier`, `minimumApplied`, `depositCapApplied`,
`chargedAmount` — эти поля assert'ит на совпадение (ловит регресс эталона).
НЕ трогает narrative `calculation` и `conditions` — это отдельная ручная правка.

`detail(case)` — полный клон тела `reference_fine` (library_fine_grading.py:170-204)
с промежуточными значениями. Арифметика — точная через Fraction; round до float
только при записи в JSON, чтобы не зависеть от float-представления (как в эталоне).

Формат чисел: int если Fraction целочислен (75, не 75.0), иначе float (19.25).
json.dumps(ensure_ascii=False, indent=2), ключи в исходном порядке — минимальный
дифф, без int→float шума.

Запуск:
    python scripts/regenerate_matrix_expected.py             # dry-run по умолчанию
    python scripts/regenerate_matrix_expected.py --apply     # записать
"""

import argparse
import json
import sys
from fractions import Fraction
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # корень
sys.path.insert(0, str(Path(__file__).resolve().parent))  # scripts — _common

import library_fine_grading as lg  # noqa: E402

# Константы/функции эталона — тот же код, расхождение физически невозможно.
from library_fine_grading import (  # noqa: E402
    BASE_RATE_RARE, BASE_RATE_REGULAR, GRACE_RARE, GRACE_REGULAR,
    STUDENT_RATE_MULT, PENSIONER_TOTAL_MULT, REPEAT_SURCHARGE, MIN_FINE,
    ceil10, overdue_days,
)


def format_num(value: Fraction) -> int | float:
    """int если Fraction целочислен, иначе float. Сохраняет 75 как int, 19.25
    как float без trailing zero."""
    v = Fraction(value)
    if v.denominator == 1:
        return int(v)
    return float(v)


def detail(case: lg.FineCase) -> dict | None:
    """Полный клон тела reference_fine (стр. 170-204), сохраняющий промежуточные
    значения. None — если days==0 (expected уже корректен для zero-day, не трогаем)."""
    days = overdue_days(case.control_date, case.actual_date)
    if days == 0:
        return None  # I6: нет просрочки — нет штрафа; expected уже корректен
    rate = Fraction(BASE_RATE_RARE if case.category else BASE_RATE_REGULAR)
    grace = GRACE_RARE if case.category else GRACE_REGULAR
    pensioner_discount = bool(case.pensioner)
    if case.student:
        base = Fraction(BASE_RATE_REGULAR)
        grace = GRACE_REGULAR
        if case.pensioner:
            base = -base
            pensioner_discount = False
        rate = base * STUDENT_RATE_MULT  # НЕ ceil10 — правило 9
    effective_rate = rate
    daily_charges: list[dict] = []
    total = Fraction(0)
    for n in range(1, min(days, grace) + 1):
        percent = 21 + 10 * (n - 1)
        daily = rate * Fraction(percent, 100)
        charged = ceil10(daily)
        total += charged
        daily_charges.append({
            "fromDay": n, "toDay": n, "count": 1, "percent": percent,
            "rawAmount": format_num(daily), "chargedAmount": charged,
        })
    for d in range(grace + 1, days + 1):  # полные дни — каждый отдельно (правило 9)
        charged = ceil10(rate)
        total += charged
        daily_charges.append({
            "fromDay": d, "toDay": d, "count": 1, "percent": 100,
            "unitRate": format_num(rate),
            "rawAmount": format_num(rate), "chargedAmount": charged,
        })
    daily_subtotal = int(total)
    repeat_surcharge = REPEAT_SURCHARGE if case.repeat else 0
    total += repeat_surcharge
    after_surcharge = int(total)
    pensioner_multiplier = None
    if pensioner_discount:
        pensioner_multiplier = float(PENSIONER_TOTAL_MULT)
        total = Fraction(ceil10(total * PENSIONER_TOTAL_MULT))
    before_limits = int(total)
    fine_after_min = max(before_limits, MIN_FINE)
    minimum_applied = before_limits < MIN_FINE
    fine = min(fine_after_min, case.deposit)
    deposit_cap_applied = fine_after_min > case.deposit
    # КРИТИЧНЫЙ assert: пересчёт обязан совпадать с эталоном по финалу.
    assert fine == lg.reference_fine(case), (
        f"{case.name}: detail fine={fine} ≠ reference_fine={lg.reference_fine(case)}")
    return {
        "overdueDays": days,
        "originalRate": BASE_RATE_RARE if case.category else BASE_RATE_REGULAR,
        "effectiveRate": format_num(effective_rate),
        "graceDays": grace,
        "dailyCharges": daily_charges,
        "dailySubtotal": daily_subtotal,
        "repeatSurcharge": repeat_surcharge,
        "afterSurcharge": after_surcharge,
        "pensionerMultiplier": pensioner_multiplier,
        "beforeLimits": before_limits,
        "minimumApplied": minimum_applied,
        "depositCapApplied": deposit_cap_applied,
        "fine": fine,
    }


def _norm(v):
    """Нормализация для сравнения int/float/None (5 == 5.0, None == None)."""
    if v is None:
        return None
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)):
        return float(v)
    return v


def _dc_norm(dc: dict) -> dict:
    """Нормализация элемента dailyCharges для сравнения."""
    return {k: _norm(val) for k, val in dc.items()}


def diff_expected(case: lg.FineCase, exp_json: dict) -> dict:
    """Возвращает {поле: (старое, новое)} для расходящихся полей, где новое надо
    вписать. Для неизменных полей, которые НЕ должны расходиться, кидает assert
    (ловит регресс эталона)."""
    d = detail(case)
    if d is None:
        return {}  # zero-day — не трогаем
    changes: dict[str, tuple] = {}
    # Поля, которые скрипт правит (числовая детализация):
    edit_fields = ("effectiveRate", "dailyCharges", "dailySubtotal",
                   "afterSurcharge", "beforeLimits")
    for f in edit_fields:
        old = exp_json.get(f)
        new = d[f]
        if f == "dailyCharges":
            assert isinstance(old, list), f"{case.name}: dailyCharges не list"
            old_n = [_dc_norm(x) for x in old]
            new_n = [_dc_norm(x) for x in new]
            if old_n != new_n:
                changes[f] = (old, new)
        elif _norm(old) != _norm(new):
            changes[f] = (old, new)
    # Поля, которые НЕ должны меняться — assert (страж регресса):
    for f in ("overdueDays", "originalRate", "graceDays", "repeatSurcharge",
              "pensionerMultiplier", "minimumApplied", "depositCapApplied", "fine"):
        if _norm(exp_json.get(f)) != _norm(d[f]):
            raise AssertionError(
                f"{case.name}: поле {f} НЕ должно меняться: "
                f"json={exp_json.get(f)!r} detail={d[f]!r} — регресс эталона или "
                f"уже разъехалось; разберись вручную, скрипт не правит это поле")
    # chargedAmount может меняться у student-кейсов (правило 9: неокруглённая
    # ставка меняет дневной ceil10) — поэтому не стражим по нему. Вместо этого —
    # инвариант: Σ chargedAmount == dailySubtotal в НОВОЙ детализации.
    sum_charged = sum(dc["chargedAmount"] for dc in d["dailyCharges"])
    assert sum_charged == d["dailySubtotal"], (
        f"{case.name}: Σ chargedAmount={sum_charged} ≠ dailySubtotal="
        f"{d['dailySubtotal']} — баг в detail()")
    return changes


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Перегенерация числовой детализации expected матрицы "
                    "library_fine под текущий эталон reference_fine.")
    # dry-run ПО УМОЛЧАНИЮ (безопасный контракт, как у delete_reports.py и др.):
    # без флагов — только показать правки; --apply чтобы записать.
    parser.add_argument(
        "--apply", action="store_true",
        help="записать изменения в data/library_fine_matrix.json "
             "(по умолчанию — dry-run, только показать)")
    args = parser.parse_args()
    apply = args.apply

    matrix_path = Path(lg.MATRIX_PATH)
    data = json.loads(matrix_path.read_text(encoding="utf-8"))
    rows = data["calculations"]["rows"]

    total_changes = 0
    changed_cases: list[str] = []
    for row in rows:
        case = lg.TEST_MATRIX[row["id"] - 1]
        changes = diff_expected(case, row["expected"])
        if changes:
            changed_cases.append(case.name)
            for f, (old, new) in changes.items():
                total_changes += 1
                print(f"  {case.name}: {f}: {old!r} → {new!r}")
            if apply:
                for f, (old, new) in changes.items():
                    row["expected"][f] = new
        # result должен == fine (страж): если разъехалось — правим синхронно
        if row.get("result") != row["expected"]["fine"]:
            if apply:
                row["result"] = row["expected"]["fine"]
            print(f"  {case.name}: result {row.get('result')} → "
                  f"{row['expected']['fine']} (==fine)")

    print()
    print(f"Кейсов с правками: {len(changed_cases)}: {changed_cases}")
    print(f"Всего правок полей: {total_changes}")
    if apply:
        # ensure_ascii=False, indent=2, ключи в исходном порядке (без sort_keys)
        matrix_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        print("ЗАПИСАНО (--apply)")
    else:
        print("(dry-run; --apply чтобы записать)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

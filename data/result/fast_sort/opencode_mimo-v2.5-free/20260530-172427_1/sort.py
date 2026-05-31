"""Быстрая сортировка (Quicksort)."""

from __future__ import annotations

from typing import TypeVar

T = TypeVar("T")


def quicksort(arr: list[T]) -> list[T]:
    """Возвращает новый отсортированный список, не изменяя исходный.

    Использует pivot — серединный элемент.
    Средняя сложность: O(n log n), худшая: O(n²).
    """
    if len(arr) <= 1:
        return arr
    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]
    return quicksort(left) + middle + quicksort(right)


def quicksort_inplace(arr: list[T], low: int = 0, high: int | None = None) -> None:
    """Сортирует список на месте (in-place) по алгоритму Hoare.

    Параметры
    ----------
    arr : list[T]
        Исходный список (модифицируется).
    low : int
        Левая граница (включительно).
    high : int | None
        Правая граница (включительно). Если None — берётся len(arr) - 1.
    """
    if high is None:
        high = len(arr) - 1
    if low >= high:
        return

    pivot = arr[(low + high) // 2]
    i, j = low, high

    while i <= j:
        while arr[i] < pivot:
            i += 1
        while arr[j] > pivot:
            j -= 1
        if i <= j:
            arr[i], arr[j] = arr[j], arr[i]
            i += 1
            j -= 1

    quicksort_inplace(arr, low, j)
    quicksort_inplace(arr, i, high)


# ── Демонстрация ──────────────────────────────────────────────
if __name__ == "__main__":
    data = [38, 27, 43, 3, 9, 82, 10]

    print("Исходный список :", data)
    print("quicksort()      :", quicksort(data))
    print("Исходный после quicksort():", data)  # не изменился

    quicksort_inplace(data)
    print("quicksort_inplace():", data)

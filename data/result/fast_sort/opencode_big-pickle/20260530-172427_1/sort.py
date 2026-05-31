from __future__ import annotations

import random
from typing import Protocol, TypeVar

T = TypeVar("T", bound="Comparable")


class Comparable(Protocol):
    """Тип, поддерживающий операторы сравнения."""
    def __lt__(self, other: object) -> bool: ...
    def __le__(self, other: object) -> bool: ...
    def __gt__(self, other: object) -> bool: ...
    def __ge__(self, other: object) -> bool: ...


def quicksort(arr: list[T]) -> list[T]:
    """Быстрая сортировка (in-place, рекурсивная)."""
    _quicksort(arr, 0, len(arr) - 1)
    return arr


def _quicksort(arr: list[T], lo: int, hi: int) -> None:
    """Рекурсивная сортировка подмассива arr[lo..hi]."""
    if lo >= hi or lo < 0:
        return

    p = _partition(arr, lo, hi)
    _quicksort(arr, lo, p - 1)
    _quicksort(arr, p + 1, hi)


def _partition(arr: list[T], lo: int, hi: int) -> int:
    """
    Схема разбиения Lomuto с опорным элементом посередине.

    Возвращает индекс опорного элемента после разбиения.
    """
    mid = (lo + hi) // 2
    arr[mid], arr[hi] = arr[hi], arr[mid]  # ставим опорный в конец
    pivot = arr[hi]

    i = lo  # граница элементов меньше опорного
    for j in range(lo, hi):
        if arr[j] <= pivot:
            arr[i], arr[j] = arr[j], arr[i]
            i += 1

    arr[i], arr[hi] = arr[hi], arr[i]
    return i


def quicksort_random(arr: list[T]) -> list[T]:
    """Быстрая сортировка со случайным выбором опорного элемента."""
    _quicksort_random(arr, 0, len(arr) - 1)
    return arr


def _quicksort_random(arr: list[T], lo: int, hi: int) -> None:
    if lo >= hi or lo < 0:
        return

    p = _partition_random(arr, lo, hi)
    _quicksort_random(arr, lo, p - 1)
    _quicksort_random(arr, p + 1, hi)


def _partition_random(arr: list[T], lo: int, hi: int) -> int:
    """Разбиение со случайным опорным элементом."""
    r = random.randint(lo, hi)
    arr[r], arr[hi] = arr[hi], arr[r]
    pivot = arr[hi]

    i = lo
    for j in range(lo, hi):
        if arr[j] <= pivot:
            arr[i], arr[j] = arr[j], arr[i]
            i += 1

    arr[i], arr[hi] = arr[hi], arr[i]
    return i

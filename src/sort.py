"""Sorting utilities.

Provides a simple implementation of quick sort.
"""

from __future__ import annotations

from typing import List, Protocol, TypeVar

T = TypeVar("T")


class Comparable(Protocol):
    def __lt__(self: "Comparable", other: "Comparable") -> bool: ...


def quick_sort(items: List[T]) -> List[T]:
    """Return a new list containing the items sorted in ascending order.

    The implementation uses the classic quick‑sort algorithm with the
    middle element chosen as pivot.  It works for any type that implements the
    ``<`` operator.
    """
    if len(items) <= 1:
        return items[:]
    pivot = items[len(items) // 2]
    lows = [x for x in items if x < pivot]
    highs = [x for x in items if x > pivot]
    pivots = [x for x in items if x == pivot]
    return quick_sort(lows) + pivots + quick_sort(highs)

__all__ = ["quick_sort"]

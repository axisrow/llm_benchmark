from __future__ import annotations

from typing import List


def quicksort(arr: List[int]) -> List[int]:
    """Return a new list with the elements of *arr* sorted in ascending order.

    Uses the classic recursive quicksort algorithm with a middle-pivot
    partitioning scheme.  The implementation is **not** in-place — it builds
    and returns a new list so that the original input is left untouched.
    """
    if len(arr) <= 1:
        return list(arr)

    pivot = arr[len(arr) // 2]
    left: List[int] = [x for x in arr if x < pivot]
    middle: List[int] = [x for x in arr if x == pivot]
    right: List[int] = [x for x in arr if x > pivot]

    return quicksort(left) + middle + quicksort(right)


if __name__ == "__main__":
    sample = [3, 6, 8, 10, 1, 2, 1]
    print(f"Before: {sample}")
    print(f"After:  {quicksort(sample)}")

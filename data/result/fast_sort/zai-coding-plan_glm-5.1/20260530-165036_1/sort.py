from __future__ import annotations

from typing import List


def quicksort(arr: List[int]) -> List[int]:
    """Return a new list with the elements of *arr* sorted in ascending order.

    Uses the classic recursive quicksort algorithm with a middle-element pivot.
    """
    if len(arr) <= 1:
        return list(arr)

    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]

    return quicksort(left) + middle + quicksort(right)


if __name__ == "__main__":
    data = [3, 6, 8, 10, 1, 2, 1]
    print("Original:", data)
    print("Sorted:  ", quicksort(data))

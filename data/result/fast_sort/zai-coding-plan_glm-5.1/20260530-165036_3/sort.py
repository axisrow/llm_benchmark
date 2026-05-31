from __future__ import annotations

from typing import List


def quicksort(arr: List[int]) -> List[int]:
    """Return a new list with the elements of *arr* sorted in ascending order.

    Uses the classic recursive quicksort algorithm with a middle-element pivot.
    """
    if len(arr) <= 1:
        return list(arr)

    pivot = arr[len(arr) // 2]

    less: List[int] = [x for x in arr if x < pivot]
    equal: List[int] = [x for x in arr if x == pivot]
    greater: List[int] = [x for x in arr if x > pivot]

    return quicksort(less) + equal + quicksort(greater)


if __name__ == "__main__":
    import random

    data = [random.randint(0, 100) for _ in range(20)]
    print("Original:", data)
    print("Sorted:  ", quicksort(data))

from typing import List, TypeVar

T = TypeVar('T')

def quick_sort(arr: List[T]) -> List[T]:
    """Return a new list with the elements of *arr* sorted using quicksort.

    The function works for any orderable type. It does not modify the input list.
    """
    if len(arr) <= 1:
        return arr[:]
    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]
    return quick_sort(left) + middle + quick_sort(right)

def in_place_quick_sort(arr: List[T], low: int = 0, high: int = None) -> None:
    """Sort *arr* in place using quicksort.

    Args:
        arr: List of orderable items.
        low: Starting index.
        high: Ending index (inclusive). If ``None``, sorts the whole list.
    """
    if high is None:
        high = len(arr) - 1
    if low < high:
        pi = _partition(arr, low, high)
        in_place_quick_sort(arr, low, pi - 1)
        in_place_quick_sort(arr, pi + 1, high)

def _partition(arr: List[T], low: int, high: int) -> int:
    pivot = arr[high]
    i = low - 1
    for j in range(low, high):
        if arr[j] <= pivot:
            i += 1
            arr[i], arr[j] = arr[j], arr[i]
    arr[i + 1], arr[high] = arr[high], arr[i + 1]
    return i + 1

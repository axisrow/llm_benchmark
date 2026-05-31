from typing import List


def quicksort(arr: List[int]) -> List[int]:
    """Быстрая сортировка (quick sort)."""
    if len(arr) <= 1:
        return arr

    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]

    return quicksort(left) + middle + quicksort(right)


if __name__ == "__main__":
    data = [3, 6, 8, 10, 1, 2, 1]
    print("Исходный массив:", data)
    print("Отсортированный :", quicksort(data))

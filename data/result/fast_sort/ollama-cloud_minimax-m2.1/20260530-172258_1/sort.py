"""Модуль быстрой сортировки (QuickSort)."""


def quicksort(arr: list) -> list:
    """
    Сортирует массив методом быстрой сортировки.

    Args:
        arr: Список элементов для сортировки.

    Returns:
        Отсортированный список.
    """
    if len(arr) <= 1:
        return arr

    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]

    return quicksort(left) + middle + quicksort(right)


if __name__ == "__main__":
    # Пример использования
    numbers = [3, 6, 8, 10, 1, 2, 1]
    print(f"До: {numbers}")
    print(f"После: {quicksort(numbers)}")
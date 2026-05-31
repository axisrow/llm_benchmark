"""Быстрая сортировка (Quick Sort) на Python."""
from typing import List


def quick_sort(arr: List[int]) -> List[int]:
    """
    Сортирует массив целых чисел методом быстрой сортировки.
    
    Args:
        arr: Список целых чисел для сортировки
        
    Returns:
        Отсортированный список целых чисел
    """
    if len(arr) <= 1:
        return arr.copy()
    
    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]
    
    return quick_sort(left) + middle + quick_sort(right)


def quick_sort_in_place(arr: List[int], low: int = 0, high: int = None) -> None:
    """
    Сортирует массив целых чисел методом быстрой сортировки вставками.
    
    Args:
        arr: Список целых чисел для сортировки (изменяется в месте)
        low: Начальный индекс подмассива
        high: Конечный индекс подмассива
    """
    if high is None:
        high = len(arr) - 1
    
    if low < high:
        pi = partition(arr, low, high)
        quick_sort_in_place(arr, low, pi - 1)
        quick_sort_in_place(arr, pi + 1, high)


def partition(arr: List[int], low: int, high: int) -> int:
    """
    Разделяет подмассив так, что элементы меньше опорного находятся слева,
    а больше — справа.
    
    Args:
        arr: Список целых чисел
        low: Начальный индекс
        high: Конечный индекс
        
    Returns:
        Индекс опорного элемента после разделения
    """
    pivot = arr[high]
    i = low - 1
    
    for j in range(low, high):
        if arr[j] <= pivot:
            i += 1
            arr[i], arr[j] = arr[j], arr[i]
    
    arr[i + 1], arr[high] = arr[high], arr[i + 1]
    return i + 1


if __name__ == "__main__":
    import random
    
    # Тестовый пример
    test_arr = [64, 34, 25, 12, 22, 11, 90]
    print(f"Исходный массив: {test_arr}")
    
    sorted_arr = quick_sort(test_arr)
    print(f"Отсортированный массив: {sorted_arr}")
    
    # Тест с произвольными данными
    random_arr = [random.randint(0, 1000) for _ in range(20)]
    print(f"\nСлучайный массив: {random_arr}")
    
    sorted_random = quick_sort(random_arr)
    print(f"Отсортированный: {sorted_random}")
"""Быстрая сортировка (quicksort) — алгоритм сортировки с разделением."""


def quicksort(arr: list, low: int = 0, high: int | None = None) -> None:
    """Сортирует список arr на отрезке [low, high] по возрастанию (in-place).

    Args:
        arr: Список чисел (или любых сравнимых элементов).
        low: Левая граница сортируемого отрезка (включительно).
        high: Правая граница сортируемого отрезка (включительно).
              Если None, сортируется весь список.
    """
    if high is None:
        high = len(arr) - 1

    if low < high:
        # Разделяем массив и получаем индекс опорного элемента
        pivot_index = _partition(arr, low, high)
        # Рекурсивно сортируем левую и правую части
        quicksort(arr, low, pivot_index - 1)
        quicksort(arr, pivot_index + 1, high)


def _partition(arr: list, low: int, high: int) -> int:
    """Разделяет отрезок arr[low..high] относительно опорного элемента.

    Используется схема Ломуто: опорный элемент — последний элемент отрезка.
    В конце опорный элемент оказывается на своём месте.

    Returns:
        Индекс опорного элемента после разделения.
    """
    pivot = arr[high]
    i = low - 1  # индекс последнего элемента, меньшего опорного

    for j in range(low, high):
        if arr[j] <= pivot:
            i += 1
            arr[i], arr[j] = arr[j], arr[i]

    # Ставим опорный элемент на своё место
    arr[i + 1], arr[high] = arr[high], arr[i + 1]
    return i + 1


if __name__ == "__main__":
    # Простейшая проверка
    test_data = [
        [],
        [1],
        [3, 2, 1],
        [5, 4, 3, 2, 1],
        [1, 2, 3, 4, 5],
        [3, 0, 1, 8, 7, 2, 5, 4, 9, 6],
        [-5, 10, 0, -3, 7, 2, -1],
    ]

    for arr in test_data:
        original = arr[:]
        quicksort(arr)
        expected = sorted(original)
        status = "✓" if arr == expected else "✗"
        print(f"{status} {original} -> {arr}")

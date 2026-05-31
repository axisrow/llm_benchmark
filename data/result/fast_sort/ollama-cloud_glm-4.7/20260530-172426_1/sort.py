def quicksort(arr):
    """
    Быстрая сортировка (QuickSort)
    
    Args:
        arr: Список элементов для сортировки
        
    Returns:
        Отсортированный список
    """
    if len(arr) <= 1:
        return arr
    
    # Выбираем опорный элемент (pivot)
    pivot = arr[len(arr) // 2]
    
    # Разделяем массив на три части
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]
    
    # Рекурсивно сортируем и объединяем
    return quicksort(left) + middle + quicksort(right)


def quicksort_inplace(arr, low=0, high=None):
    """
    Быстрая сортировка на месте (in-place)
    
    Args:
        arr: Список элементов для сортировки
        low: Нижний индекс (по умолчанию 0)
        high: Верхний индекс (по умолчанию len(arr) - 1)
        
    Returns:
        Отсортированный список (тот же объект)
    """
    if high is None:
        high = len(arr) - 1
    
    if low < high:
        # Разделяем массив и получаем индекс опорного элемента
        pivot_index = partition(arr, low, high)
        
        # Рекурсивно сортируем левую и правую части
        quicksort_inplace(arr, low, pivot_index - 1)
        quicksort_inplace(arr, pivot_index + 1, high)
    
    return arr


def partition(arr, low, high):
    """
    Разделяет массив и возвращает индекс опорного элемента
    
    Args:
        arr: Список элементов
        low: Нижний индекс
        high: Верхний индекс
        
    Returns:
        Индекс опорного элемента после разделения
    """
    # Выбираем последний элемент как опорный
    pivot = arr[high]
    i = low - 1
    
    for j in range(low, high):
        if arr[j] <= pivot:
            i += 1
            arr[i], arr[j] = arr[j], arr[i]
    
    # Помещаем опорный элемент на правильное место
    arr[i + 1], arr[high] = arr[high], arr[i + 1]
    return i + 1


if __name__ == "__main__":
    # Пример использования
    test_array = [64, 34, 25, 12, 22, 11, 90, 5]
    
    print("Исходный массив:", test_array)
    
    # Используем функцию, возвращающую новый массив
    sorted_array = quicksort(test_array.copy())
    print("Отсортированный массив (quicksort):", sorted_array)
    
    # Используем in-place версию
    test_array_copy = test_array.copy()
    quicksort_inplace(test_array_copy)
    print("Отсортированный массив (in-place):", test_array_copy)
    
    # Проверка
    assert sorted_array == sorted(test_array), "Сортировка работает некорректно"
    assert test_array_copy == sorted(test_array), "In-place сортировка работает некорректно"
    print("Все тесты пройдены!")
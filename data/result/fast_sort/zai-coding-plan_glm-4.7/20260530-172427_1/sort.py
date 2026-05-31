def quicksort(arr):
    """
    Реализация быстрой сортировки (Quick Sort).
    
    Args:
        arr: Список элементов для сортировки
        
    Returns:
        Отсортированный список
    """
    # Базовый случай: если список пустой или содержит один элемент, он уже отсортирован
    if len(arr) <= 1:
        return arr
    
    # Выбираем опорный элемент (pivot) - берем средний элемент
    pivot = arr[len(arr) // 2]
    
    # Разделяем элементы на три группы:
    # меньше опорного, равные опорному, больше опорного
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]
    
    # Рекурсивно сортируем левую и правую части
    return quicksort(left) + middle + quicksort(right)


def quicksort_inplace(arr, low=0, high=None):
    """
    Реализация быстрой сортировки с модификацией списка на месте (in-place).
    
    Args:
        arr: Список для сортировки (изменяется напрямую)
        low: Начальный индекс (по умолчанию 0)
        high: Конечный индекс (по умолчанию len(arr) - 1)
        
    Returns:
        Отсортированный список (тот же объект)
    """
    if high is None:
        high = len(arr) - 1
    
    if low < high:
        # Разделяем массив и получаем индекс опорного элемента
        pivot_index = partition(arr, low, high)
        
        # Рекурсивно сортируем элементы до и после опорного
        quicksort_inplace(arr, low, pivot_index - 1)
        quicksort_inplace(arr, pivot_index + 1, high)
    
    return arr


def partition(arr, low, high):
    """
    Вспомогательная функция для in-place версии: разделяет массив
    вокруг опорного элемента и возвращает его индекс.
    
    Args:
        arr: Список для разделения
        low: Начальный индекс
        high: Конечный индекс
        
    Returns:
        Индекс опорного элемента после разделения
    """
    # Выбираем последний элемент как опорный
    pivot = arr[high]
    
    # Индекс для элементов меньше опорного
    i = low - 1
    
    for j in range(low, high):
        if arr[j] <= pivot:
            i += 1
            arr[i], arr[j] = arr[j], arr[i]
    
    # Помещаем опорный элемент на правильную позицию
    arr[i + 1], arr[high] = arr[high], arr[i + 1]
    return i + 1


if __name__ == "__main__":
    # Пример использования
    test_arr = [64, 34, 25, 12, 22, 11, 90]
    print("Исходный массив:", test_arr)
    
    # Версия с созданием нового списка
    sorted_arr = quicksort(test_arr)
    print("Отсортированный (новый список):", sorted_arr)
    print("Исходный массив после quicksort:", test_arr)
    
    # In-place версия
    test_arr2 = [64, 34, 25, 12, 22, 11, 90]
    quicksort_inplace(test_arr2)
    print("Отсортированный (in-place):", test_arr2)
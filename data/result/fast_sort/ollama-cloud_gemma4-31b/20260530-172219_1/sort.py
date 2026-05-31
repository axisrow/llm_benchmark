from typing import List

def quicksort(arr: List[int]) -> List[int]:
    """
    Быстрая сортировка (Quicksort) массива целых чисел.
    """
    if len(arr) <= 1:
        return arr
    
    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]
    
    return quicksort(left) + middle + quicksort(right)

if __name__ == "__main__":
    test_cases = [
        [3, 6, 8, 10, 1, 2, 1],
        [1, 2, 3, 4, 5],
        [5, 4, 3, 2, 1],
        [],
        [1],
        [2, 2, 2, 2]
    ]
    
    for case in test_cases:
        print(f"Original: {case} -> Sorted: {quicksort(case)}")

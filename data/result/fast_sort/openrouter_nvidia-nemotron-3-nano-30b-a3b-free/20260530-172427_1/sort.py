def quick_sort(arr):
    """Quick sort implementation (returns a new sorted list)."""
    if len(arr) <= 1:
        return arr
    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]
    return quick_sort(left) + middle + quick_sort(right)


if __name__ == "__main__":
    # Example usage
    import random
    data = [random.randint(0, 100) for _ in range(10)]
    print("Unsorted:", data)
    print("Sorted:  ", quick_sort(data))
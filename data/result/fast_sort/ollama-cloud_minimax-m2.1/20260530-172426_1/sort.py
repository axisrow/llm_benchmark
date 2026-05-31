"""QuickSort implementation in Python."""


def quicksort(arr: list) -> list:
    """Sorts a list using the quicksort algorithm."""
    if len(arr) <= 1:
        return arr

    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]

    return quicksort(left) + middle + quicksort(right)


if __name__ == "__main__":
    # Example usage
    test_list = [3, 6, 8, 10, 1, 2, 1]
    print(f"Original: {test_list}")
    print(f"Sorted:   {quicksort(test_list)}")
"""Quick sort implementation."""


def quick_sort(arr: list) -> list:
    """Return a new sorted list using the quick sort algorithm.

    Args:
        arr: List of comparable items to sort.

    Returns:
        A new list with the items in ascending order.
    """
    if len(arr) <= 1:
        return list(arr)

    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]

    return quick_sort(left) + middle + quick_sort(right)


if __name__ == "__main__":
    sample = [3, 6, 8, 10, 1, 2, 1]
    print(f"Before: {sample}")
    print(f"After:  {quick_sort(sample)}")

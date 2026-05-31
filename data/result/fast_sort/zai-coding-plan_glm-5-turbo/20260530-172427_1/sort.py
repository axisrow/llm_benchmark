"""Quick sort implementation."""


def quick_sort(arr: list[int | float]) -> list[int | float]:
    """Sort a list using the quick sort algorithm.

    Returns a new sorted list (does not mutate the original).
    """
    if len(arr) <= 1:
        return list(arr)

    pivot = arr[len(arr) // 2]
    left = [x for x in arr if x < pivot]
    middle = [x for x in arr if x == pivot]
    right = [x for x in arr if x > pivot]

    return quick_sort(left) + middle + quick_sort(right)


if __name__ == "__main__":
    data = [3, 6, 8, 10, 1, 2, 1]
    print("before:", data)
    print("after :", quick_sort(data))

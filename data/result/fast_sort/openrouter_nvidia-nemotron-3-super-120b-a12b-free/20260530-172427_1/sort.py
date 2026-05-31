from typing import List

def quicksort(arr: List[int]) -> List[int]:
    """
    Sorts a list of integers using the quicksort algorithm.

    Args:
        arr: List of integers to sort.

    Returns:
        A new list containing the sorted integers.
    """
    # Base case: lists with 0 or 1 element are already sorted
    if len(arr) <= 1:
        return arr.copy()

    # Choose pivot (here we use the last element)
    pivot = arr[-1]

    # Partition the array into three parts:
    # less than pivot, equal to pivot, greater than pivot
    less = [x for x in arr[:-1] if x < pivot]
    equal = [x for x in arr if x == pivot]
    greater = [x for x in arr[:-1] if x > pivot]

    # Recursively sort the partitions and concatenate
    return quicksort(less) + equal + quicksort(greater)


# Example usage
if __name__ == "__main__":
    example = [3, 6, 8, 10, 1, 2, 1]
    sorted_example = quicksort(example)
    print(f"Original: {example}")
    print(f"Sorted:   {sorted_example}")
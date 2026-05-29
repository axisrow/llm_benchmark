"""Simple Hello World script.

Prints a greeting to stdout when run as a script.
"""

from __future__ import annotations


def main() -> None:
    """Print a hello message to stdout.

    This function intentionally has a return type annotation to follow
    the project's Python typing guidelines.
    """
    print("Hello, world!")


if __name__ == "__main__":
    main()

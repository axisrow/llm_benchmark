#!/usr/bin/env python3
"""CLI wrapper for index_builder.build_index."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db import PROJECT_ROOT
from index_builder import build_index


if __name__ == "__main__":
    count = build_index()
    print(f"✓ Индекс создан: {PROJECT_ROOT / 'docs' / 'data' / 'index.json'}")
    print(f"  Найдено отчётов: {count}")

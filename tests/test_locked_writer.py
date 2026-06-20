"""Тест issue #54 (находка #8): общий locked_writer для log-writer.

Раньше идентичная thread-safe closure (lock + write + flush, контракт Writer для
probe_session) была задвоена в benchmark_report.run_copy и check_models.check_one.
Вынесена в opencode_runtime.locked_writer.
"""

import threading
import unittest


import opencode_runtime as rt


class _FakeFile:
    def __init__(self):
        self.text = ""
        self.flushes = 0

    def write(self, s: str) -> None:
        self.text += s

    def flush(self) -> None:
        self.flushes += 1


class LockedWriterTests(unittest.TestCase):
    def test_writes_and_flushes_each_call(self):
        fh = _FakeFile()
        write = rt.locked_writer(fh)
        write("a")
        write("b")
        self.assertEqual(fh.text, "ab")
        self.assertEqual(fh.flushes, 2)  # flush на каждую запись

    def test_concurrent_writes_are_serialized(self):
        # 5 потоков по 100 записей: под общим lock каждая запись атомарна, строки
        # не перемешиваются и не теряются.
        fh = _FakeFile()
        write = rt.locked_writer(fh)

        def worker(n: int) -> None:
            for _ in range(100):
                write(f"{n}\n")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        lines = fh.text.splitlines()
        self.assertEqual(len(lines), 500)
        self.assertTrue(all(line in {"0", "1", "2", "3", "4"} for line in lines))


if __name__ == "__main__":
    unittest.main()

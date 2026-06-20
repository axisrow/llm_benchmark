"""Тест issue #54 (находка #2): порог «ложного таймаута» — единый источник.

Раньше литерал `code = 1 AND elapsed < 130` был задвоен в cleanup_runs.py и
cleanup_false_timeouts.py. Перетюнят порог в одном — скрипты разойдутся в
семантике удаления и повредят коммитящуюся в git базу. Теперь предикат живёт в
db.FALSE_TIMEOUT_SQL; этот тест ловит повторное расхождение.
"""

import unittest

import db
import scripts.cleanup_false_timeouts as cft
import scripts.cleanup_runs as cleanup


class FalseTimeoutThresholdTests(unittest.TestCase):
    def test_single_source_of_threshold(self):
        # Число пинуем отдельно (осознанный чекпоинт значения), а SQL —
        # выводим из константы, чтобы не дублировать литерал 130 ещё раз.
        self.assertEqual(db.FALSE_TIMEOUT_MAX_ELAPSED, 130)
        self.assertEqual(
            db.FALSE_TIMEOUT_SQL,
            f"code = 1 AND elapsed < {db.FALSE_TIMEOUT_MAX_ELAPSED}")
        # Оба destructive-скрипта берут предикат ровно из db — не свой литерал.
        self.assertEqual(cleanup.FALSE_TIMEOUT, db.FALSE_TIMEOUT_SQL)
        self.assertEqual(cft.FALSE_TIMEOUT, db.FALSE_TIMEOUT_SQL)


if __name__ == "__main__":
    unittest.main()

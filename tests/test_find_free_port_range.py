"""Тесты find_free_port_range (issue #123)."""

import socket
import unittest

from opencode_runtime import DEFAULT_BASE_PORT, find_free_port_range


class FindFreePortRangeTests(unittest.TestCase):
    """Детерминированные тесты find_free_port_range без гонок."""

    def test_find_one_returns_default_if_free(self):
        """find(1) возвращает DEFAULT_BASE_PORT если свободен."""
        # Предварительно проверяем, что DEFAULT_BASE_PORT свободен
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            try:
                s.bind(("127.0.0.1", DEFAULT_BASE_PORT))
            except OSError:
                # Порт занят, пропускаем тест (нечего проверять)
                self.skipTest(f"DEFAULT_BASE_PORT {DEFAULT_BASE_PORT} занят")
        # После проверки порт освобождён, должен вернуть DEFAULT_BASE_PORT
        result = find_free_port_range(1, start=DEFAULT_BASE_PORT)
        self.assertEqual(result, DEFAULT_BASE_PORT)

    def test_find_one_skips_busy_port(self):
        """Занятый порт P пропускается: find(1, start=P) > P."""
        # Занимаем порт
        busy_port = 50123
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            s.bind(("127.0.0.1", busy_port))
            # Порт busy_port занят, должен найти следующий свободный
            result = find_free_port_range(1, start=busy_port)
            self.assertGreater(result, busy_port)
            # Проверяем, что результат действительно свободен
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s2:
                s2.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                s2.bind(("127.0.0.1", result))
                # Успешно забиндились — порт свободен

    def test_range_skips_busy_port_in_middle(self):
        """Диапазон с занятым портом в середине пропускается."""
        # Занимаем порт в середине потенциального диапазона
        busy_port = 50124
        start = 50123
        n = 3  # Хотим [start, start+1, start+2]
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
            s.bind(("127.0.0.1", busy_port))
            # Диапазон [50123, 50124, 50125] не подходит (50124 занят)
            # Должен найти непересекающийся диапазон
            result = find_free_port_range(n, start=start)
            # Либо result < 50124 (диапазон [50121, 50122, 50123])
            # либо result > 50124 (диапазон после занятого)
            self.assertNotEqual(result, start)  # Не может быть start, т.к. busy_port в середине
            if result < start:
                # Диапазон ДО занятого порта
                self.assertGreaterEqual(result + n - 1, start - n)
            else:
                # Диапазон ПОСЛЕ занятого порта
                self.assertGreater(result, busy_port)

    def test_n_le_zero_raises(self):
        """n <= 0 вызывает ValueError."""
        with self.assertRaises(ValueError) as cm:
            find_free_port_range(0)
        self.assertIn("n должно быть > 0", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            find_free_port_range(-1)
        self.assertIn("n должно быть > 0", str(cm.exception))

    def test_start_out_of_range_raises(self):
        """start вне 1..65535 вызывает ValueError."""
        with self.assertRaises(ValueError) as cm:
            find_free_port_range(1, start=0)
        self.assertIn("start должен быть в 1..65535", str(cm.exception))
        with self.assertRaises(ValueError) as cm:
            find_free_port_range(1, start=65536)
        self.assertIn("start должен быть в 1..65535", str(cm.exception))

    def test_no_space_for_range_raises(self):
        """Нет места для диапазона до 65535 -> ValueError."""
        # start=65535, n=2 -> нужно [65535, 65536], но 65536 не существует
        with self.assertRaises(ValueError) as cm:
            find_free_port_range(2, start=65535)
        self.assertIn("Нет места для диапазона", str(cm.exception))

    def test_returns_first_port_of_range(self):
        """Возвращает первый порт диапазона, проверяем весь диапазон."""
        result = find_free_port_range(3, start=50000)
        # Проверяем, что все три порта свободны
        for offset in range(3):
            port = result + offset
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 0)
                s.bind(("127.0.0.1", port))
                # Успешно забиндились — порт свободен


if __name__ == "__main__":
    unittest.main()

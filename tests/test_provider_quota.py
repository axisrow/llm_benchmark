"""Тесты scripts/provider_quota.py (PR #166) — redirect-safety + file-mode.

C1 (codex cycle-1): _fetch_json/_oauth_refresh НЕ должны форвардить
Authorization/refresh_token на кросс-хост redirect (тот же класс что C1 в
zai_quota #164). C2: _atomic_write НЕ должен понижать права cred-файла 0600.
"""

import os
import socket
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import provider_quota  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Capture:
    """target: ловит Authorization после редиректа с origin (другой порт)."""

    def __init__(self) -> None:
        self.auth: str | None = None
        self.port = _free_port()
        capture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                capture.auth = self.headers.get("Authorization")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":{}}')

            def log_message(self, *a, **k) -> None:
                pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._srv.allow_reuse_address = True
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


class _Redirect:
    """origin: 302 → target на ДРУГОМ порту (кросс-хост для urllib)."""

    def __init__(self, target_port: int) -> None:
        self.port = _free_port()
        tp = target_port

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location", f"http://127.0.0.1:{tp}/")
                self.end_headers()

            def log_message(self, *a, **k) -> None:
                pass

        self._srv = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self._srv.allow_reuse_address = True
        self._thread = threading.Thread(target=self._srv.serve_forever,
                                        daemon=True)

    def start(self) -> None:
        self._thread.start()

    def stop(self) -> None:
        self._srv.shutdown()
        self._srv.server_close()


class RedirectSafetyTests(unittest.TestCase):
    """C1: Authorization не уходит на кросс-хост редирект в _fetch_json."""

    def test_authorization_not_forwarded_across_host_redirect(self) -> None:
        target = _Capture()
        origin = _Redirect(target.port)
        target.start()
        origin.start()
        try:
            url = f"http://127.0.0.1:{origin.port}/"
            try:
                provider_quota._fetch_json(
                    url, {"Authorization": "sk-SECRET-KEY-123"}, timeout=5)
            except Exception:
                pass  # редирект заблокирован — тоже валидно
        finally:
            origin.stop()
            target.stop()
        self.assertNotEqual(
            target.auth, "sk-SECRET-KEY-123",
            "C1 РЕГРЕССИЯ: Authorization форвардится на кросс-хост редирект")


class AtomicWriteModeTests(unittest.TestCase):
    """C2: _atomic_write сохраняет права 0600 cred-файла."""

    def test_atomic_write_preserves_0600(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            cred = Path(td) / "cred.json"
            fd = os.open(str(cred), os.O_WRONLY | os.O_CREAT, 0o600)
            os.write(fd, b"{}")
            os.close(fd)
            self.assertEqual(oct(cred.stat().st_mode & 0o777), "0o600")
            provider_quota._atomic_write(cred, {"tokens": {"key": "x"}})
            mode = oct(cred.stat().st_mode & 0o777)
            self.assertEqual(mode, "0o600",
                             f"C2: cred-файл стал {mode} после _atomic_write "
                             "(должен остаться 0600)")

    def test_atomic_write_ignores_stale_0644_tmp(self) -> None:
        # C3 (codex cycle-2): stale .tmp 0644 (от старой write_text-версии)
        # не должен обойти фикс — cred остаётся 0600 даже если .tmp уже есть
        # с широкими правами (O_TRUNC не меняет режим существующего inode).
        import tempfile
        with tempfile.TemporaryDirectory() as td:
            cred = Path(td) / "auth.json"
            fd = os.open(str(cred), os.O_WRONLY | os.O_CREAT, 0o600)
            os.write(fd, b"{}")
            os.close(fd)
            # precreate stale .tmp с 0644 (как старая write_text версия)
            tmp = cred.with_suffix(".json.tmp")
            fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT, 0o644)
            os.write(fd, b"stale")
            os.close(fd)
            provider_quota._atomic_write(cred, {"tokens": {"key": "x"}})
            mode = oct(cred.stat().st_mode & 0o777)
            self.assertEqual(mode, "0o600",
                             f"C3: cred стал {mode} из-за stale 0644 .tmp "
                             "(должен остаться 0600)")


if __name__ == "__main__":
    unittest.main()

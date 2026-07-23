"""Тест /api/provider_quota в dashboard_server (issue #163/MVP).

Endpoint отдаёт live-квоты провайдеров через collect_all_quotas(). Тестируем
handler-интеграцию (маршрутизация /api/provider_quota, 200, JSON), мокая сам
сборщик — не зависим от live-сети/ключей в CI.
"""

import functools
import json
import socketserver
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import dashboard_server

REQUEST_TIMEOUT = 10.0


class ProviderQuotaApiTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self._docs = Path(self._tmp.name) / "docs"
        self._docs.mkdir()
        (self._docs / "index.html").write_text("<html></html>")
        self._db_path = Path(self._tmp.name) / "main.db"
        self._db_path.write_bytes(b"")

        handler_cls = dashboard_server.make_dashboard_handler(self._db_path)
        handler = functools.partial(handler_cls, directory=str(self._docs))
        self._httpd = socketserver.TCPServer(("127.0.0.1", 0), handler)
        self._port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever,
                                        daemon=True)
        self._thread.start()
        self.addCleanup(self._stop)

    def _stop(self):
        self._httpd.shutdown()
        self._thread.join(timeout=5)
        self._httpd.server_close()

    def _request(self, method, path):
        url = f"http://127.0.0.1:{self._port}{path}"
        req = urllib.request.Request(url, method=method)
        try:
            resp = urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT)
            return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_endpoint_returns_collected_quotas(self):
        fake = {"auth_path": "/fake", "providers": [
            {"name": "Z.AI", "provider": "zai-coding-plan", "status": "ok",
             "tariff": "max", "items": [{"label": "x", "value": "1%"}]},
            {"name": "Ollama", "provider": "ollama-cloud",
             "status": "unavailable", "reason": "no endpoint"},
        ]}
        with mock.patch.object(dashboard_server, "_collect_provider_quota",
                               return_value=fake):
            status, body = self._request("GET", "/api/provider_quota")
        self.assertEqual(status, 200)
        data = json.loads(body.decode())
        self.assertEqual(data["providers"], fake["providers"])
        self.assertEqual(data["auth_path"], "/fake")

    def test_unknown_api_still_404(self):
        status, _ = self._request("GET", "/api/nonexistent")
        self.assertEqual(status, 404)


if __name__ == "__main__":
    unittest.main()

"""Тесты scripts/zai_quota.py (issue #163/PR #164).

Standalone-утилита мониторинга квоты Z.AI — только stdlib. Здесь покрываем
поведение, а не live-вызовы (сеть/ключи замоканы): редирект-безопасность (C1,
cycle-1 codex), парсинг auth.json, --models + --json взаимодействие.
"""

import contextlib
import io
import json
import os
import socket
import sys
import threading
import unittest
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
import zai_quota  # noqa: E402


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class _Capture:
    """HTTP-сервер ловит то, что пришло после редиректа (target)."""

    def __init__(self) -> None:
        self.auth: str | None = None
        self.host: str | None = None
        self.port = _free_port()

        capture = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                capture.auth = self.headers.get("Authorization")
                capture.host = self.headers.get("Host")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":{"level":"max","limits":[]}}')

            def log_message(self, *args, **kwargs) -> None:
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


class _RedirectToOtherHost:
    """Origin: 302 → target на ДРУГОМ порту (другой хост для urllib)."""

    def __init__(self, target_port: int) -> None:
        self.target_port = target_port
        self.port = _free_port()
        target_port_ref = target_port

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(302)
                self.send_header("Location",
                                 f"http://127.0.0.1:{target_port_ref}/")
                self.end_headers()

            def log_message(self, *args, **kwargs) -> None:
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
    """C1 (codex cycle-1): Authorization НЕ должен уходить на кросс-хост редирект."""

    def test_authorization_not_forwarded_across_host_redirect(self) -> None:
        # Контракт фикса: редирект на другой origin НЕ должен уносить ключ.
        # Фикс либо блокирует редирект, либо разрешает его, сняв Authorization.
        target = _Capture()
        origin = _RedirectToOtherHost(target.port)
        target.start()
        origin.start()
        try:
            url = f"http://127.0.0.1:{origin.port}/"
            try:
                zai_quota.fetch_json(url, "sk-SECRET-KEY-123", timeout=5)
            except (urllib.error.URLError, SystemExit, ConnectionError):
                pass  # редирект заблокирован — тоже валидный исход
        finally:
            origin.stop()
            target.stop()

        # Если target дёрнули (редирект выполнен) — ключ НЕ должен был дойти.
        self.assertNotEqual(
            target.auth, "sk-SECRET-KEY-123",
            "C1 РЕГРЕССИЯ: Authorization форвардится на кросс-хост редирект "
            f"(target получил {target.auth!r})")


class ResolveApiKeyTests(unittest.TestCase):
    """R2: повреждённый auth.json → читаемая SystemExit, не трейсбек."""

    def test_corrupt_auth_json_raises_readable_error(self) -> None:
        env = {k: v for k, v in os.environ.items() if k != "ZAI_API_KEY"}
        with mock.patch.object(Path, "exists", return_value=True), \
                mock.patch.object(Path, "read_text", return_value="{not json"), \
                mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as cm:
                zai_quota.resolve_api_key(
                    auth_path=Path("/fake/auth.json"), api_key=None)
            # Сообщение должно быть читаемым, упоминать auth.json.
            self.assertIn("auth.json", str(cm.exception).lower())

    def test_explicit_key_wins(self) -> None:
        key = zai_quota.resolve_api_key(
            auth_path=Path("/nonexistent"), api_key="sk-explicit")
        self.assertEqual(key, "sk-explicit")

    def test_platform_zhipu_picks_zhipu_key_not_zai(self) -> None:
        # C2 (codex cycle-2): при --platform zhipu и наличии ОБЕИХ ключей в
        # auth.json должен выбрать zhipu-ключ — НЕ ZAI. Иначе credential
        # уходит на чужой origin (open.bigmodel.cn с ZAI-ключом и наоборот).
        auth_json = json.dumps({
            "zai-coding-plan": {"key": "sk-ZAI-KEY"},
            "zhipu-coding-plan": {"key": "sk-ZHIPU-KEY"},
        })
        env = {k: v for k, v in os.environ.items() if k != "ZAI_API_KEY"}
        with mock.patch.object(Path, "exists", return_value=True), \
                mock.patch.object(Path, "read_text", return_value=auth_json), \
                mock.patch.dict(os.environ, env, clear=True):
            key = zai_quota.resolve_api_key(
                auth_path=Path("/fake/auth.json"), api_key=None,
                platform="zhipu")
        self.assertEqual(key, "sk-ZHIPU-KEY",
                         "C2: --platform zhipu должен выбрать zhipu-ключ, "
                         f"не ZAI (получили {key!r})")

    def test_platform_zai_picks_zai_key(self) -> None:
        auth_json = json.dumps({
            "zai-coding-plan": {"key": "sk-ZAI-KEY"},
            "zhipu-coding-plan": {"key": "sk-ZHIPU-KEY"},
        })
        env = {k: v for k, v in os.environ.items() if k != "ZAI_API_KEY"}
        with mock.patch.object(Path, "exists", return_value=True), \
                mock.patch.object(Path, "read_text", return_value=auth_json), \
                mock.patch.dict(os.environ, env, clear=True):
            key = zai_quota.resolve_api_key(
                auth_path=Path("/fake/auth.json"), api_key=None,
                platform="zai")
        self.assertEqual(key, "sk-ZAI-KEY")

    def test_openai_oauth_token_never_used_as_zai_key(self) -> None:
        # C3 (codex cycle-3): openai в auth.json — это OAuth-credential (поле
        # access), отдельная от Coding Plan. Если zai-coding-plan отсутствует,
        # скрипт НЕ должен падать на openai.access как на ZAI-ключ — иначе
        # OAuth-токен уходит на api.z.ai в голом Authorization.
        auth_json = json.dumps({
            "openai": {"type": "oauth", "access": "oauth-OPENAI-TOKEN-xyz"},
        })
        env = {k: v for k, v in os.environ.items()
               if k not in ("ZAI_API_KEY", "ZHIPU_API_KEY")}
        with mock.patch.object(Path, "exists", return_value=True), \
                mock.patch.object(Path, "read_text", return_value=auth_json), \
                mock.patch.dict(os.environ, env, clear=True):
            with self.assertRaises(SystemExit) as cm:
                zai_quota.resolve_api_key(
                    auth_path=Path("/fake/auth.json"), api_key=None,
                    platform="zai")
            self.assertIn("zai", str(cm.exception).lower())

    def test_zai_api_key_env_not_used_for_zhipu(self) -> None:
        # C4 (codex cycle-3): ZAI_API_KEY — резерв ТОЛЬКО для платформы zai.
        # При --platform zhipu он не должен перехватывать выбор, иначе ZAI-ключ
        # уходит на open.bigmodel.cn.
        auth_json = json.dumps({
            "zhipu-coding-plan": {"key": "sk-ZHIPU-KEY"},
        })
        env = {**{k: v for k, v in os.environ.items()
                  if k not in ("ZAI_API_KEY", "ZHIPU_API_KEY")},
               "ZAI_API_KEY": "sk-ZAI-ENV-LEAK"}
        with mock.patch.object(Path, "exists", return_value=True), \
                mock.patch.object(Path, "read_text", return_value=auth_json), \
                mock.patch.dict(os.environ, env, clear=True):
            key = zai_quota.resolve_api_key(
                auth_path=Path("/fake/auth.json"), api_key=None,
                platform="zhipu")
        self.assertEqual(key, "sk-ZHIPU-KEY",
                         "C4: --platform zhipu не должен брать ZAI_API_KEY "
                         f"(получили {key!r})")


class JsonAndModelsFlagTests(unittest.TestCase):
    """R3: --models --json должен выдавать и квоту, и модели (не терять квоту)."""

    def test_models_json_includes_quota_and_models(self) -> None:
        quota_payload = {"data": {"level": "max",
                                  "limits": [{"type": "TOKENS_LIMIT",
                                              "percentage": 13}]}}
        models_payload = {"data": {"list": [{"modelCode": "glm-5.2",
                                              "usage": 90000}]}}
        calls = {"quota": 0, "models": 0}

        def fake_fetch(url, _key, **_kw):
            if "quota" in url:
                calls["quota"] += 1
                return quota_payload
            calls["models"] += 1
            return models_payload

        buf = io.StringIO()
        argv = ["zai_quota", "--json", "--models"]
        with mock.patch.object(sys, "argv", argv), \
                mock.patch.object(zai_quota, "fetch_json", side_effect=fake_fetch), \
                mock.patch.object(zai_quota, "resolve_api_key", return_value="sk-x"), \
                contextlib.redirect_stdout(buf):
            rc = zai_quota.main()

        self.assertEqual(rc, 0)
        out = buf.getvalue()
        self.assertIn("TOKENS_LIMIT", out, "R3: --models --json потерял квоту")
        self.assertIn("glm-5.2", out, "R3: --models --json потерял модели")
        self.assertEqual(calls["quota"], 1)
        self.assertEqual(calls["models"], 1)


if __name__ == "__main__":
    unittest.main()

"""SECURITY/edge-case покрытие санитайзеров и парсеров (issue #38 P1).

Ключевая защита приватности: секреты/PII из тела провайдера не должны утекать в
public_reason → raw_json → дашборд. Здесь же — граничные случаи sanitize_name,
split_model_ref и round-trip read_artifact. Сеть/opencode не дёргаются, реальная
data/main.db не трогается (всё через tempfile-БД).
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import artifacts
import db
import opencode_runtime as runtime

HIDDEN = "[скрыто]"


class ScrubSecretsTests(unittest.TestCase):
    """_scrub_secrets: каждый паттерн _SECRET_PATTERNS режется в «[скрыто]»."""

    def _assert_scrubbed(self, text: str, secret: str) -> str:
        out = runtime._scrub_secrets(text)
        self.assertIn(HIDDEN, out, f"маркер скрытия отсутствует в {out!r}")
        self.assertNotIn(secret, out, f"секрет {secret!r} утёк в {out!r}")
        return out

    def test_bearer_token_scrubbed(self):
        token = "abcDEF1234567890token"
        self._assert_scrubbed(f"Authorization: Bearer {token}", token)

    def test_lowercase_bearer_token_scrubbed(self):
        token = "qwertyuiopASDFGH987"
        self._assert_scrubbed(f"bearer {token}", token)

    def test_api_key_sk_scrubbed(self):
        secret = "sk-ABCDEF123456"
        self._assert_scrubbed(f"key was {secret} rejected", secret)

    def test_api_key_ghp_scrubbed(self):
        secret = "ghp_aBcDeF0123456789"
        self._assert_scrubbed(f"github token {secret}", secret)

    def test_email_scrubbed(self):
        email = "user.name@example.com"
        self._assert_scrubbed(f"account {email} not found", email)

    def test_https_url_scrubbed(self):
        url = "https://api.provider.test/v1/keys?token=topsecretvalue123"
        self._assert_scrubbed(f"call failed {url} oops", url)

    def test_long_token_like_string_scrubbed(self):
        # >=20 символов токено-подобной строки.
        token = "A1B2C3D4E5F6G7H8I9J0K1"
        self.assertGreaterEqual(len(token), 20)
        self._assert_scrubbed(f"trace id {token} done", token)

    def test_benign_short_text_left_intact(self):
        text = "нет ответа за 60с"
        self.assertEqual(runtime._scrub_secrets(text), text)
        self.assertNotIn(HIDDEN, runtime._scrub_secrets(text))


class PublicReasonTests(unittest.TestCase):
    """public_reason: безопасный публичный каркас, без утечки тела провайдера."""

    def test_none_returns_none(self):
        self.assertIsNone(runtime.public_reason(None))

    def test_empty_string_returns_none(self):
        self.assertIsNone(runtime.public_reason(""))

    def test_unauthorized_keyword(self):
        self.assertEqual(runtime.public_reason("request unauthorized"),
                         "ошибка авторизации")

    def test_forbidden_keyword(self):
        self.assertEqual(runtime.public_reason("access forbidden"),
                         "ошибка авторизации")

    def test_auth_with_http_code_prefix(self):
        self.assertEqual(runtime.public_reason("HTTP 401 Unauthorized"),
                         "HTTP 401: ошибка авторизации")

    def test_auth_403_code(self):
        self.assertEqual(runtime.public_reason("HTTP 403 nope"),
                         "HTTP 403: ошибка авторизации")

    def test_rate_limit(self):
        self.assertEqual(runtime.public_reason("rate limit exceeded"),
                         "превышен лимит/квота")

    def test_rate_limit_429_with_prefix(self):
        self.assertEqual(runtime.public_reason("HTTP 429 too many requests"),
                         "HTTP 429: превышен лимит/квота")

    def test_account_billing(self):
        self.assertEqual(runtime.public_reason("billing problem"),
                         "проблема аккаунта/биллинга")

    def test_account_billing_with_prefix(self):
        self.assertEqual(
            runtime.public_reason("HTTP 402 insufficient credits left"),
            "HTTP 402: проблема аккаунта/биллинга",
        )

    def test_timeout_reason_kept_head_only_and_scrubbed(self):
        secret = "ghp_secretTAILtoken123456"
        reason = f"нет ответа за 60с | provider tail Bearer {secret}"
        out = runtime.public_reason(reason)
        # Берётся только головная (безопасная) часть до " | ".
        self.assertEqual(out, "нет ответа за 60с")
        self.assertNotIn(secret, out)

    def test_timeout_reason_head_is_scrubbed(self):
        # Даже головная часть таймаута проходит скрабинг.
        secret = "ghp_headSecretToken0123456"
        out = runtime.public_reason(f"нет ответа Bearer {secret}")
        self.assertNotIn(secret, out)
        self.assertIn(HIDDEN, out)

    def test_unrecognized_with_http_code_does_not_leak_body(self):
        # КЛЮЧЕВОЕ СВОЙСТВО: нераспознанная причина с HTTP-кодом отдаёт только
        # «HTTP <код>: ошибка провайдера», тело провайдера (с токеном) не утекает.
        secret = "sk-LEAKEDsecret0123456789"
        body = (f'HTTP 500 {{"error":{{"message":"key {secret} invalid",'
                f'"apiKey":"Bearer {secret}"}}}}')
        out = runtime.public_reason(body)
        self.assertEqual(out, "HTTP 500: ошибка провайдера")
        self.assertNotIn(secret, out)
        self.assertNotIn("apiKey", out)

    def test_unrecognized_without_code_is_generic(self):
        secret = "ghp_anotherLeak01234567890"
        out = runtime.public_reason(f"weird provider blob {secret}")
        self.assertEqual(out, "ошибка провайдера")
        self.assertNotIn(secret, out)

    def test_local_infra_reason_scrubbed_not_misclassified(self):
        # Локальная причина ("сбой ...") идёт до keyword-классификации: слово
        # forbidden в пути не должно стать «ошибкой авторизации», а секрет режется.
        secret = "ghp_localPathSecret012345678"
        reason = f"сбой future forbidden /tmp/Bearer {secret}"
        out = runtime.public_reason(reason)
        self.assertNotEqual(out, "ошибка авторизации")
        self.assertTrue(out.startswith("сбой"))
        self.assertNotIn(secret, out)


class SanitizeNameTests(unittest.TestCase):
    """sanitize_name: нормализация имён для путей рабочих папок."""

    def test_spaces_to_dash(self):
        self.assertEqual(runtime.sanitize_name("my proj"), "my-proj")

    def test_slashes_to_dash(self):
        self.assertEqual(runtime.sanitize_name("zai/coding"), "zai-coding")

    def test_double_dots_collapsed(self):
        # ".." схлопывается до "." (защита от обхода каталогов).
        self.assertEqual(runtime.sanitize_name("a..b"), "a.b")
        self.assertNotIn("..", runtime.sanitize_name("..foo.."))

    def test_leading_trailing_stripped(self):
        self.assertEqual(runtime.sanitize_name("--foo.."), "foo")
        self.assertEqual(runtime.sanitize_name("...bar"), "bar")

    def test_empty_becomes_x(self):
        self.assertEqual(runtime.sanitize_name(""), "x")

    def test_only_punctuation_becomes_x(self):
        self.assertEqual(runtime.sanitize_name("/// ..."), "x")


class SplitModelRefTests(unittest.TestCase):
    """split_model_ref: разбор 'provider/model' по ПЕРВОМУ слешу."""

    def test_valid_pair(self):
        self.assertEqual(db.split_model_ref("prov/model"), ("prov", "model"))

    def test_strips_whitespace(self):
        self.assertEqual(db.split_model_ref("  prov / model "),
                         ("prov", "model"))

    def test_split_on_first_slash_only(self):
        self.assertEqual(db.split_model_ref("a/b/c"), ("a", "b/c"))

    def test_no_slash_raises(self):
        with self.assertRaises(ValueError):
            db.split_model_ref("noslash")

    def test_empty_provider_raises(self):
        with self.assertRaises(ValueError):
            db.split_model_ref("/model")

    def test_empty_model_raises(self):
        with self.assertRaises(ValueError):
            db.split_model_ref("prov/")

    def test_blank_provider_raises(self):
        with self.assertRaises(ValueError):
            db.split_model_ref("   /model")


class ReadArtifactTests(unittest.TestCase):
    """read_artifact: round-trip существующего артефакта и contract на отсутствие."""

    @staticmethod
    def _report() -> dict:
        return {
            "project": "p",
            "provider": "provider",
            "model": "model",
            "started_at": "2026-01-01T00:00:00",
            "summary": {"ok": 1, "timeout": 0, "error": 0},
            "runs": [{
                "index": 1,
                "port": 4096,
                "dir": "/tmp/run1",
                "status": "готово",
                "code": 0,
                "elapsed": 1.0,
            }],
        }

    def test_round_trip_and_missing(self):
        payload = b"print('hi')\n"
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            run_dir = root / "run1"
            run_dir.mkdir()
            (run_dir / "hello.py").write_bytes(payload)
            collection = artifacts.collect_run_artifacts(1, run_dir)

            conn = db.connect(root / "main.db")
            try:
                db.init_schema(conn)
                report = self._report()
                with conn:
                    report_id = db.upsert_report(
                        conn,
                        report,
                        "data/result/p/report.json",
                        json.dumps(report),
                        artifacts=collection.artifacts,
                    )

                # Существующий артефакт: round-trip декодированных байт.
                self.assertEqual(
                    db.read_artifact(conn, report_id, 1, "hello.py"),
                    payload,
                )

                # Несуществующий артефакт: по контракту функция бросает
                # FileNotFoundError (НЕ возвращает None — см. db.read_artifact).
                with self.assertRaises(FileNotFoundError):
                    db.read_artifact(conn, report_id, 1, "missing.py")
                with self.assertRaises(FileNotFoundError):
                    db.read_artifact(conn, report_id, 99, "hello.py")
            finally:
                conn.close()


if __name__ == "__main__":
    unittest.main()

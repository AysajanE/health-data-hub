from __future__ import annotations

from contextlib import contextmanager, redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from io import BytesIO, StringIO
import json
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlsplit

from scripts import oura_authorize
from src.ingestion.oura_auth import OuraAuthError, OuraCredentials, TokenStore


NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)
ACCESS_TOKEN = "fixture-new-access-token"
REFRESH_TOKEN = "fixture-new-refresh-token"
CODE = "fixture-authorization-code"
CREDENTIALS = OuraCredentials("fixture-client-id", "fixture-client-secret")


class FakeTransport:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict, float]] = []

    def post_form(self, url, fields, *, timeout):
        self.calls.append((url, dict(fields), timeout))
        return 200, {
            "access_token": ACCESS_TOKEN,
            "refresh_token": REFRESH_TOKEN,
            "token_type": "Bearer",
            "scope": "daily",
            "expires_in": 2592000,
        }

    def get_json(self, url, *, headers, timeout):
        raise AssertionError("Authorization does not fetch collections")


class ParseCallbackTest(unittest.TestCase):
    def test_valid_callback_returns_decoded_code(self) -> None:
        self.assertEqual(
            oura_authorize.parse_callback("/oauth/oura/callback?state=expected&code=abc%2Bdef", "expected"),
            "abc+def",
        )

    def test_rejects_bad_state_missing_and_duplicate_code_without_echo(self) -> None:
        queries = [
            f"state=mismatch&code={CODE}",
            "state=expected",
            "state=expected&code=",
            f"code={CODE}",
            f"state=expected&state=expected&code={CODE}",
            f"state=expected&code={CODE}&code=second",
            f"state=expected&code={CODE}&error=access_denied",
            f"state=expected&code={CODE}&malformed",
            f"state=expected&code={CODE}#fragment",
        ]
        for query in queries:
            with self.subTest(query=query):
                with self.assertRaises(OuraAuthError) as caught:
                    oura_authorize.parse_callback("/oauth/oura/callback?" + query, "expected")
                self.assertEqual(str(caught.exception), "Invalid Oura authorization callback")
                self.assertNotIn(CODE, str(caught.exception))

    def test_redirect_accepts_only_loopback_http_addresses(self) -> None:
        for value in (
            "https://localhost:8765/oauth/oura/callback",
            "http://example.com/oauth/oura/callback",
            "http://0.0.0.0:8765/oauth/oura/callback",
            "http://localhost:0/oauth/oura/callback",
            "http://user:secret@localhost/oauth/oura/callback",
            "http://localhost:8765/oauth/oura/callback?code=private",
            "http://localhost:8765/oauth/oura/callback#fragment",
        ):
            with self.subTest(value=value), self.assertRaises(OuraAuthError):
                oura_authorize._redirect_address(value)
        self.assertEqual(
            oura_authorize._redirect_address(oura_authorize.DEFAULT_REDIRECT_URI),
            ("localhost", 8765, "/oauth/oura/callback"),
        )
        self.assertEqual(oura_authorize._redirect_address("http://[::1]:8765/callback"), ("::1", 8765, "/callback"))


class CompleteAuthorizationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = TokenStore(Path(self.temp.name).resolve() / "secrets" / "tokens.json")
        self.transport = FakeTransport()

    def test_exchanges_and_saves_tokens_before_returning_safe_summary(self) -> None:
        summary = oura_authorize.complete_authorization(
            CODE,
            credentials=CREDENTIALS,
            redirect_uri=oura_authorize.DEFAULT_REDIRECT_URI,
            transport=self.transport,
            store=self.store,
            now=NOW,
        )
        saved = self.store.load()
        self.assertEqual(saved.access_token, ACCESS_TOKEN)
        self.assertEqual(saved.refresh_token, REFRESH_TOKEN)
        self.assertEqual(stat.S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.store.path.parent.stat().st_mode), 0o700)
        self.assertEqual(summary, {
            "status": "ok",
            "token_file": str(self.store.path),
            "expires_at_utc": "2026-10-10T00:00:00+00:00",
        })
        for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CODE, CREDENTIALS.client_secret):
            self.assertNotIn(secret, json.dumps(summary))
        self.assertEqual(self.transport.calls[0][1], {
            "grant_type": "authorization_code",
            "code": CODE,
            "redirect_uri": oura_authorize.DEFAULT_REDIRECT_URI,
            "client_id": CREDENTIALS.client_id,
            "client_secret": CREDENTIALS.client_secret,
        })

    def test_exchange_and_save_share_the_token_refresh_lock(self) -> None:
        active = []
        original_lock = self.store.refresh_lock
        original_post = self.transport.post_form
        original_save = self.store.save

        @contextmanager
        def observed_lock():
            with original_lock():
                active.append(True)
                try:
                    yield
                finally:
                    active.clear()

        def locked_post(*args, **kwargs):
            self.assertEqual(active, [True])
            return original_post(*args, **kwargs)

        def locked_save(tokens):
            self.assertEqual(active, [True])
            return original_save(tokens)

        with (
            patch.object(self.store, "refresh_lock", side_effect=observed_lock) as lock,
            patch.object(self.transport, "post_form", side_effect=locked_post),
            patch.object(self.store, "save", side_effect=locked_save),
        ):
            summary = oura_authorize.complete_authorization(
                CODE, credentials=CREDENTIALS, redirect_uri=oura_authorize.DEFAULT_REDIRECT_URI,
                transport=self.transport, store=self.store, now=NOW,
            )
        lock.assert_called_once_with()
        self.assertEqual(summary["status"], "ok")
        self.assertEqual(active, [])

    def invoke_handler(self, path: str):
        result = {"status": "error"}
        handler_type = oura_authorize._callback_handler(
            expected_path="/oauth/oura/callback",
            expected_state="expected",
            credentials=CREDENTIALS,
            redirect_uri=oura_authorize.DEFAULT_REDIRECT_URI,
            transport=self.transport,
            store=self.store,
            result=result,
        )
        handler = object.__new__(handler_type)
        handler.path = path
        handler.wfile = BytesIO()
        handler.send_response = Mock()
        handler.send_header = Mock()
        handler.end_headers = Mock()
        handler.do_GET()
        return handler, result

    def test_callback_rejects_wrong_path_and_state_without_token_exchange(self) -> None:
        for path in (
            f"/wrong?state=expected&code={CODE}",
            f"/oauth/oura/callback?state=wrong&code={CODE}",
            "/oauth/oura/callback?state=expected",
        ):
            with self.subTest(path=path):
                handler, result = self.invoke_handler(path)
                handler.send_response.assert_called_once_with(400)
                self.assertEqual(result["status"], "error")
                self.assertFalse(self.store.path.exists())
                self.assertEqual(self.transport.calls, [])
                self.assertNotIn(CODE, handler.wfile.getvalue().decode())

    def test_callback_success_and_http_logging_do_not_emit_secrets(self) -> None:
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            handler, result = self.invoke_handler(f"/oauth/oura/callback?state=expected&code={CODE}")
            handler.log_message("%s", CODE)
            handler.log_error("%s", ACCESS_TOKEN)
        self.assertEqual(stdout.getvalue() + stderr.getvalue(), "")
        handler.send_response.assert_called_once_with(200)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(handler.wfile.getvalue().decode(), "Authorization complete. You can close this tab.")
        self.assertTrue(self.store.path.exists())

    def test_main_no_browser_handles_one_request_without_real_server(self) -> None:
        env_file = Path(self.temp.name) / "settings.env"
        env_file.write_text("OURA_CLIENT_ID=fixture-client-id\nOURA_CLIENT_SECRET=fixture-client-secret\n")
        requests = []

        class FakeServer:
            def __init__(server, address, handler_type):
                self.assertEqual(address, ("localhost", 8765))
                server.handler_type = handler_type

            def __enter__(server):
                return server

            def __exit__(server, *args):
                pass

            def handle_request(server):
                requests.append(1)
                handler = object.__new__(server.handler_type)
                handler.path = f"/oauth/oura/callback?state=expected&code={CODE}"
                handler.wfile = BytesIO()
                handler.send_response = Mock()
                handler.send_header = Mock()
                handler.end_headers = Mock()
                handler.do_GET()

        stdout, stderr = StringIO(), StringIO()
        with (
            patch.dict("os.environ", {}, clear=True),
            patch.object(oura_authorize, "HTTPServer", FakeServer),
            patch.object(oura_authorize, "UrllibTransport", return_value=self.transport),
            patch.object(oura_authorize.secrets, "token_urlsafe", return_value="expected") as state,
            patch.object(oura_authorize.webbrowser, "open") as browser,
            redirect_stdout(stdout), redirect_stderr(stderr),
        ):
            code = oura_authorize.main([
                "--no-browser", "--env-file", str(env_file), "--token-file", str(self.store.path),
            ])
        self.assertEqual(code, 0)
        self.assertEqual(requests, [1])
        state.assert_called_once_with(16)
        browser.assert_not_called()
        url, summary = stdout.getvalue().splitlines()
        self.assertEqual(parse_qs(urlsplit(url).query), {
            "response_type": ["code"], "client_id": [CREDENTIALS.client_id],
            "redirect_uri": [oura_authorize.DEFAULT_REDIRECT_URI], "scope": ["daily"], "state": ["expected"],
        })
        self.assertEqual(json.loads(summary)["status"], "ok")
        for secret in (ACCESS_TOKEN, REFRESH_TOKEN, CODE, CREDENTIALS.client_secret):
            self.assertNotIn(secret, stdout.getvalue() + stderr.getvalue())


if __name__ == "__main__":
    unittest.main()

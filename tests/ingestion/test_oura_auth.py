from __future__ import annotations

from datetime import UTC, datetime, timedelta
import fcntl
from io import BytesIO
import json
import os
from pathlib import Path
from stat import S_IMODE
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs

from src.config.env_file import read_env_file, resolve_env
from src.ingestion import oura_auth
from src.ingestion.oura_auth import (
    OuraAuthError,
    OuraCredentials,
    OuraTokens,
    OuraTransportError,
    TokenStore,
    UrllibTransport,
    ensure_access_token,
    exchange_authorization_code,
    load_oura_credentials,
    redact_secrets,
    refresh_tokens,
    write_private_json,
)

NOW = datetime(2026, 9, 10, 15, 30, tzinfo=UTC)
OLD_ACCESS = "TEST_ACCESS_PREVIOUS_do_not_disclose"
OLD_REFRESH = "TEST_REFRESH_PREVIOUS_do_not_disclose"
NEW_ACCESS = "TEST_ACCESS_SUCCESSOR_do_not_disclose"
NEW_REFRESH = "TEST_REFRESH_SUCCESSOR_do_not_disclose"
CLIENT_SECRET = "fixture-client-secret"
CREDENTIALS = OuraCredentials("test-client-id", CLIENT_SECRET)


def tokens(*, expires_at: datetime = NOW + timedelta(days=1)) -> OuraTokens:
    return OuraTokens(OLD_ACCESS, OLD_REFRESH, "Bearer", "daily", NOW - timedelta(days=1), expires_at)


def token_response() -> dict[str, object]:
    return {
        "access_token": NEW_ACCESS,
        "refresh_token": NEW_REFRESH,
        "token_type": "Bearer",
        "scope": "daily",
        "expires_in": 2592000,
    }


class FakeTransport:
    def __init__(self, status: int = 200, body: object | None = None):
        self.status = status
        self.body = token_response() if body is None else body
        self.calls: list[tuple[str, dict[str, str], float]] = []

    def post_form(self, url: str, fields: dict[str, str], *, timeout: float):
        self.calls.append((url, dict(fields), timeout))
        return self.status, self.body


class EnvironmentFileTests(unittest.TestCase):
    def test_whitelist_matches_mood_parser_and_nonempty_process_env_wins(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "settings.env"
            path.write_text(
                "# comment\ninvalid\nexport OURA_CLIENT_ID = 'file-id'\n"
                'OURA_CLIENT_SECRET="file-secret"\nIGNORED=private-value\n'
                "HOME_TIMEZONE = America/Toronto # literal inline comment\n",
                encoding="utf-8",
            )
            keys = ("OURA_CLIENT_ID", "OURA_CLIENT_SECRET", "MISSING")
            self.assertEqual(
                read_env_file(path, keys),
                {"OURA_CLIENT_ID": "file-id", "OURA_CLIENT_SECRET": "file-secret"},
            )
            self.assertEqual(
                resolve_env(iter(keys), env={"OURA_CLIENT_ID": " env-id ", "OURA_CLIENT_SECRET": " "}, env_file=path),
                {"OURA_CLIENT_ID": "env-id", "OURA_CLIENT_SECRET": "file-secret", "MISSING": ""},
            )
            with patch.dict(os.environ, {"OURA_CLIENT_ID": "env-id"}, clear=True):
                self.assertEqual(load_oura_credentials(path), OuraCredentials("env-id", "file-secret"))

    def test_missing_file_and_credentials(self) -> None:
        with TemporaryDirectory() as directory:
            path = Path(directory) / "absent.env"
            self.assertEqual(read_env_file(path, ["OURA_CLIENT_ID"]), {})
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(OuraAuthError, "^OURA_CLIENT_ID and OURA_CLIENT_SECRET are required$"):
                    load_oura_credentials(path)


class TokenStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.root = Path(self.directory.name).resolve()
        self.store = TokenStore(self.root / "secrets" / "oura_tokens.json")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_round_trip_and_private_modes(self) -> None:
        self.assertIsNone(self.store.load())
        self.store.save(tokens())
        self.assertEqual(self.store.load(), tokens())
        self.assertEqual(S_IMODE(self.store.path.stat().st_mode), 0o600)
        self.assertEqual(S_IMODE(self.store.path.parent.stat().st_mode), 0o700)
        self.assertEqual(list(self.store.path.parent.iterdir()), [self.store.path])
        stored = json.loads(self.store.path.read_text())
        self.assertEqual(stored["obtained_at_utc"], "2026-09-09T15:30:00+00:00")

    def test_stale_temporaries_are_removed_but_active_writers_are_left_alone(self) -> None:
        self.store.path.parent.mkdir(mode=0o700)
        name = self.store.path.name
        stale = self.store.path.with_name(f".{name}.12345.deadbeef.tmp")
        stale.write_text("half-written", encoding="utf-8")
        os.utime(stale, (0, 0))
        active = self.store.path.with_name(f".{name}.67890.cafef00d.tmp")
        active.write_text("in-progress", encoding="utf-8")
        unrelated = self.store.path.parent / "other.json"
        unrelated.write_text("{}", encoding="utf-8")

        self.store.save(tokens())

        self.assertEqual(self.store.load(), tokens())
        self.assertFalse(stale.exists())
        self.assertTrue(active.exists())
        self.assertTrue(unrelated.exists())
        self.assertEqual(
            sorted(item.name for item in self.store.path.parent.iterdir()),
            sorted([self.store.path.name, active.name, unrelated.name]),
        )

    def test_symlink_token_target_is_refused_for_load_and_save(self) -> None:
        destination = self.root / "destination.json"
        destination.write_text("untouched")
        self.store.path.parent.mkdir()
        self.store.path.symlink_to(destination)
        for operation in (self.store.load, lambda: self.store.save(tokens())):
            with self.assertRaises(OuraAuthError):
                operation()
        self.assertEqual(destination.read_text(), "untouched")

    def test_symlink_temporary_target_and_parent_are_refused(self) -> None:
        destination = self.root / "destination.json"
        destination.write_text("untouched")
        self.store.path.parent.mkdir()
        # Temporaries carry a per-writer random name and are created with
        # O_EXCL|O_NOFOLLOW, so a planted symlink is never followed; a planted
        # symlink matching the stale-temporary pattern is left alone as well.
        planted = self.store.path.with_name(f".{self.store.path.name}.1.00000000.tmp")
        planted.symlink_to(destination)
        with patch.object(oura_auth, "_temporary_path", return_value=planted):
            with self.assertRaises(OuraAuthError):
                self.store.save(tokens())
        self.assertEqual(destination.read_text(), "untouched")
        self.assertTrue(planted.is_symlink())
        planted.unlink()
        linked_directory = self.root / "linked"
        linked_directory.symlink_to(self.store.path.parent, target_is_directory=True)
        linked_store = TokenStore(linked_directory / "oura_tokens.json")
        with self.assertRaises(OuraAuthError):
            linked_store.save(tokens())
        with self.assertRaises(OuraAuthError):
            linked_store.load()

    def test_existing_nested_symlink_ancestor_is_refused_by_all_store_operations(self) -> None:
        real_parent = self.root / "real"
        actual_store = TokenStore(real_parent / "nested" / "oura_tokens.json")
        actual_store.save(tokens())
        old_bytes = actual_store.path.read_bytes()
        linked_parent = self.root / "linked"
        linked_parent.symlink_to(real_parent, target_is_directory=True)
        linked_store = TokenStore(linked_parent / "nested" / "oura_tokens.json")

        def take_lock() -> None:
            with linked_store.refresh_lock():
                self.fail("a token store through a symlink must not be locked")

        operations = {
            "load": linked_store.load,
            "save": lambda: linked_store.save(tokens()),
            "lock": take_lock,
            "invalidate": linked_store.invalidate,
        }
        for name, operation in operations.items():
            with self.subTest(operation=name):
                with self.assertRaises(OuraAuthError):
                    operation()
                self.assertEqual(actual_store.path.read_bytes(), old_bytes)
                self.assertEqual(list(actual_store.path.parent.iterdir()), [actual_store.path])

        missing_store = TokenStore(linked_parent / "absent" / "nested" / "oura_tokens.json")
        with self.assertRaises(OuraAuthError):
            missing_store.save(tokens())
        self.assertFalse((real_parent / "absent").exists())

    def test_malformed_files_and_naive_timestamps_are_redacted(self) -> None:
        self.store.path.parent.mkdir()
        values = [OLD_ACCESS + " not JSON", json.dumps({"access_token": OLD_ACCESS}), "[]"]
        self.store.save(tokens())
        naive_payload = json.loads(self.store.path.read_text())
        naive_payload["expires_at_utc"] = "2026-09-11T15:30:00"
        values.append(json.dumps(naive_payload))
        for value in values:
            with self.subTest(value_type=value[:1]):
                self.store.path.write_text(value)
                with self.assertRaises(OuraAuthError) as caught:
                    self.store.load()
                self.assertNotIn(OLD_ACCESS, str(caught.exception))
                self.assertNotIn(OLD_REFRESH, str(caught.exception))

    def test_save_failure_is_redacted_and_cleans_temporary_file(self) -> None:
        self.store.save(tokens())
        old_bytes = self.store.path.read_bytes()
        with patch.object(oura_auth.os, "replace", side_effect=OSError(OLD_REFRESH)):
            with self.assertRaises(OuraAuthError) as caught:
                self.store.save(tokens())
        self.assertNotIn(OLD_REFRESH, str(caught.exception))
        self.assertEqual(self.store.path.read_bytes(), old_bytes)
        self.assertFalse(self.store.path.with_suffix(".json.tmp").exists())

    def test_private_json_is_atomic_on_serialization_error(self) -> None:
        self.store.save(tokens())
        old_bytes = self.store.path.read_bytes()
        with self.assertRaises(OuraAuthError):
            write_private_json(self.store.path, {"invalid": object()})
        self.assertEqual(self.store.path.read_bytes(), old_bytes)

    def test_refresh_directory_lock_excludes_another_holder_without_a_file(self) -> None:
        with self.store.refresh_lock():
            descriptor = os.open(self.store.path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                with self.assertRaises(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(descriptor)
        self.assertEqual(list(self.store.path.parent.iterdir()), [])


class OAuthTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.store = TokenStore(Path(self.directory.name).resolve() / "secrets" / "oura_tokens.json")

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_expiry_skew_is_inclusive(self) -> None:
        value = tokens(expires_at=NOW + timedelta(seconds=300))
        self.assertTrue(value.is_expiring(NOW))
        self.assertFalse(value.is_expiring(NOW - timedelta(seconds=1)))
        self.assertFalse(value.is_expiring(NOW, skew_seconds=299))
        self.assertTrue(value.is_expiring(NOW + timedelta(seconds=300), skew_seconds=0))

    def test_refresh_rotates_and_persists_before_return(self) -> None:
        self.store.save(tokens(expires_at=NOW))
        transport = FakeTransport()
        save = self.store.save
        saved = []

        def observe_save(value: OuraTokens) -> None:
            save(value)
            self.assertEqual(self.store.load(), value)
            saved.append(value)

        with patch.object(self.store, "save", side_effect=observe_save):
            value = ensure_access_token(self.store, CREDENTIALS, transport, now=NOW)
        self.assertEqual(saved, [value])
        self.assertEqual(value.access_token, NEW_ACCESS)
        self.assertEqual(value.refresh_token, NEW_REFRESH)
        self.assertEqual(value.expires_at_utc, NOW + timedelta(seconds=2592000))
        self.assertEqual(transport.calls[0][0], "https://api.ouraring.com/oauth/token")
        self.assertEqual(transport.calls[0][1], {
            "grant_type": "refresh_token", "refresh_token": OLD_REFRESH,
            "client_id": "test-client-id", "client_secret": CLIENT_SECRET,
        })
        self.assertEqual(transport.calls[0][2], 20)
        self.assertEqual(list(self.store.path.parent.iterdir()), [self.store.path])

    def test_unexpired_token_is_reused_unless_forced(self) -> None:
        self.store.save(tokens())
        transport = FakeTransport()
        self.assertEqual(ensure_access_token(self.store, CREDENTIALS, transport, now=NOW), tokens())
        self.assertEqual(transport.calls, [])
        value = ensure_access_token(self.store, CREDENTIALS, transport, now=NOW, force_refresh=True)
        self.assertEqual(value.refresh_token, NEW_REFRESH)
        self.assertEqual(len(transport.calls), 1)

    def test_missing_store_requires_authorization_without_request(self) -> None:
        transport = FakeTransport()
        with self.assertRaisesRegex(OuraAuthError, "scripts/oura_authorize.py"):
            ensure_access_token(self.store, CREDENTIALS, transport, now=NOW)
        self.assertEqual(transport.calls, [])

    def test_rejected_refresh_leaves_old_file_untouched(self) -> None:
        for status in (400, 401):
            with self.subTest(status=status):
                self.store.save(tokens(expires_at=NOW))
                old_bytes = self.store.path.read_bytes()
                transport = FakeTransport(status, {"error": OLD_ACCESS + OLD_REFRESH + CLIENT_SECRET})
                with self.assertRaisesRegex(OuraAuthError, "scripts/oura_authorize.py") as caught:
                    ensure_access_token(self.store, CREDENTIALS, transport, now=NOW)
                self.assertEqual(self.store.path.read_bytes(), old_bytes)
                for secret in (OLD_ACCESS, OLD_REFRESH, CLIENT_SECRET):
                    self.assertNotIn(secret, str(caught.exception))

    def test_non_success_and_transport_exception_have_safe_messages(self) -> None:
        for transport in (FakeTransport(500, {"error": CLIENT_SECRET}), FakeTransport(429)):
            with self.assertRaisesRegex(OuraAuthError, "^Oura token refresh failed$"):
                refresh_tokens(CREDENTIALS, tokens(), transport, now=NOW)
        transport = FakeTransport()
        with patch.object(transport, "post_form", side_effect=RuntimeError(OLD_REFRESH)):
            with self.assertRaisesRegex(OuraTransportError, "^Oura token refresh request failed$") as caught:
                refresh_tokens(CREDENTIALS, tokens(), transport, now=NOW)
        self.assertNotIn(OLD_REFRESH, str(caught.exception))

    def test_consumed_refresh_is_removed_when_success_response_is_invalid(self) -> None:
        for field in ("access_token", "refresh_token", "expires_in"):
            with self.subTest(field=field):
                self.store.save(tokens(expires_at=NOW))
                body = token_response()
                del body[field]
                with self.assertRaisesRegex(OuraAuthError, "scripts/oura_authorize.py"):
                    ensure_access_token(self.store, CREDENTIALS, FakeTransport(body=body), now=NOW)
                self.assertIsNone(self.store.load())

    def test_reused_refresh_token_is_not_saved_as_successor(self) -> None:
        self.store.save(tokens(expires_at=NOW))
        body = token_response()
        body["refresh_token"] = OLD_REFRESH
        with self.assertRaises(OuraAuthError):
            ensure_access_token(self.store, CREDENTIALS, FakeTransport(body=body), now=NOW)
        self.assertIsNone(self.store.load())

    def test_consumed_refresh_is_removed_when_save_fails(self) -> None:
        self.store.save(tokens(expires_at=NOW))
        with patch.object(self.store, "save", side_effect=OSError(NEW_REFRESH)):
            with self.assertRaises(OuraAuthError) as caught:
                ensure_access_token(self.store, CREDENTIALS, FakeTransport(), now=NOW)
        for secret in (OLD_ACCESS, OLD_REFRESH, NEW_ACCESS, NEW_REFRESH, CLIENT_SECRET):
            self.assertNotIn(secret, str(caught.exception))
        self.assertIsNone(self.store.load())

    def test_transport_failure_during_refresh_keeps_the_stored_tokens_for_retry(self) -> None:
        # A request that never completed (DNS, socket, timeout) cannot have
        # consumed the single-use refresh token, so the file must survive and
        # the failure must not be reported as "authorization required".
        self.store.save(tokens(expires_at=NOW))
        transport = FakeTransport()
        with patch.object(transport, "post_form", side_effect=OSError(OLD_REFRESH)):
            with self.assertRaises(OuraTransportError) as caught:
                ensure_access_token(self.store, CREDENTIALS, transport, now=NOW)
        self.assertNotIsInstance(caught.exception, OuraAuthError)
        self.assertNotIn(OLD_REFRESH, str(caught.exception))
        self.assertEqual(self.store.load(), tokens(expires_at=NOW))

    def test_invalid_success_body_after_refresh_does_not_retain_a_possibly_consumed_token(self) -> None:
        self.store.save(tokens(expires_at=NOW))
        with self.assertRaises(OuraAuthError):
            ensure_access_token(self.store, CREDENTIALS, FakeTransport(200, {"unexpected": True}), now=NOW)
        self.assertIsNone(self.store.load())

    def test_authorization_code_exchange(self) -> None:
        transport = FakeTransport()
        value = exchange_authorization_code(CREDENTIALS, "test-code", "http://localhost:8765/oauth/oura/callback", transport, now=NOW)
        self.assertEqual(value.access_token, NEW_ACCESS)
        self.assertEqual(transport.calls[0][1], {
            "grant_type": "authorization_code", "code": "test-code",
            "redirect_uri": "http://localhost:8765/oauth/oura/callback",
            "client_id": "test-client-id", "client_secret": CLIENT_SECRET,
        })
        for transport in (FakeTransport(400, {"error": CLIENT_SECRET}), FakeTransport(200, {})):
            with self.assertRaises(OuraAuthError) as caught:
                exchange_authorization_code(CREDENTIALS, OLD_ACCESS, "http://localhost", transport, now=NOW)
            self.assertNotIn(OLD_ACCESS, str(caught.exception))
            self.assertNotIn(CLIENT_SECRET, str(caught.exception))

    def test_redaction_handles_overlapping_and_empty_secrets(self) -> None:
        self.assertEqual(redact_secrets("abcde abc abcde", ("abc", "abcde", "")), "[REDACTED] [REDACTED] [REDACTED]")
        self.assertNotIn(OLD_ACCESS, repr(tokens()))
        self.assertNotIn(CLIENT_SECRET, repr(CREDENTIALS))


class UrllibTransportTests(unittest.TestCase):
    def test_http_error_is_returned_as_json_and_form_is_encoded(self) -> None:
        error = HTTPError("https://example.invalid", 401, "Unauthorized", {}, BytesIO(b'{"error":"invalid_grant"}'))
        opener = Mock()
        opener.open.side_effect = error
        with patch.object(oura_auth, "build_opener", return_value=opener):
            self.assertEqual(UrllibTransport().post_form("https://example.invalid", {"code": "a b&c"}), (401, {"error": "invalid_grant"}))
        request = opener.open.call_args.args[0]
        self.assertEqual(parse_qs(request.data.decode()), {"code": ["a b&c"]})
        self.assertEqual(opener.open.call_args.kwargs, {"timeout": 20.0})

    def test_non_json_http_error_and_network_error_never_leak_in_exceptions(self) -> None:
        opener = Mock()
        opener.open.side_effect = HTTPError("https://example.invalid", 500, "error", {}, BytesIO(b"unavailable"))
        with patch.object(oura_auth, "build_opener", return_value=opener):
            self.assertEqual(UrllibTransport().get_json("https://example.invalid", headers={}), (500, "unavailable"))
        opener.open.side_effect = URLError(OLD_ACCESS)
        with patch.object(oura_auth, "build_opener", return_value=opener):
            with self.assertRaisesRegex(RuntimeError, "^Oura request failed$") as caught:
                UrllibTransport().get_json("https://example.invalid", headers={"Authorization": "Bearer " + OLD_ACCESS})
        self.assertNotIn(OLD_ACCESS, str(caught.exception))


if __name__ == "__main__":
    unittest.main()

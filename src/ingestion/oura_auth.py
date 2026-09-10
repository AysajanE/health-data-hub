"""Private Oura OAuth storage and exchanges. This module never logs payloads."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import fcntl
import json
import math
import os
from pathlib import Path
import secrets
import stat
import time
from typing import Any, Iterable, Iterator, Mapping, Protocol
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from src.config.env_file import DEFAULT_ENV_FILE, REPO_ROOT, resolve_env

DEFAULT_TOKEN_PATH = REPO_ROOT / "data" / "secrets" / "oura_tokens.json"
TOKEN_ENDPOINT = "https://api.ouraring.com/oauth/token"
AUTHORIZATION_REQUIRED = "Oura authorization is required; run scripts/oura_authorize.py"
HTTP_TIMEOUT_SECONDS = 20.0


class OuraAuthError(RuntimeError):
    """A safe OAuth failure message, with no provider response attached."""

    def __init__(self, message: str, *, refresh_consumed: bool = False):
        super().__init__(message)
        self.refresh_consumed = refresh_consumed


class OuraTransportError(RuntimeError):
    """The token request never completed; the stored refresh token is kept for retry."""


@dataclass(frozen=True)
class OuraCredentials:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)


def load_oura_credentials(env_file: Path = DEFAULT_ENV_FILE) -> OuraCredentials:
    try:
        settings = resolve_env(("OURA_CLIENT_ID", "OURA_CLIENT_SECRET"), env_file=env_file)
    except Exception:
        raise OuraAuthError("Oura credentials could not be read") from None
    if not all(settings.values()):
        raise OuraAuthError("OURA_CLIENT_ID and OURA_CLIENT_SECRET are required")
    return OuraCredentials(settings["OURA_CLIENT_ID"], settings["OURA_CLIENT_SECRET"])


@dataclass(frozen=True)
class OuraTokens:
    access_token: str = field(repr=False)
    refresh_token: str = field(repr=False)
    token_type: str
    scope: str
    obtained_at_utc: datetime
    expires_at_utc: datetime

    def is_expiring(self, now: datetime, skew_seconds: int = 300) -> bool:
        return _utc(self.expires_at_utc) <= _utc(now) + timedelta(seconds=skew_seconds)


def _utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise OuraAuthError("Oura token timestamp is invalid")
    return value.astimezone(UTC)


def _token_payload(tokens: OuraTokens) -> dict[str, str]:
    values = {
        "access_token": tokens.access_token,
        "refresh_token": tokens.refresh_token,
        "token_type": tokens.token_type,
        "scope": tokens.scope,
    }
    if any(not isinstance(value, str) or not value.strip() for value in values.values()):
        raise OuraAuthError("Oura token file is invalid")
    return {
        **values,
        "obtained_at_utc": _utc(tokens.obtained_at_utc).isoformat(timespec="seconds"),
        "expires_at_utc": _utc(tokens.expires_at_utc).isoformat(timespec="seconds"),
    }


def _private_parent(path: Path) -> None:
    _refuse_symlink(path.parent)
    if not path.parent.exists():
        _private_parent(path.parent)
        path.parent.mkdir(mode=0o700, exist_ok=True)
    if path.parent.is_symlink() or not path.parent.is_dir():
        raise OuraAuthError("Private file parent is invalid")


def _refuse_symlink(path: Path) -> None:
    absolute = path.absolute()
    # Check from the root down, so no descendant is inspected through a link.
    for candidate in reversed((absolute, *absolute.parents)):
        if candidate.is_symlink():
            raise OuraAuthError("Private file symlinks are not allowed")


STALE_TEMPORARY_AGE_SECONDS = 3600.0


def _temporary_path(path: Path) -> Path:
    """A per-writer temporary name, so concurrent writers never share or delete each other's file."""
    return path.with_name(f".{path.name}.{os.getpid()}.{secrets.token_hex(4)}.tmp")


def _remove_stale_temporaries(path: Path, *, now: float | None = None) -> None:
    """Remove leftover temporaries from crashed writers; active writers are far younger."""
    current = time.time() if now is None else now
    prefix = f".{path.name}."
    for candidate in path.parent.iterdir():
        if not candidate.name.startswith(prefix) or not candidate.name.endswith(".tmp"):
            continue
        try:
            info = os.lstat(candidate)
        except FileNotFoundError:
            continue
        if stat.S_ISREG(info.st_mode) and current - info.st_mtime > STALE_TEMPORARY_AGE_SECONDS:
            try:
                os.unlink(candidate)
            except FileNotFoundError:
                pass


def write_private_json(path: Path, payload: Mapping[str, Any]) -> None:
    """Atomically replace a private JSON file, refusing symlink destinations."""
    path = Path(path)
    temporary = _temporary_path(path)
    created = False
    try:
        _private_parent(path)
        _refuse_symlink(path)
        _refuse_symlink(temporary)
        _remove_stale_temporaries(path)
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        created = True
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(payload, stream, sort_keys=True, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _refuse_symlink(path)
        os.replace(temporary, path)
        created = False
        os.chmod(path, 0o600, follow_symlinks=False)
        parent_fd = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    except Exception:
        raise OuraAuthError("Private JSON file could not be saved") from None
    finally:
        if created:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


class TokenStore:
    def __init__(self, path: Path = DEFAULT_TOKEN_PATH):
        self.path = Path(path)

    def load(self) -> OuraTokens | None:
        try:
            _refuse_symlink(self.path)
            try:
                descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
            except FileNotFoundError:
                return None
            with os.fdopen(descriptor, "r", encoding="utf-8") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise ValueError
                payload = json.load(stream)
            tokens = OuraTokens(
                access_token=payload["access_token"],
                refresh_token=payload["refresh_token"],
                token_type=payload["token_type"],
                scope=payload["scope"],
                obtained_at_utc=_utc(datetime.fromisoformat(payload["obtained_at_utc"])),
                expires_at_utc=_utc(datetime.fromisoformat(payload["expires_at_utc"])),
            )
            _token_payload(tokens)
            return tokens
        except Exception:
            raise OuraAuthError("Oura token file could not be read or is invalid") from None

    def save(self, tokens: OuraTokens) -> None:
        write_private_json(self.path, _token_payload(tokens))

    @contextmanager
    def refresh_lock(self) -> Iterator[None]:
        """Serialize load/refresh/save without a retained second credential file."""
        descriptor = None
        try:
            _refuse_symlink(self.path)
            _private_parent(self.path)
            descriptor = os.open(self.path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        except Exception:
            if descriptor is not None:
                os.close(descriptor)
            raise OuraAuthError("Oura token store could not be locked") from None
        try:
            yield
        finally:
            os.close(descriptor)

    def invalidate(self) -> None:
        """Remove an unusable refresh token when its successor cannot be saved."""
        try:
            _refuse_symlink(self.path)
            self.path.unlink(missing_ok=True)
        except Exception:
            raise OuraAuthError("Oura consumed token could not be removed; authorization is required") from None


class Transport(Protocol):
    def post_form(self, url: str, fields: Mapping[str, str], *, timeout: float) -> tuple[int, Any]: ...

    def get_json(self, url: str, *, headers: Mapping[str, str], timeout: float) -> tuple[int, Any]: ...


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req: Any, fp: Any, code: int, msg: str, headers: Any, newurl: str) -> None:
        return None


class UrllibTransport:
    def _request(self, request: Request, timeout: float) -> tuple[int, Any]:
        try:
            try:
                response = build_opener(_NoRedirect).open(request, timeout=timeout)
            except HTTPError as error:
                response = error
            with response:
                status = response.code
                raw = response.read().decode("utf-8", errors="replace")
            try:
                body = json.loads(raw)
            except ValueError:
                body = raw
            return status, body
        except Exception:
            raise RuntimeError("Oura request failed") from None

    def post_form(
        self, url: str, fields: Mapping[str, str], *, timeout: float = HTTP_TIMEOUT_SECONDS
    ) -> tuple[int, Any]:
        request = Request(
            url,
            data=urlencode(fields).encode("utf-8"),
            headers={"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"},
            method="POST",
        )
        return self._request(request, timeout)

    def get_json(
        self, url: str, *, headers: Mapping[str, str], timeout: float = HTTP_TIMEOUT_SECONDS
    ) -> tuple[int, Any]:
        return self._request(Request(url, headers=dict(headers), method="GET"), timeout)


def _response_tokens(body: Any, *, now: datetime) -> OuraTokens:
    try:
        expires_in = body["expires_in"]
        if isinstance(expires_in, bool) or not isinstance(expires_in, (int, float)):
            raise ValueError
        if not math.isfinite(expires_in) or expires_in <= 0:
            raise ValueError
        tokens = OuraTokens(
            access_token=body["access_token"],
            refresh_token=body["refresh_token"],
            token_type=body.get("token_type", "Bearer"),
            scope=body.get("scope", "daily"),
            obtained_at_utc=_utc(now),
            expires_at_utc=_utc(now) + timedelta(seconds=expires_in),
        )
        _token_payload(tokens)
        return tokens
    except Exception:
        raise OuraAuthError(AUTHORIZATION_REQUIRED) from None


def refresh_tokens(
    credentials: OuraCredentials, tokens: OuraTokens, transport: Transport, *, now: datetime
) -> OuraTokens:
    try:
        status, body = transport.post_form(
            TOKEN_ENDPOINT,
            {
                "grant_type": "refresh_token",
                "refresh_token": tokens.refresh_token,
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except Exception:
        # The request did not complete (DNS, socket, timeout). Keep the stored
        # refresh token so the next scheduled run can retry; if Oura did consume
        # it, that run gets 400/401 and reports that authorization is required.
        raise OuraTransportError("Oura token refresh request failed") from None
    if status in {400, 401}:
        raise OuraAuthError(AUTHORIZATION_REQUIRED)
    if status != 200:
        raise OuraAuthError("Oura token refresh failed")
    try:
        successor = _response_tokens(body, now=now)
        if successor.refresh_token == tokens.refresh_token:
            raise OuraAuthError(AUTHORIZATION_REQUIRED)
        return successor
    except OuraAuthError:
        raise OuraAuthError(AUTHORIZATION_REQUIRED, refresh_consumed=True) from None


def ensure_access_token(
    store: TokenStore,
    credentials: OuraCredentials,
    transport: Transport,
    *,
    now: datetime | None = None,
    force_refresh: bool = False,
) -> OuraTokens:
    now = _utc(now or datetime.now(UTC))
    with store.refresh_lock():
        tokens = store.load()
        if tokens is None:
            raise OuraAuthError(AUTHORIZATION_REQUIRED)
        if not force_refresh and not tokens.is_expiring(now):
            return tokens
        try:
            successor = refresh_tokens(credentials, tokens, transport, now=now)
        except OuraAuthError as error:
            if error.refresh_consumed:
                store.invalidate()
            raise
        try:
            store.save(successor)
        except Exception:
            store.invalidate()
            raise OuraAuthError("Oura refreshed tokens could not be saved; " + AUTHORIZATION_REQUIRED) from None
        return successor


def exchange_authorization_code(
    credentials: OuraCredentials,
    code: str,
    redirect_uri: str,
    transport: Transport,
    *,
    now: datetime,
) -> OuraTokens:
    try:
        status, body = transport.post_form(
            TOKEN_ENDPOINT,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
                "client_id": credentials.client_id,
                "client_secret": credentials.client_secret,
            },
            timeout=HTTP_TIMEOUT_SECONDS,
        )
    except Exception:
        raise OuraAuthError("Oura authorization exchange failed") from None
    if status != 200:
        raise OuraAuthError("Oura authorization exchange failed")
    return _response_tokens(body, now=now)


def redact_secrets(text: str, secrets: Iterable[str]) -> str:
    result = str(text)
    for secret in sorted({value for value in secrets if value}, key=len, reverse=True):
        result = result.replace(secret, "[REDACTED]")
    return result

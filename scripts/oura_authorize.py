#!/usr/bin/env python3
"""Authorize Oura once and save rotating OAuth credentials in a private file."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer
import ipaddress
import json
from pathlib import Path
import secrets
import socket
import sys
from urllib.parse import parse_qs, urlencode, urlsplit
import webbrowser

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config.env_file import DEFAULT_ENV_FILE, resolve_env
from src.ingestion.oura_auth import (
    DEFAULT_TOKEN_PATH,
    OuraAuthError,
    OuraCredentials,
    TokenStore,
    Transport,
    UrllibTransport,
    exchange_authorization_code,
    load_oura_credentials,
)


AUTHORIZE_URL = "https://cloud.ouraring.com/oauth/authorize"
DEFAULT_REDIRECT_URI = "http://localhost:8765/oauth/oura/callback"


def parse_callback(path: str, expected_state: str) -> str:
    """Validate the one-time state and return exactly one nonempty code."""
    try:
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise ValueError
        query = parse_qs(parsed.query, keep_blank_values=True, strict_parsing=True)
        state, code = query.get("state", []), query.get("code", [])
        if (
            "error" in query
            or len(state) != 1
            or len(code) != 1
            or not code[0].strip()
            or not expected_state
            or not secrets.compare_digest(state[0].encode(), expected_state.encode())
        ):
            raise ValueError
        return code[0]
    except (ValueError, UnicodeError):
        raise OuraAuthError("Invalid Oura authorization callback") from None


def complete_authorization(
    code: str,
    *,
    credentials: OuraCredentials,
    redirect_uri: str,
    transport: Transport,
    store: TokenStore,
    now: datetime | None = None,
) -> dict[str, str]:
    with store.refresh_lock():
        tokens = exchange_authorization_code(
            credentials,
            code,
            redirect_uri,
            transport,
            now=now or datetime.now(timezone.utc),
        )
        store.save(tokens)
    return {
        "status": "ok",
        "token_file": str(store.path),
        "expires_at_utc": tokens.expires_at_utc.isoformat(timespec="seconds"),
    }


def _redirect_address(redirect_uri: str) -> tuple[str, int, str]:
    """The one-shot callback server binds only to a loopback HTTP address."""
    try:
        parsed = urlsplit(redirect_uri)
        host = parsed.hostname
        if (
            parsed.scheme != "http"
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError
        if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
            raise ValueError
        port = parsed.port if parsed.port is not None else 80
        if not 1 <= port <= 65535:
            raise ValueError
        return host, port, parsed.path or "/"
    except ValueError:
        raise OuraAuthError("OURA_REDIRECT_URI must be a loopback HTTP callback URL") from None


def _callback_handler(
    *,
    expected_path: str,
    expected_state: str,
    credentials: OuraCredentials,
    redirect_uri: str,
    transport: Transport,
    store: TokenStore,
    result: dict[str, str],
) -> type[BaseHTTPRequestHandler]:
    class CallbackHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            try:
                if urlsplit(self.path).path != expected_path:
                    raise OuraAuthError("Invalid Oura authorization callback")
                code = parse_callback(self.path, expected_state)
                completed = complete_authorization(
                    code,
                    credentials=credentials,
                    redirect_uri=redirect_uri,
                    transport=transport,
                    store=store,
                )
            except Exception:
                result.update(status="error", error="Oura authorization failed")
                status, text = 400, "Authorization failed. You can close this tab."
            else:
                result.clear()
                result.update(completed)
                status, text = 200, "Authorization complete. You can close this tab."
            body = text.encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return CallbackHandler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_PATH)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args(argv)
    try:
        credentials = load_oura_credentials(args.env_file.expanduser())
        settings = resolve_env(("OURA_REDIRECT_URI",), env_file=args.env_file.expanduser())
        redirect_uri = settings.get("OURA_REDIRECT_URI") or DEFAULT_REDIRECT_URI
        host, port, callback_path = _redirect_address(redirect_uri)
        state = secrets.token_urlsafe(16)
        url = AUTHORIZE_URL + "?" + urlencode(
            {
                "response_type": "code",
                "client_id": credentials.client_id,
                "redirect_uri": redirect_uri,
                "scope": "daily",
                "state": state,
            }
        )
        result = {"status": "error", "error": "Oura authorization failed"}
        handler = _callback_handler(
            expected_path=callback_path,
            expected_state=state,
            credentials=credentials,
            redirect_uri=redirect_uri,
            transport=UrllibTransport(),
            store=TokenStore(args.token_file.expanduser()),
            result=result,
        )
        server_type = HTTPServer
        if ":" in host:
            class IPv6HTTPServer(HTTPServer):
                address_family = socket.AF_INET6

            server_type = IPv6HTTPServer
        with server_type((host, port), handler) as server:
            server.timeout = 300
            print(url, flush=True)
            if not args.no_browser:
                webbrowser.open(url)
            server.handle_request()
    except Exception:
        result = {"status": "error", "error": "Oura authorization failed"}
    print(json.dumps(result, sort_keys=True))
    return 0 if result["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

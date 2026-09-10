#!/usr/bin/env python3
"""Launch the home-Wi-Fi Streamlit mood form with settings from ``.env.local``.

Only four keys are read from the env file: ``LAN_BIND_IP``, ``MOOD_FORM_TOKEN``,
``HOME_TIMEZONE``, and ``HEALTH_HUB_DATABASE_PATH``. Values already present in
the process environment win. The token value is never printed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib.util
import ipaddress
import json
import os
from pathlib import Path
import sys
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.setup_permissions import setup_permissions


REPO_ROOT = Path(__file__).resolve().parents[1]
FORM_PATH = REPO_ROOT / "app" / "mood_form.py"
DEFAULT_ENV_FILE = REPO_ROOT / ".env.local"
DEFAULT_DATABASE_PATH = REPO_ROOT / "data" / "warehouse.duckdb"
DEFAULT_HOME_TIMEZONE = "America/Toronto"
DEFAULT_PORT = 8501
ENV_BIND_IP = "LAN_BIND_IP"
ENV_TOKEN = "MOOD_FORM_TOKEN"
ENV_HOME_TIMEZONE = "HOME_TIMEZONE"
ENV_DATABASE_PATH = "HEALTH_HUB_DATABASE_PATH"
ALLOWED_ENV_KEYS = (ENV_BIND_IP, ENV_TOKEN, ENV_HOME_TIMEZONE, ENV_DATABASE_PATH)


@dataclass(frozen=True)
class LaunchSettings:
    bind_ip: str
    port: int
    token: str
    home_timezone: str
    database_path: Path
    errors: tuple[str, ...]


def read_env_file(path: Path, keys: Iterable[str]) -> dict[str, str]:
    """Read only the whitelisted ``KEY=VALUE`` lines from a dotenv-style file."""

    wanted = set(keys)
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in wanted:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def resolve_settings(
    env: Mapping[str, str],
    env_file: Path,
    *,
    port: int = DEFAULT_PORT,
) -> LaunchSettings:
    file_values = read_env_file(env_file, ALLOWED_ENV_KEYS)
    merged: dict[str, str] = {}
    for key in ALLOWED_ENV_KEYS:
        process_value = (env.get(key) or "").strip()
        merged[key] = process_value if process_value else (file_values.get(key) or "").strip()

    errors: list[str] = []

    bind_ip = merged[ENV_BIND_IP]
    try:
        parsed_ip = ipaddress.ip_address(bind_ip)
    except ValueError:
        errors.append(f"{ENV_BIND_IP} must be a valid IP address")
    else:
        bind_ip = str(parsed_ip)
        if parsed_ip.is_unspecified or parsed_ip.is_multicast:
            errors.append(
                f"{ENV_BIND_IP} must be one specific interface address, not a wildcard or multicast address"
            )

    token = merged[ENV_TOKEN]
    if not token:
        errors.append(f"{ENV_TOKEN} must be set and non-empty")

    home_timezone = merged[ENV_HOME_TIMEZONE] or DEFAULT_HOME_TIMEZONE
    try:
        ZoneInfo(home_timezone)
    except (ZoneInfoNotFoundError, ValueError):
        errors.append(f"{ENV_HOME_TIMEZONE} is not a valid timezone name")

    database_value = merged[ENV_DATABASE_PATH]
    database_path = Path(database_value).expanduser() if database_value else DEFAULT_DATABASE_PATH

    if not (1 <= int(port) <= 65535):
        errors.append("port must be between 1 and 65535")

    return LaunchSettings(
        bind_ip=bind_ip,
        port=int(port),
        token=token,
        home_timezone=home_timezone,
        database_path=database_path,
        errors=tuple(errors),
    )


def build_check_summary(settings: LaunchSettings) -> dict[str, Any]:
    """Summary safe to print: it carries whether a token exists, never its value."""

    return {
        "status": "ok",
        "bind_ip": settings.bind_ip,
        "port": settings.port,
        "home_timezone": settings.home_timezone,
        "database_path": str(settings.database_path),
        "token_present": bool(settings.token),
        "form_path": str(FORM_PATH),
        "url": f"http://{settings.bind_ip}:{settings.port}",
    }


def streamlit_command(settings: LaunchSettings) -> list[str]:
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(FORM_PATH),
        "--server.address",
        settings.bind_ip,
        "--server.port",
        str(settings.port),
        "--server.headless",
        "true",
        "--browser.gatherUsageStats",
        "false",
        "--client.toolbarMode",
        "minimal",
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Launch the LAN-only Streamlit mood form.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--check", action="store_true", help="Validate settings and exit without launching.")
    args = parser.parse_args(argv)

    settings = resolve_settings(os.environ, Path(args.env_file).expanduser(), port=args.port)
    if settings.errors:
        print(json.dumps({"status": "error", "errors": list(settings.errors)}, indent=2))
        return 1

    if importlib.util.find_spec("streamlit") is None:
        print(json.dumps({"status": "error", "errors": ["streamlit is not installed in this interpreter"]}, indent=2))
        return 1

    permissions = setup_permissions(REPO_ROOT)
    if permissions.get("status") != "ok":
        print(json.dumps({"status": "error", "errors": list(permissions.get("errors", []))}, indent=2))
        return 1

    if args.check:
        print(json.dumps(build_check_summary(settings), indent=2, sort_keys=True))
        return 0

    child_env = dict(os.environ)
    child_env[ENV_TOKEN] = settings.token
    child_env[ENV_HOME_TIMEZONE] = settings.home_timezone
    child_env[ENV_DATABASE_PATH] = str(settings.database_path)

    print(f"Mood form: http://{settings.bind_ip}:{settings.port}", flush=True)
    os.execve(sys.executable, streamlit_command(settings), child_env)
    return 0  # pragma: no cover - execve does not return


if __name__ == "__main__":
    raise SystemExit(main())

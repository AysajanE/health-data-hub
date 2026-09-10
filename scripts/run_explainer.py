#!/usr/bin/env python3
"""Launch the same-host Streamlit retrospective with whitelisted settings."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import importlib.util
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.setup_permissions import setup_permissions
from src.config.env_file import resolve_env


REPO_ROOT = Path(__file__).resolve().parents[1]
EXPLAINER_PATH = REPO_ROOT / "app" / "explainer.py"
DEFAULT_ENV_FILE = REPO_ROOT / ".env.local"
DEFAULT_DATABASE_PATH = REPO_ROOT / "data" / "warehouse.duckdb"
DEFAULT_MODEL_DIR = REPO_ROOT / "models"
DEFAULT_HOME_TIMEZONE = "America/Toronto"
DEFAULT_PORT = 8502
BIND_IP = "127.0.0.1"
ENV_TOKEN = "MOOD_FORM_TOKEN"
ENV_HOME_TIMEZONE = "HOME_TIMEZONE"
ENV_DATABASE_PATH = "HEALTH_HUB_DATABASE_PATH"
ENV_MODEL_DIR = "HEALTH_HUB_MODEL_DIR"
ALLOWED_ENV_KEYS = (ENV_TOKEN, ENV_HOME_TIMEZONE, ENV_DATABASE_PATH, ENV_MODEL_DIR)


@dataclass(frozen=True)
class LaunchSettings:
    port: int
    token: str
    home_timezone: str
    database_path: Path
    model_dir: Path
    errors: tuple[str, ...]

    @property
    def bind_ip(self) -> str:
        return BIND_IP


def resolve_settings(
    env: Mapping[str, str],
    env_file: Path,
    *,
    port: int = DEFAULT_PORT,
) -> LaunchSettings:
    merged = {key: (env.get(key) or "").strip() for key in ALLOWED_ENV_KEYS}
    missing = tuple(key for key in ALLOWED_ENV_KEYS if not merged[key])
    if missing:
        merged.update(resolve_env(missing, env=env, env_file=env_file))

    errors: list[str] = []
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
    model_value = merged[ENV_MODEL_DIR]
    model_dir = Path(model_value).expanduser() if model_value else DEFAULT_MODEL_DIR

    if not (1 <= int(port) <= 65535):
        errors.append("port must be between 1 and 65535")

    return LaunchSettings(
        port=int(port),
        token=token,
        home_timezone=home_timezone,
        database_path=database_path,
        model_dir=model_dir,
        errors=tuple(errors),
    )


def build_check_summary(settings: LaunchSettings) -> dict[str, Any]:
    """Expose token presence, never its value."""
    return {
        "status": "ok",
        "bind_ip": settings.bind_ip,
        "port": settings.port,
        "home_timezone": settings.home_timezone,
        "database_path": str(settings.database_path),
        "model_dir": str(settings.model_dir),
        "token_present": bool(settings.token),
        "url": f"http://{settings.bind_ip}:{settings.port}",
    }


def streamlit_command(settings: LaunchSettings) -> list[str]:
    return [
        sys.executable,
        "-m",
        "streamlit",
        "run",
        str(EXPLAINER_PATH),
        "--server.address",
        BIND_IP,
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
    parser = argparse.ArgumentParser(description="Launch the loopback-only retrospective explainer.")
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

    # Validation must not traverse or change the runtime data directories.
    if args.check:
        print(json.dumps(build_check_summary(settings), indent=2, sort_keys=True))
        return 0

    permissions = setup_permissions(REPO_ROOT)
    if permissions.get("status") != "ok":
        print(json.dumps({"status": "error", "errors": list(permissions.get("errors", []))}, indent=2))
        return 1

    child_env = dict(os.environ)
    child_env[ENV_TOKEN] = settings.token
    child_env[ENV_HOME_TIMEZONE] = settings.home_timezone
    child_env[ENV_DATABASE_PATH] = str(settings.database_path)
    child_env[ENV_MODEL_DIR] = str(settings.model_dir)

    print(f"Explainer: http://{settings.bind_ip}:{settings.port}", flush=True)
    os.execve(sys.executable, streamlit_command(settings), child_env)
    return 0  # pragma: no cover - execve does not return


if __name__ == "__main__":
    raise SystemExit(main())

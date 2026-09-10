#!/usr/bin/env python3
"""Refresh the owner's Oura sleep records and print an aggregate sync report."""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import sys
from typing import Callable
from zoneinfo import ZoneInfo

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.config.env_file import DEFAULT_ENV_FILE, resolve_env
from src.ingestion.oura_auth import (
    DEFAULT_TOKEN_PATH,
    OuraAuthError,
    TokenStore,
    UrllibTransport,
    load_oura_credentials,
    write_private_json,
)
from src.ingestion.oura_sync import SyncReport, sync_oura
from src.warehouse.features import load_sleep_provider_policy
from src.warehouse.locking import lock_path_for_database
from src.warehouse.warehouse import DEFAULT_DATABASE_PATH


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATUS_FILE = REPO_ROOT / "data" / "oura_sync_status.json"
AUTHORIZATION_HINT = "Oura authorization is required; run scripts/oura_authorize.py"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--days", type=int, default=14)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_PATH)
    parser.add_argument("--status-file", type=Path, default=DEFAULT_STATUS_FILE)
    parser.add_argument("--json", action="store_true")
    return parser


def main(
    argv: list[str] | None = None,
    *,
    now: datetime | None = None,
    sync_fn: Callable[..., SyncReport] | None = None,
) -> int:
    args = _parser().parse_args(argv)
    started = now or datetime.now(timezone.utc)
    start_date, end_date = args.start, args.end
    status_path_is_safe = False
    try:
        if started.tzinfo is None or started.utcoffset() is None:
            raise ValueError("An aware clock is required")
        settings = resolve_env(
            ("HOME_TIMEZONE", "HEALTH_HUB_DATABASE_PATH"),
            env_file=args.env_file.expanduser(),
        )
        database_setting = settings.get("HEALTH_HUB_DATABASE_PATH")
        database_path = (
            Path(database_setting).expanduser() if database_setting else DEFAULT_DATABASE_PATH
        )
        status_path = args.status_file.expanduser()
        token_path = args.token_file.expanduser()
        status_paths = {status_path.resolve(), status_path.with_suffix(".json.tmp").resolve()}
        protected_paths = {
            token_path.resolve(),
            token_path.with_suffix(".json.tmp").resolve(),
            database_path.resolve(),
            Path(str(database_path) + ".wal").resolve(),
            lock_path_for_database(database_path).resolve(),
            args.env_file.expanduser().resolve(),
        }
        if len(status_paths) != 2 or status_paths & protected_paths:
            raise ValueError("Invalid Oura sync file configuration")
        status_path_is_safe = True
        home_tz = ZoneInfo(settings.get("HOME_TIMEZONE") or "America/Toronto")
        today_local = started.astimezone(home_tz).date()
        end_date = args.end or today_local
        start_date = args.start or (today_local - timedelta(days=args.days))
        if args.days < 0 or start_date > end_date:
            raise ValueError("Invalid sync date window")
        credentials = load_oura_credentials(args.env_file.expanduser())
        provider_policy = load_sleep_provider_policy()
        report = (sync_fn or sync_oura)(
            database_path=database_path,
            transport=UrllibTransport(),
            store=TokenStore(args.token_file.expanduser()),
            credentials=credentials,
            home_tz=home_tz,
            start_date=start_date,
            end_date=end_date,
            provider_policy=provider_policy,
            now=now,
        )
    except Exception as exc:
        report = SyncReport(
            status="auth_required" if isinstance(exc, OuraAuthError) else "error",
            start_date=start_date,
            end_date=end_date,
            started_at_utc=started,
            finished_at_utc=now or datetime.now(timezone.utc),
            error=f"{type(exc).__name__}: Oura sync setup failed",
        )

    payload = {"schema": "oura_sync_status.v1", **report.to_dict()}
    try:
        if status_path_is_safe:
            write_private_json(args.status_file.expanduser(), payload)
    except Exception as exc:
        report.status = "error"
        report.error = f"{type(exc).__name__}: Could not write Oura sync status"
        payload = {"schema": "oura_sync_status.v1", **report.to_dict()}

    if args.json:
        print(json.dumps(payload, sort_keys=True))
    else:
        print(
            f"{report.status} nights={report.main_sleep_nights} "
            f"upserted={report.rows_upserted} features={report.feature_rows_written}"
        )
    if report.status == "auth_required":
        print(AUTHORIZATION_HINT, file=sys.stderr)
        return 2
    return 0 if report.status == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

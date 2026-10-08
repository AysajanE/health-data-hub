#!/usr/bin/env python3
"""Log one evening mood rating from the command line, or report logging status.

This is a second front door to the same write path the Streamlit mood form
uses, for an assistant that asks for the day's rating in chat:

    .venv/bin/python scripts/log_mood.py --feeling 7 --energy 6
    .venv/bin/python scripts/log_mood.py --status

Log mode resolves the mood date exactly like the form (current time in
``HOME_TIMEZONE``; a rating saved before 04:00 local counts for the previous
day) and writes through ``persist_mood_entry_locked`` under the shared
warehouse lock with source ``grokbot``. A second rating for the same day
becomes current the same way a second form submission does: a new
``mood_entries`` row that links to the one it replaces. ``--date`` backfills a
missed day, limited to today or the previous seven days.

Status mode never writes. It reports whether today's mood date has a rating,
the latest rating, and the model-ready day count against the model gate,
counted with the same loader the form and the explainer use.

Each run prints one JSON line. Only ``HOME_TIMEZONE`` and
``HEALTH_HUB_DATABASE_PATH`` are read from ``.env.local``; the form's access
token is never read. Notes are stored but never printed, and failures are
reported with fixed messages so no row values or database errors reach output.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
import json
import os
from pathlib import Path
import sys
from typing import Any, Mapping, Sequence
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.api.mood_date import resolve_mood_date  # noqa: E402
from src.config.env_file import DEFAULT_ENV_FILE, resolve_env  # noqa: E402
from src.model.baseline_gate import MIN_MODEL_ROWS_FOR_GATE  # noqa: E402
from src.warehouse.locking import (  # noqa: E402
    WarehouseLockTimeout,
    lock_path_for_database,
    warehouse_write_lock,
)
from src.warehouse.warehouse import (  # noqa: E402
    DEFAULT_DATABASE_PATH,
    connect_duckdb,
    persist_mood_entry_locked,
    select_current_mood_entries,
)
from scripts.retrain_model import load_verified_feature_rows  # noqa: E402


ENV_HOME_TIMEZONE = "HOME_TIMEZONE"
ENV_DATABASE_PATH = "HEALTH_HUB_DATABASE_PATH"
SETTINGS_KEYS = (ENV_HOME_TIMEZONE, ENV_DATABASE_PATH)
DEFAULT_HOME_TIMEZONE = "America/Toronto"
MOOD_SOURCE = "grokbot"
RATING_MIN = 1
RATING_MAX = 10
BACKFILL_MAX_DAYS = 7
READ_LOCK_TIMEOUT_SECONDS = 10.0

TIMEZONE_INVALID = "HOME_TIMEZONE is not a valid timezone name"
DATE_INVALID = "date must be YYYY-MM-DD"
DATE_IN_FUTURE = "date is after today's mood date"
DATE_TOO_OLD = f"date is more than {BACKFILL_MAX_DAYS} days before today's mood date"
FEELING_REQUIRED = "feeling is required unless --status is given"
STATUS_TAKES_NO_RATING = "--status does not take --feeling, --energy, --notes, or --date"
WAREHOUSE_BUSY = "warehouse busy, try again in a few seconds"
SAVE_FAILED = "could not save the rating"
STATUS_FAILED = "could not read the warehouse"


class UsageError(ValueError):
    """A bad argument, reported with a fixed, safe message."""


@dataclass(frozen=True)
class CliSettings:
    home_timezone: ZoneInfo
    database_path: Path


def load_settings(env: Mapping[str, str], env_file: Path) -> CliSettings:
    """Read only the timezone and database path; process values win over the env file."""

    values = resolve_env(SETTINGS_KEYS, env=env, env_file=env_file)
    timezone_name = values[ENV_HOME_TIMEZONE] or DEFAULT_HOME_TIMEZONE
    try:
        home_timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise UsageError(TIMEZONE_INVALID) from error
    database_value = values[ENV_DATABASE_PATH]
    database_path = Path(database_value).expanduser() if database_value else DEFAULT_DATABASE_PATH
    return CliSettings(home_timezone=home_timezone, database_path=database_path)


def validate_rating(value: int | None, name: str, *, required: bool) -> int | None:
    if value is None:
        if required:
            raise UsageError(f"{name} is required")
        return None
    if not RATING_MIN <= int(value) <= RATING_MAX:
        raise UsageError(f"{name} must be between {RATING_MIN} and {RATING_MAX}")
    return int(value)


def parse_date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise UsageError(DATE_INVALID) from error


def resolve_target_date(now_utc: datetime, home_timezone: ZoneInfo, override: date | None) -> date:
    """Today's mood date under the 4 AM rule, or a backfill date within the allowed window."""

    today = resolve_mood_date(now_utc, home_timezone)
    if override is None:
        return today
    if override > today:
        raise UsageError(DATE_IN_FUTURE)
    if (today - override).days > BACKFILL_MAX_DAYS:
        raise UsageError(DATE_TOO_OLD)
    return override


def _count_model_ready_days(database_path: Path) -> int | None:
    """Model-ready days exactly as the form counts them; the caller holds the lock."""

    try:
        return len(load_verified_feature_rows(database_path))
    except Exception:
        return None


def read_warehouse(database_path: Path) -> tuple[list[Any], int | None]:
    """Current mood entries and the model-ready count, read under the shared lock.

    DuckDB refuses overlapping read-only and read-write connections to one file,
    so reads hold the lock while their connection is open, as the form does.
    """

    if not database_path.exists():
        return [], 0
    with warehouse_write_lock(
        lock_path_for_database(database_path),
        timeout_seconds=READ_LOCK_TIMEOUT_SECONDS,
    ):
        conn = connect_duckdb(database_path, read_only=True)
        try:
            entries = select_current_mood_entries(conn)
        finally:
            conn.close()
        model_ready_days = _count_model_ready_days(database_path)
    return entries, model_ready_days


def log_mood(
    settings: CliSettings,
    *,
    feeling: int,
    energy: int | None,
    notes: str | None = None,
    mood_date: date | None = None,
    now_utc: datetime | None = None,
) -> dict[str, Any]:
    """Persist one rating through the canonical correction flow and summarize it."""

    feeling_value = validate_rating(feeling, "feeling", required=True)
    energy_value = validate_rating(energy, "energy", required=False)
    logged_at = now_utc or datetime.now(UTC)
    target_date = resolve_target_date(logged_at, settings.home_timezone, mood_date)
    row = persist_mood_entry_locked(
        settings.database_path,
        {
            "log_id": uuid4(),
            "logged_at_utc": logged_at,
            "mood_date": target_date,
            "feeling": feeling_value,
            "energy": energy_value,
            "notes": (notes or "").strip() or None,
            "context_chips": (),
            "source": MOOD_SOURCE,
            # None lets the warehouse link this entry to the current one for the
            # day, exactly as a second form submission does.
            "supersedes_log_id": None,
        },
    )
    try:
        _, model_ready_days = read_warehouse(settings.database_path)
    except Exception:
        model_ready_days = None
    return {
        "status": "ok",
        "mode": "log",
        "mood_date": row.mood_date.isoformat(),
        "feeling": row.feeling,
        "energy": row.energy,
        "replaced_previous": row.supersedes_log_id is not None,
        "model_ready_days": model_ready_days,
        "model_ready_target": MIN_MODEL_ROWS_FOR_GATE,
    }


def read_status(settings: CliSettings, *, now_utc: datetime | None = None) -> dict[str, Any]:
    """Report today's logging state without writing anything."""

    today = resolve_mood_date(now_utc or datetime.now(UTC), settings.home_timezone)
    entries, model_ready_days = read_warehouse(settings.database_path)
    today_entry = next((entry for entry in entries if entry.mood_date == today), None)
    latest = entries[-1] if entries else None
    return {
        "status": "ok",
        "mode": "status",
        "today_mood_date": today.isoformat(),
        "today_logged": today_entry is not None,
        "today_feeling": today_entry.feeling if today_entry else None,
        "today_energy": today_entry.energy if today_entry else None,
        "latest_mood_date": latest.mood_date.isoformat() if latest else None,
        "latest_feeling": latest.feeling if latest else None,
        "latest_energy": latest.energy if latest else None,
        "days_logged": len(entries),
        "model_ready_days": model_ready_days,
        "model_ready_target": MIN_MODEL_ROWS_FOR_GATE,
    }


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Log today's mood rating or report logging status.")
    parser.add_argument("--status", action="store_true", help="Report status only; never writes.")
    parser.add_argument("--feeling", type=int, help="Overall feeling today, 1-10.")
    parser.add_argument("--energy", type=int, help="Energy today, 1-10 (optional).")
    parser.add_argument("--notes", help="Optional short note; stored, never printed.")
    parser.add_argument("--date", help="Backfill a missed day (YYYY-MM-DD, today or up to 7 days back).")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    return parser


def run(
    argv: Sequence[str] | None = None,
    *,
    env: Mapping[str, str] | None = None,
    now_utc: datetime | None = None,
) -> tuple[int, dict[str, Any]]:
    args = build_parser().parse_args(argv)
    try:
        settings = load_settings(os.environ if env is None else env, Path(args.env_file).expanduser())
        if args.status:
            if any(value is not None for value in (args.feeling, args.energy, args.notes, args.date)):
                raise UsageError(STATUS_TAKES_NO_RATING)
            try:
                return 0, read_status(settings, now_utc=now_utc)
            except WarehouseLockTimeout:
                return 1, {"status": "error", "mode": "status", "error": WAREHOUSE_BUSY}
            except Exception:
                return 1, {"status": "error", "mode": "status", "error": STATUS_FAILED}
        if args.feeling is None:
            raise UsageError(FEELING_REQUIRED)
        feeling = validate_rating(args.feeling, "feeling", required=True)
        energy = validate_rating(args.energy, "energy", required=False)
        override = parse_date(args.date)
        try:
            return 0, log_mood(
                settings,
                feeling=feeling,
                energy=energy,
                notes=args.notes,
                mood_date=override,
                now_utc=now_utc,
            )
        except UsageError:
            raise
        except WarehouseLockTimeout:
            return 1, {"status": "error", "mode": "log", "error": WAREHOUSE_BUSY}
        except Exception:
            return 1, {"status": "error", "mode": "log", "error": SAVE_FAILED}
    except UsageError as error:
        return 2, {"status": "error", "mode": "status" if args.status else "log", "error": str(error)}


def main(argv: Sequence[str] | None = None) -> int:
    code, payload = run(argv)
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())

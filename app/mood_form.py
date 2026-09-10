"""Compact home-Wi-Fi mood form for Health Data Hub.

Launch it through ``scripts/run_mood_form.py`` so it binds only to the
configured LAN address. The form writes through the canonical warehouse
correction flow with source ``manual`` under the shared write lock. It never
logs, prints, or echoes the access token or a submitted rating.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime
import os
from pathlib import Path
import secrets
import sys
from typing import Mapping, Sequence, get_args
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.api.mood_date import resolve_mood_date  # noqa: E402
from src.warehouse.locking import (  # noqa: E402
    WarehouseLockTimeout,
    lock_path_for_database,
    warehouse_write_lock,
)
from src.warehouse.models import ContextChip  # noqa: E402
from src.warehouse.warehouse import (  # noqa: E402
    DEFAULT_DATABASE_PATH,
    connect_duckdb,
    persist_mood_entry_locked,
    select_current_mood_entries,
)


ENV_TOKEN = "MOOD_FORM_TOKEN"
ENV_HOME_TIMEZONE = "HOME_TIMEZONE"
ENV_DATABASE_PATH = "HEALTH_HUB_DATABASE_PATH"
DEFAULT_HOME_TIMEZONE = "America/Toronto"
SESSION_AUTH_KEY = "mood_form_authenticated"

PAGE_TITLE = "Mood log"
MOOD_PROMPT = "How did I feel overall today?"
MOOD_ANCHORS = (
    "1 = terrible / sick / nonfunctional",
    "3 = poor",
    "5 = normal baseline",
    "7 = good",
    "10 = exceptional",
)
RATING_OPTIONS = tuple(range(1, 11))
CONTEXT_CHIP_OPTIONS: tuple[str, ...] = tuple(get_args(ContextChip))
CUTOFF_NOTE = "Logs between midnight and 4:00 AM count for the previous day."
TOKEN_LABEL = "Access token"
TOKEN_REJECTED = "Token not accepted"
TOKEN_MISSING = "MOOD_FORM_TOKEN is not configured"
TIMEZONE_INVALID = "HOME_TIMEZONE is not a valid timezone name"
PICK_FIRST = "Pick a number from 1 to 10 first."
WAREHOUSE_BUSY = "The warehouse is busy. Try again in a few seconds."
SAVE_FAILED = "Could not save the rating."
HISTORY_UNAVAILABLE = "Recent history is unavailable right now."
RECENT_DAYS = 7
MOOD_SOURCE = "manual"
SUMMARY_LOCK_TIMEOUT_SECONDS = 3.0


@dataclass(frozen=True)
class FormSettings:
    token: str
    home_timezone: ZoneInfo
    database_path: Path


@dataclass(frozen=True)
class MoodSummary:
    available: bool
    days_logged: int
    feeling_for_target: int | None
    recent: tuple[tuple[date, int, int | None], ...]


def load_settings(env: Mapping[str, str] | None = None) -> FormSettings:
    """Read the form settings from the environment. Raises ValueError with a safe message."""

    source = os.environ if env is None else env
    token = (source.get(ENV_TOKEN) or "").strip()
    if not token:
        raise ValueError(TOKEN_MISSING)

    timezone_name = (source.get(ENV_HOME_TIMEZONE) or "").strip() or DEFAULT_HOME_TIMEZONE
    try:
        home_timezone = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError) as error:
        raise ValueError(TIMEZONE_INVALID) from error

    database_value = (source.get(ENV_DATABASE_PATH) or "").strip()
    database_path = Path(database_value).expanduser() if database_value else DEFAULT_DATABASE_PATH
    return FormSettings(token=token, home_timezone=home_timezone, database_path=database_path)


def token_matches(provided: str, expected: str) -> bool:
    return secrets.compare_digest(provided.strip().encode("utf-8"), expected.encode("utf-8"))


def format_date(value: date) -> str:
    return f"{value:%A, %B} {value.day}, {value.year}"


def humanize_chip(code: str) -> str:
    return code.replace("_", " ")


def read_summary(database_path: Path, target_date: date) -> MoodSummary:
    """Read the current mood entries under the shared warehouse lock.

    DuckDB refuses overlapping read-only and read-write connections to one file,
    so even reads hold the lock while their connection is open. A busy warehouse
    yields an unavailable summary instead of an error.
    """

    if not database_path.exists():
        return MoodSummary(available=True, days_logged=0, feeling_for_target=None, recent=())
    try:
        with warehouse_write_lock(
            lock_path_for_database(database_path),
            timeout_seconds=SUMMARY_LOCK_TIMEOUT_SECONDS,
        ):
            conn = connect_duckdb(database_path, read_only=True)
            try:
                entries = select_current_mood_entries(conn)
            finally:
                conn.close()
    except Exception:
        return MoodSummary(available=False, days_logged=0, feeling_for_target=None, recent=())

    feeling_for_target = next(
        (entry.feeling for entry in entries if entry.mood_date == target_date),
        None,
    )
    recent = tuple(
        (entry.mood_date, entry.feeling, entry.energy) for entry in entries[-RECENT_DAYS:]
    )[::-1]
    return MoodSummary(
        available=True,
        days_logged=len(entries),
        feeling_for_target=feeling_for_target,
        recent=recent,
    )


def persist_mood(
    settings: FormSettings,
    *,
    feeling: int,
    energy: int | None,
    notes: str | None,
    context_chips: Sequence[str],
    logged_at_utc: datetime | None = None,
) -> date:
    """Persist one rating through the canonical correction flow and return its mood date."""

    logged_at = logged_at_utc or datetime.now(UTC)
    mood_date = resolve_mood_date(logged_at, settings.home_timezone)
    persist_mood_entry_locked(
        settings.database_path,
        {
            "log_id": uuid4(),
            "logged_at_utc": logged_at,
            "mood_date": mood_date,
            "feeling": int(feeling),
            "energy": int(energy) if energy is not None else None,
            "notes": notes or None,
            "context_chips": tuple(context_chips),
            "source": MOOD_SOURCE,
            "supersedes_log_id": None,
        },
    )
    return mood_date


def _render_token_gate(settings: FormSettings) -> bool:
    if st.session_state.get(SESSION_AUTH_KEY) is True:
        return True

    st.title(PAGE_TITLE)
    with st.form("token_form"):
        provided = st.text_input(TOKEN_LABEL, type="password")
        submitted = st.form_submit_button("Continue")
    if submitted:
        if token_matches(provided or "", settings.token):
            st.session_state[SESSION_AUTH_KEY] = True
            st.rerun()
        st.error(TOKEN_REJECTED)
    return False


def _render_summary(summary: MoodSummary) -> None:
    st.divider()
    if not summary.available:
        st.caption(HISTORY_UNAVAILABLE)
        return
    st.markdown(f"**Days logged so far:** {summary.days_logged}")
    if summary.recent:
        st.table(
            [
                {
                    "Date": entry_date.isoformat(),
                    "Feeling": feeling,
                    "Energy": "" if energy is None else energy,
                }
                for entry_date, feeling, energy in summary.recent
            ]
        )


def main() -> None:
    st.set_page_config(page_title=PAGE_TITLE, layout="centered")

    try:
        settings = load_settings()
    except ValueError as error:
        st.error(str(error))
        st.stop()

    if not _render_token_gate(settings):
        st.stop()

    st.title(PAGE_TITLE)
    target_date = resolve_mood_date(datetime.now(UTC), settings.home_timezone)
    st.markdown(f"**Recording for:** {format_date(target_date)}")
    st.caption(CUTOFF_NOTE)

    summary = read_summary(settings.database_path, target_date)
    if summary.feeling_for_target is not None:
        st.info(
            f"A rating for {format_date(target_date)} already exists. "
            "Saving again replaces it; the earlier rating is kept in history."
        )

    with st.form("mood_form"):
        feeling = st.radio(MOOD_PROMPT, options=RATING_OPTIONS, horizontal=True, index=None)
        st.caption("  \n".join(MOOD_ANCHORS))
        energy = st.radio("Energy (optional)", options=RATING_OPTIONS, horizontal=True, index=None)
        chips = st.multiselect(
            "Context (optional)",
            options=CONTEXT_CHIP_OPTIONS,
            format_func=humanize_chip,
        )
        notes = st.text_area("Notes (optional)", height=80)
        submitted = st.form_submit_button("Save")

    if submitted:
        if feeling is None:
            st.warning(PICK_FIRST)
        else:
            try:
                saved_date = persist_mood(
                    settings,
                    feeling=int(feeling),
                    energy=int(energy) if energy is not None else None,
                    notes=(notes or "").strip() or None,
                    context_chips=list(chips),
                )
            except WarehouseLockTimeout:
                st.error(WAREHOUSE_BUSY)
            except Exception:
                st.error(SAVE_FAILED)
            else:
                st.success(f"Saved. Rating recorded for {format_date(saved_date)}.")
                summary = read_summary(settings.database_path, target_date)

    _render_summary(summary)


if __name__ == "__main__":
    main()

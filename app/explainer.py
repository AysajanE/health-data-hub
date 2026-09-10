"""Same-host sleep and mood retrospective, using the nightly evaluation log."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
import os
from pathlib import Path
from statistics import median
import sys
from typing import Mapping
from zoneinfo import ZoneInfo

import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.mood_form import (  # noqa: E402
    SUMMARY_LOCK_TIMEOUT_SECONDS,
    WAREHOUSE_BUSY,
    format_date,
    load_settings,
    render_mood_form,
    render_token_gate,
)
from scripts.retrain_model import load_verified_feature_rows  # noqa: E402
from src.api.mood_date import resolve_mood_date  # noqa: E402
from src.model.baseline_gate import MIN_MODEL_ROWS_FOR_GATE  # noqa: E402
from src.model.display_gate import DisplayState, InsightView, build_insight_view  # noqa: E402
from src.model.eval_log import read_eval_records  # noqa: E402
from src.warehouse.locking import (  # noqa: E402
    WarehouseLockTimeout,
    lock_path_for_database,
    warehouse_write_lock,
)
from src.warehouse.models import DailyFeaturesRow, MoodEntryRow, SleepNightRow  # noqa: E402
from src.warehouse.warehouse import (  # noqa: E402
    connect_duckdb,
    select_current_mood_entries,
    select_daily_features,
    select_sleep_nights,
)

PAGE_TITLE = "Sleep + mood retrospective"
PROVIDER_CAPTION = "Sleep source: Oura · 8 Sleep: not active in v1 provider path"
FRESHNESS_WARNING = "Oura data is more than 36 hours old. Open the Oura app to sync the ring."
FRESHNESS_LIMIT = timedelta(hours=36)


@dataclass(frozen=True)
class ExplainerData:
    available: bool = True
    database_exists: bool = False
    message: str | None = None
    moods: tuple[MoodEntryRow, ...] = ()
    sleeps: tuple[SleepNightRow, ...] = ()
    daily_features: tuple[DailyFeaturesRow, ...] = ()
    latest_sleep_date: date | None = None
    latest_sleep_instant_utc: datetime | None = None
    latest_mood_date: date | None = None
    today_feeling: int | None = None
    insight_date_feeling: int | None = None
    model_ready_days: int = 0
    usual_values: Mapping[str, float] = field(default_factory=dict)


def _latest_sleep_instant(conn) -> datetime | None:
    """The most recent moment Oura sleep data covers, as an aware UTC datetime.

    Uses the wake time when known and falls back to noon UTC of the sleep date,
    so freshness is measured in elapsed hours rather than calendar dates.
    """
    row = conn.execute(
        """
        SELECT max(COALESCE(waketime_utc, CAST(sleep_date AS TIMESTAMP) + INTERVAL 12 HOUR))
        FROM sleep_nights
        WHERE source = ?
        """,
        ["oura"],
    ).fetchone()
    value = row[0] if row else None
    if not isinstance(value, datetime):
        return None
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def read_explainer_data(
    database_path: Path,
    *,
    home_tz: ZoneInfo,
    today: date,
    days: int = 28,
    insight_date: date | None = None,
) -> ExplainerData:
    """Read one locked snapshot; ``today`` already uses the home timezone cutoff.

    ``insight_date`` is the date the latest eval record explains; its current
    rating is read here so a cached insight is never shown for a date whose
    rating is missing or has since been corrected. Close the first connection
    before the verified-feature loader opens its own, keeping both reads inside
    the same warehouse lock.
    """
    database_path = Path(database_path)
    if not database_path.exists():
        return ExplainerData()
    start = today - timedelta(days=days - 1)
    try:
        with warehouse_write_lock(
            lock_path_for_database(database_path),
            timeout_seconds=SUMMARY_LOCK_TIMEOUT_SECONDS,
        ):
            conn = connect_duckdb(database_path, read_only=True)
            try:
                moods = select_current_mood_entries(conn, start_date=start, end_date=today)
                sleeps = select_sleep_nights(conn, start_date=start, end_date=today)
                features = select_daily_features(conn, start_date=start, end_date=today)
                latest_sleep = conn.execute(
                    "SELECT max(sleep_date) FROM sleep_nights WHERE source = ?", ["oura"]
                ).fetchone()[0]
                latest_sleep_instant = _latest_sleep_instant(conn)
                latest_mood = conn.execute("SELECT max(mood_date) FROM mood_current").fetchone()[0]
                insight_feeling = None
                if insight_date is not None:
                    insight_rows = select_current_mood_entries(
                        conn, start_date=insight_date, end_date=insight_date
                    )
                    insight_feeling = insight_rows[0].feeling if insight_rows else None
            finally:
                conn.close()
            model_rows = load_verified_feature_rows(database_path)
    except WarehouseLockTimeout:
        return ExplainerData(available=False, message=WAREHOUSE_BUSY)
    except Exception:
        return ExplainerData(available=False, message="The warehouse is unavailable right now.")

    usual = {}
    if len(model_rows) >= 7:
        usual = {
            name: float(median(row[name] for row in model_rows[-28:]))
            for name in ("total_sleep_min", "deep_sleep_pct")
        }
    return ExplainerData(
        database_exists=True,
        moods=tuple(moods),
        sleeps=tuple(sleeps),
        daily_features=tuple(features),
        latest_sleep_date=latest_sleep,
        latest_sleep_instant_utc=latest_sleep_instant,
        latest_mood_date=latest_mood,
        today_feeling=next((row.feeling for row in moods if row.mood_date == today), None),
        insight_date_feeling=insight_feeling,
        model_ready_days=len(model_rows),
        usual_values=usual,
    )


def oura_data_is_stale(data: ExplainerData, *, now_utc: datetime) -> bool:
    """True when Oura coverage is older than 36 hours, or absent while ratings exist."""
    if not data.database_exists:
        return False
    if data.latest_sleep_instant_utc is None:
        return data.latest_mood_date is not None
    return now_utc - data.latest_sleep_instant_utc > FRESHNESS_LIMIT


def load_latest_record(model_dir: Path) -> Mapping | None:
    """Newest eval record, None when the log is absent or empty, {} when unreadable."""
    try:
        records = read_eval_records(model_dir / "eval.jsonl")
    except (OSError, ValueError):
        return {}  # The pure gate turns this into a safe unavailable view.
    return records[-1] if records else None


def record_insight_date(record: Mapping | None) -> date | None:
    if not isinstance(record, Mapping):
        return None
    value = record.get("trained_through_date")
    if not isinstance(value, str):
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def timeline_rows(data: ExplainerData) -> list[dict[str, str]]:
    """Join raw Oura nights and current ratings by date without filling gaps."""
    moods = {row.mood_date: row for row in data.moods}
    sleeps = {row.sleep_date: row for row in data.sleeps}
    result = []
    for day in sorted(moods.keys() | sleeps.keys(), reverse=True):
        mood = moods.get(day)
        sleep = sleeps.get(day)
        minutes = sleep.total_sleep_min if sleep else None
        deep_pct = (
            100 * sleep.deep_min / minutes
            if sleep and minutes and sleep.deep_min is not None else None
        )
        result.append({
            "Date": day.isoformat(),
            "Feeling": str(mood.feeling) if mood else "",
            "Energy": str(mood.energy) if mood and mood.energy is not None else "",
            "Sleep": f"{minutes // 60}:{minutes % 60:02d}" if minutes is not None else "",
            "HRV ms": f"{sleep.hrv_avg_ms:.0f}" if sleep and sleep.hrv_avg_ms is not None else "",
            "Deep %": f"{deep_pct:.0f}%" if deep_pct is not None else "",
            "Score": str(sleep.sleep_score) if sleep and sleep.sleep_score is not None else "",
            "Context chips": ", ".join(mood.context_chips) if mood else "",
        })
    return result


def render_insight(view: InsightView, data: ExplainerData, today: date) -> None:
    st.subheader("Latest retrospective insight")
    if view.state == DisplayState.COLLECTING:
        st.info(view.message)
        st.progress(min(data.model_ready_days, MIN_MODEL_ROWS_FOR_GATE) / MIN_MODEL_ROWS_FOR_GATE)
    elif view.state == DisplayState.GATE_FAILED:
        st.info(view.message)
        st.caption(f"Model-ready days: {view.n_model}. Re-evaluated nightly.")
    elif view.state == DisplayState.INSUFFICIENT_STABLE_SIGNAL:
        st.info(view.message)
    elif view.state == DisplayState.UNAVAILABLE:
        st.warning(view.message)
    elif data.insight_date_feeling is None:
        # Mood-first: a cached insight is never shown for a date without a current rating.
        if view.insight_date == today:
            st.info("Log today's feeling to see its retrospective insight.")
        else:
            st.info(
                f"Log the rating for {format_date(view.insight_date)} to see its retrospective insight."
            )
    elif data.insight_date_feeling != view.logged_feeling:
        st.info(
            f"The rating for {format_date(view.insight_date)} changed after the last retrain. "
            "Tonight's retrain will refresh this insight."
        )
    else:
        st.markdown(f"**{format_date(view.insight_date)} — you rated {view.logged_feeling} / 10**")
        st.markdown("**Top model contributors**")
        st.caption("patterns associated with this rating")
        for contributor in view.contributors:
            line = (
                f"• {contributor.display_name} …… {contributor.value_text}"
                f"  {contributor.direction_text or ''}"
            )
            if contributor.low_confidence:
                line += " *(low-confidence signal)*"
            st.markdown(line)
        st.markdown("**Model-estimated change in your past data**")
        st.caption("The retrospective counterfactual is not built yet in this version.")
        st.markdown(f"**Confidence: {view.confidence_label}**")
        st.caption(
            f"90% interval {view.interval_low:.1f} to {view.interval_high:.1f} · "
            "correlation, not proven causation · may reflect unmeasured factors like stress, illness, schedule"
        )
    if (
        view.trained_through_date is not None
        and data.latest_mood_date is not None
        and view.trained_through_date < data.latest_mood_date
    ):
        st.caption(f"Tonight's retrain will cover {format_date(data.latest_mood_date)}.")


def render_timeline(data: ExplainerData) -> None:
    st.subheader("Last 28 days")
    rows = timeline_rows(data)
    if rows:
        st.table(rows)
        # A date union with explicit gaps keeps the two raw measures separate.
        mood_by_date = {row.mood_date: row.feeling for row in data.moods}
        sleep_by_date = {
            row.sleep_date: row.total_sleep_min / 60
            for row in data.sleeps if row.total_sleep_min is not None
        }
        dates = sorted(mood_by_date.keys() | {row.sleep_date for row in data.sleeps})
        st.line_chart({"Date": dates, "Feeling": [mood_by_date.get(day) for day in dates]}, x="Date", y="Feeling")
        st.line_chart({"Date": dates, "Sleep hours": [sleep_by_date.get(day) for day in dates]}, x="Date", y="Sleep hours")
    else:
        st.caption("No data yet.")
    st.caption("Raw values. These do not predict today's state.")


def main() -> None:
    st.set_page_config(page_title=PAGE_TITLE, layout="centered")
    try:
        settings = load_settings()
    except ValueError as error:
        st.error(str(error))
        st.stop()

    now_utc = datetime.now(UTC)
    today = resolve_mood_date(now_utc, settings.home_timezone)
    st.title(PAGE_TITLE)
    st.caption(PROVIDER_CAPTION)

    model_value = (os.environ.get("HEALTH_HUB_MODEL_DIR") or "").strip()
    model_dir = Path(model_value).expanduser() if model_value else REPO_ROOT / "models"
    latest_record = load_latest_record(model_dir)

    data = read_explainer_data(
        settings.database_path,
        home_tz=settings.home_timezone,
        today=today,
        insight_date=record_insight_date(latest_record),
    )
    if not data.available:
        st.warning(data.message)
        st.stop()
    st.caption(
        f"Latest Oura night: {data.latest_sleep_date or 'none'} · "
        f"Latest rating: {data.latest_mood_date or 'none'}"
    )
    if oura_data_is_stale(data, now_utc=now_utc):
        st.warning(FRESHNESS_WARNING)

    if data.today_feeling is None:
        st.subheader("Log today's feeling first")
        if render_token_gate(settings, heading=None):
            if render_mood_form(settings, heading=None, show_summary=False):
                st.rerun()
    else:
        st.caption(f"Today's rating is logged ({format_date(today)}).")

    view = build_insight_view(
        latest_record=latest_record if data.database_exists else None,
        model_ready_days=data.model_ready_days,
        usual_values=data.usual_values,
    )
    render_insight(view, data, today)
    render_timeline(data)


if __name__ == "__main__":
    main()

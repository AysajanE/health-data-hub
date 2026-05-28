from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo


LOCAL_CUTOFF = timedelta(hours=4)


def _coerce_home_timezone(home_tz: str | ZoneInfo) -> ZoneInfo:
    if isinstance(home_tz, ZoneInfo):
        return home_tz
    if isinstance(home_tz, str):
        return ZoneInfo(home_tz)
    raise TypeError("home_tz must be a zoneinfo name or ZoneInfo instance")


def resolve_mood_date(logged_at_utc: datetime, home_tz: str | ZoneInfo) -> date:
    if logged_at_utc.tzinfo is None or logged_at_utc.utcoffset() is None:
        raise ValueError("logged_at_utc must be timezone-aware")

    local_timestamp = logged_at_utc.astimezone(UTC).astimezone(_coerce_home_timezone(home_tz))
    return (local_timestamp - LOCAL_CUTOFF).date()

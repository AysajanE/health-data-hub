"""Rebuild persisted daily features in chronological order."""

from datetime import date, datetime, timedelta

from src.warehouse.features import SleepProviderPolicy
from src.warehouse.warehouse import compute_daily_features


def recompute_daily_features(
    conn,
    *,
    start_date: date,
    end_date: date,
    provider_policy: SleepProviderPolicy,
    computed_at_utc: datetime,
) -> int:
    written = 0
    current = start_date
    while current <= end_date:
        row = compute_daily_features(
            conn,
            current,
            computed_at_utc=computed_at_utc,
            provider_policy=provider_policy,
        )
        written += row is not None
        if current == end_date:
            break
        current += timedelta(days=1)
    return written

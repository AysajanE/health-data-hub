"""Oura sleep ingestion with complete fetches and aggregate-only reporting."""

from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
import math
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from pydantic import ValidationError

from src.ingestion.oura_auth import (
    OuraAuthError,
    OuraCredentials,
    TokenStore,
    Transport,
    ensure_access_token,
)
from src.warehouse.features import SleepProviderPolicy, eligible_sleep_rows_for_v1
from src.warehouse.locking import lock_path_for_database, warehouse_write_lock
from src.warehouse.models import SleepNightRow
from src.warehouse.recompute import recompute_daily_features
from src.warehouse.warehouse import (
    _checkpoint,
    _refuse_symlink_database,
    connect_duckdb,
    insert_sleep_night,
    secure_database_files,
)


OURA_API_BASE = "https://api.ouraring.com/v2/usercollection"
_SLEEP_FIELDS = frozenset({
    "day", "type", "bedtime_start", "bedtime_end", "total_sleep_duration",
    "deep_sleep_duration", "light_sleep_duration", "rem_sleep_duration",
    "awake_time", "average_hrv", "average_heart_rate",
})
_SLEEP_TYPES = frozenset({"long_sleep", "sleep", "late_nap", "rest", "deleted"})


class OuraSyncError(RuntimeError):
    """A sync failed without exposing provider data."""


def _project_record(record: dict, collection: str) -> dict:
    if collection == "daily_sleep":
        return {key: record.get(key) for key in ("day", "score", "timestamp")}
    projected = {key: record.get(key) for key in _SLEEP_FIELDS}
    readiness = record.get("readiness")
    if readiness is not None and not isinstance(readiness, dict):
        raise OuraSyncError("Invalid Oura sleep record")
    projected["readiness"] = (
        {"temperature_deviation": readiness.get("temperature_deviation")}
        if readiness is not None else None
    )
    return projected


def fetch_collection(
    transport: Transport,
    access_token: str,
    collection: str,
    start_date: date,
    end_date: date,
) -> list[dict]:
    if collection not in {"sleep", "daily_sleep"}:
        raise OuraSyncError("Unsupported Oura collection")
    records: list[dict] = []
    seen_tokens: set[str] = set()
    query = {"start_date": start_date.isoformat(), "end_date": end_date.isoformat()}
    while True:
        try:
            status, body = transport.get_json(
                f"{OURA_API_BASE}/{collection}?{urlencode(query)}",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=20,
            )
        except Exception:
            raise OuraSyncError("Oura request failed") from None
        if status == 401:
            raise OuraAuthError("Oura authorization is required; run scripts/oura_authorize.py")
        if status == 429:
            raise OuraSyncError("Oura rate limit")
        if status != 200:
            raise OuraSyncError("Oura collection request failed")
        if not isinstance(body, dict) or not isinstance(body.get("data"), list):
            raise OuraSyncError("Invalid Oura collection response")
        if "next_token" not in body:
            raise OuraSyncError("Invalid Oura pagination")
        for record in body["data"]:
            if not isinstance(record, dict):
                raise OuraSyncError("Invalid Oura collection record")
            records.append(_project_record(record, collection))
        next_token = body["next_token"]
        del body
        if next_token is None:
            return records
        if not isinstance(next_token, str) or not next_token or next_token in seen_tokens:
            raise OuraSyncError("Invalid Oura pagination")
        seen_tokens.add(next_token)
        query["next_token"] = next_token


def _timestamp(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        raise OuraSyncError("Invalid Oura sleep timestamp") from None


def _number(value: Any, *, integer: bool = False, nonnegative: bool = True):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise OuraSyncError("Invalid Oura sleep value")
    try:
        valid = math.isfinite(value) and (not nonnegative or value >= 0)
    except OverflowError:
        valid = False
    if not valid or (integer and not isinstance(value, int)):
        raise OuraSyncError("Invalid Oura sleep value")
    return value


def select_main_sleep_records(records: list[dict], home_tz: tzinfo) -> dict[date, dict]:
    selected: dict[date, dict] = {}
    for record in records:
        if not isinstance(record, dict):
            raise OuraSyncError("Invalid Oura sleep record")
        if record.get("type") != "long_sleep":
            continue
        wake_date = _timestamp(record.get("bedtime_end")).astimezone(home_tz).date()
        duration = _number(record.get("total_sleep_duration"), integer=True)
        prior = selected.get(wake_date)
        if prior is None or (duration if duration is not None else -1) > (
            prior.get("total_sleep_duration") if prior.get("total_sleep_duration") is not None else -1
        ):
            selected[wake_date] = record
    return selected


def _skipped_counts(records: list[dict], selected: dict[date, dict]) -> dict[str, int]:
    counts = Counter(
        record.get("type") if isinstance(record.get("type"), str)
        and record["type"] in _SLEEP_TYPES else "unknown"
        for record in records
    )
    counts["long_sleep"] -= len(selected)
    return {key: count for key, count in sorted(counts.items()) if count > 0}


def map_sleep_record(
    record: dict,
    *,
    home_tz: tzinfo,
    ingested_at_utc: datetime,
    sleep_score: int | None,
) -> dict:
    bedtime = _timestamp(record.get("bedtime_start"))
    waketime = _timestamp(record.get("bedtime_end"))
    readiness = record.get("readiness")
    if readiness is not None and not isinstance(readiness, dict):
        raise OuraSyncError("Invalid Oura sleep record")
    row = {
        "source": "oura",
        "sleep_date": waketime.astimezone(home_tz).date(),
        "bedtime_utc": bedtime,
        "waketime_utc": waketime,
        "ingested_at_utc": ingested_at_utc,
        "sleep_score": _number(sleep_score, integer=True),
        "body_temp_dev_c": _number(
            readiness.get("temperature_deviation") if readiness else None,
            nonnegative=False,
        ),
    }
    for source, target in (
        ("total_sleep_duration", "total_sleep_min"),
        ("rem_sleep_duration", "rem_min"),
        ("deep_sleep_duration", "deep_min"),
        ("light_sleep_duration", "light_min"),
        ("awake_time", "awake_min"),
    ):
        seconds = _number(record.get(source), integer=True)
        row[target] = int(round(seconds / 60)) if seconds is not None else None
    hrv = _number(record.get("average_hrv"))
    heart_rate = _number(record.get("average_heart_rate"))
    row["hrv_avg_ms"] = float(hrv) if hrv is not None else None
    row["rhr_avg_bpm"] = int(round(heart_rate)) if heart_rate is not None else None
    try:
        # Validate before ingestion: insert_sleep_night quarantines invalid mappings.
        return SleepNightRow.model_validate(row).model_dump()
    except ValidationError:
        raise OuraSyncError("Invalid Oura sleep record") from None


def _record_instant(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _daily_scores(records: list[dict]) -> tuple[dict[str, int | None], int]:
    """Return one score per Oura day plus the number of superseded duplicates.

    Oura re-delivers a day's daily_sleep record after a ring re-sync, so the
    same day can appear more than once with different scores. The record with
    the latest ``timestamp`` wins; ties keep the later record in delivery order.
    """

    scores: dict[str, int | None] = {}
    instants: dict[str, datetime | None] = {}
    duplicates = 0
    for record in records:
        try:
            day = record["day"]
            if not isinstance(day, str) or date.fromisoformat(day).isoformat() != day:
                raise ValueError
        except (KeyError, ValueError, TypeError):
            raise OuraSyncError("Invalid Oura daily score") from None
        score = _number(record.get("score"), integer=True)
        if score is not None and score > 100:
            raise OuraSyncError("Invalid Oura daily score")
        instant = _record_instant(record.get("timestamp"))
        if day in scores:
            duplicates += 1
            previous = instants[day]
            # A dated record always beats an undated one; among dated records
            # the latest instant wins; among undated ones the later delivery wins.
            if previous is not None and (instant is None or instant < previous):
                continue
        scores[day] = score
        instants[day] = instant
    return scores, duplicates


@dataclass
class SyncReport:
    status: str
    source: str = "oura"
    start_date: date | None = None
    end_date: date | None = None
    records_fetched: int = 0
    daily_scores_fetched: int = 0
    duplicate_daily_scores: int = 0
    main_sleep_nights: int = 0
    skipped_by_type: dict[str, int] = field(default_factory=dict)
    rows_upserted: int = 0
    earliest_changed_sleep_date: date | None = None
    recompute_start: date | None = None
    recompute_end: date | None = None
    feature_rows_written: int = 0
    checkpoint: str = "not_run"
    started_at_utc: datetime | None = None
    finished_at_utc: datetime | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            key: value.isoformat() if isinstance(value, (date, datetime)) else value
            for key, value in asdict(self).items()
        }


def _latest_dependent_date(conn, end_date: date) -> date:
    """Persisted prior-only features after the window depend on the changed history."""

    latest = end_date
    for query in (
        "SELECT MAX(sleep_date) FROM sleep_nights",
        "SELECT MAX(mood_date) FROM mood_current",
    ):
        value = conn.execute(query).fetchone()[0]
        if isinstance(value, datetime):
            value = value.date()
        if isinstance(value, date) and value > latest:
            latest = value
    return latest


def _write_rows(conn, rows: list[SleepNightRow], report: SyncReport, provider_policy, now):
    # One transaction: sleep rows and the features derived from them become
    # visible together or not at all, so a recompute failure never leaves
    # fresh sleep rows next to stale prior-only features.
    conn.execute("BEGIN")
    try:
        for row in rows:
            insert_sleep_night(conn, row)
        report.recompute_end = _latest_dependent_date(conn, report.recompute_end)
        count = recompute_daily_features(
            conn,
            start_date=report.recompute_start,
            end_date=report.recompute_end,
            provider_policy=provider_policy,
            computed_at_utc=now,
        )
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    report.rows_upserted = len(rows)
    report.feature_rows_written = count
    try:
        _checkpoint(conn)
        report.checkpoint = "ok"
    except Exception:
        report.checkpoint = "failed"


def sync_oura(
    *,
    database_path: Path,
    transport: Transport,
    store: TokenStore,
    credentials: OuraCredentials,
    home_tz: tzinfo,
    start_date: date,
    end_date: date,
    provider_policy: SleepProviderPolicy,
    now: datetime | None = None,
    lock_timeout_seconds: float = 30.0,
    recompute_lookback_days: int = 45,
) -> SyncReport:
    started = now or datetime.now(UTC)
    report = SyncReport(
        status="error", start_date=start_date, end_date=end_date, started_at_utc=started,
    )
    try:
        if start_date > end_date or recompute_lookback_days < 0:
            raise OuraSyncError("Invalid sync date window")
        eligible_sleep_rows_for_v1([], provider_policy)
        tokens = ensure_access_token(store, credentials, transport, now=started)
        fetched: dict[str, list[dict]] = {}
        refreshed = False
        for collection in ("sleep", "daily_sleep"):
            try:
                records = fetch_collection(transport, tokens.access_token, collection, start_date, end_date)
            except OuraAuthError:
                if refreshed:
                    raise
                refreshed = True
                tokens = ensure_access_token(
                    store, credentials, transport, now=started, force_refresh=True,
                )
                records = fetch_collection(transport, tokens.access_token, collection, start_date, end_date)
            fetched[collection] = records
        report.records_fetched = len(fetched["sleep"])
        report.daily_scores_fetched = len(fetched["daily_sleep"])
        selected = select_main_sleep_records(fetched["sleep"], home_tz)
        report.main_sleep_nights = len(selected)
        report.skipped_by_type = _skipped_counts(fetched["sleep"], selected)
        scores, report.duplicate_daily_scores = _daily_scores(fetched["daily_sleep"])
        rows = [
            SleepNightRow.model_validate(map_sleep_record(
                record, home_tz=home_tz, ingested_at_utc=started,
                sleep_score=scores.get(record.get("day")),
            ))
            for _, record in sorted(selected.items())
        ]
        del fetched, selected, scores, records
        report.earliest_changed_sleep_date = min((row.sleep_date for row in rows), default=None)
        lookback_start = end_date - timedelta(days=recompute_lookback_days)
        report.recompute_start = min(report.earliest_changed_sleep_date or lookback_start, lookback_start)
        report.recompute_end = end_date
        database_path = Path(database_path)
        _refuse_symlink_database(database_path)
        if any(parent.is_symlink() for parent in database_path.parents):
            raise OuraSyncError("Refusing symlink warehouse directory")
        with warehouse_write_lock(lock_path_for_database(database_path), timeout_seconds=lock_timeout_seconds):
            _refuse_symlink_database(database_path)
            conn = connect_duckdb(database_path, apply_schema=True)
            try:
                secure_database_files(database_path)
                _write_rows(conn, rows, report, provider_policy, started)
            finally:
                try:
                    conn.close()
                finally:
                    secure_database_files(database_path)
        report.status = "ok"
    except Exception as error:
        report.status = "auth_required" if isinstance(error, OuraAuthError) else "error"
        # Third-party exceptions can embed entire rows, SQL values, URLs or tokens.
        # A static message is stronger redaction than searching known secrets.
        message = (
            "Oura authorization is required; run scripts/oura_authorize.py"
            if isinstance(error, OuraAuthError) else "Oura sync failed"
        )
        if isinstance(error, OuraSyncError) and str(error) == "Oura rate limit":
            message = "Oura rate limit"
        report.error = f"{type(error).__name__}: {message}"
    report.finished_at_utc = now or datetime.now(UTC)
    return report

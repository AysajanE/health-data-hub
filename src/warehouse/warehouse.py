from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
import os
from pathlib import Path
import statistics
from typing import Any, Mapping, Sequence, TypeVar
from uuid import UUID, uuid4

import duckdb
from pydantic import ValidationError

from src.warehouse.features import SleepProviderPolicy, eligible_sleep_rows_for_v1, load_sleep_provider_policy
from src.warehouse.models import (
    DailyFeaturesRow,
    MoodEntryRow,
    SleepMergeDiagnosticsRow,
    SleepNightRow,
    ValidationFailureMetadata,
)


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "src" / "db" / "schema.sql"
DEFAULT_DATABASE_PATH = REPO_ROOT / "data" / "warehouse.duckdb"
DEFAULT_GENERAL_LOG_PATH = Path.home() / "Library" / "Logs" / "healthhub.log"
DEFAULT_QUARANTINE_DIR = REPO_ROOT / "data" / "quarantine"
DEFAULT_FEATURE_VERSION = "v1.0"
EIGHT_SLEEP_FALLBACK_IGNORED_WARNING = "8sleep_fallback_ignored"

_ModelT = TypeVar("_ModelT", SleepNightRow, MoodEntryRow, DailyFeaturesRow, SleepMergeDiagnosticsRow)

_SLEEP_NIGHT_COLUMNS = (
    "source",
    "sleep_date",
    "bedtime_utc",
    "waketime_utc",
    "total_sleep_min",
    "rem_min",
    "deep_min",
    "light_min",
    "awake_min",
    "hrv_avg_ms",
    "rhr_avg_bpm",
    "body_temp_dev_c",
    "sleep_score",
    "ingested_at_utc",
)
_MOOD_ENTRY_COLUMNS = (
    "log_id",
    "logged_at_utc",
    "mood_date",
    "feeling",
    "energy",
    "notes",
    "context_chips",
    "source",
    "supersedes_log_id",
)
_DAILY_FEATURE_COLUMNS = (
    "feature_date",
    "total_sleep_min",
    "hrv_z",
    "deep_sleep_pct",
    "prior_day_feeling",
    "hrv_avg_ms",
    "hrv_z_method",
    "feature_version",
    "prior_day_feeling_imputed",
    "sleep_source_count",
    "sleep_merge_warning",
    "computed_at_utc",
)
_DIAGNOSTICS_COLUMNS = (
    "sleep_date",
    "oura_present",
    "eight_present",
    "total_sleep_delta_min",
    "hrv_merge_method",
    "stage_source",
    "warning",
    "computed_at_utc",
)


def _coerce_row(model_type: type[_ModelT], payload: _ModelT | Mapping[str, Any]) -> _ModelT:
    if isinstance(payload, model_type):
        return payload
    return model_type.model_validate(payload)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC).isoformat()
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Path):
        return str(value)
    raise TypeError(f"unsupported JSON value: {type(value)!r}")


def _extract_validation_source(payload: Any, *, fallback: str) -> str:
    if isinstance(payload, Mapping):
        source = payload.get("source")
        if isinstance(source, str) and source.strip():
            return source
    source = getattr(payload, "source", None)
    if isinstance(source, str) and source.strip():
        return source
    return fallback


def _append_redacted_validation_log(
    general_log_path: Path,
    metadata: ValidationFailureMetadata,
) -> None:
    general_log_path.parent.mkdir(parents=True, exist_ok=True)
    with general_log_path.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(
                metadata.model_dump(mode="json"),
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        handle.write("\n")


def _write_private_json(quarantine_path: Path, payload: Any) -> None:
    quarantine_path.parent.mkdir(parents=True, exist_ok=True)
    serialized = json.dumps(
        payload,
        default=_json_default,
        indent=2,
        sort_keys=True,
    )
    file_descriptor = os.open(quarantine_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(file_descriptor, "w", encoding="utf-8") as handle:
        handle.write(serialized)
        handle.write("\n")
    os.chmod(quarantine_path, 0o600)


def _record_validation_failure(
    *,
    source: str,
    payload: Any,
    error: ValidationError,
    quarantine_dir: Path,
    general_log_path: Path,
) -> None:
    metadata = ValidationFailureMetadata.from_validation_error(
        source=source,
        payload=payload,
        error=error,
    )
    quarantine_path = quarantine_dir / f"{metadata.detected_at_utc.date().isoformat()}-{uuid4()}.json"
    _write_private_json(
        quarantine_path,
        {
            "metadata": metadata.model_dump(mode="json"),
            "payload": payload,
        },
    )
    _append_redacted_validation_log(general_log_path, metadata)


def _coerce_ingest_row(
    model_type: type[_ModelT],
    payload: _ModelT | Mapping[str, Any],
    *,
    validation_source: str,
    quarantine_dir: Path,
    general_log_path: Path,
) -> _ModelT:
    if isinstance(payload, model_type):
        return payload
    try:
        return _coerce_row(model_type, payload)
    except ValidationError as error:
        _record_validation_failure(
            source=validation_source,
            payload=payload,
            error=error,
            quarantine_dir=quarantine_dir,
            general_log_path=general_log_path,
        )
        raise


def _normalize_db_datetime(value: Any) -> Any:
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            return value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
    return value


def _normalize_db_uuid(value: Any) -> Any:
    if value is None or isinstance(value, UUID):
        return value
    return UUID(str(value))


def _row_dict(
    columns: Sequence[str],
    row: Sequence[Any],
    *,
    uuid_fields: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    data = dict(zip(columns, row, strict=True))
    for key, value in tuple(data.items()):
        normalized = _normalize_db_datetime(value)
        if key in uuid_fields:
            normalized = _normalize_db_uuid(normalized)
        data[key] = normalized
    return data


def _sleep_night_from_db(row: Sequence[Any]) -> SleepNightRow:
    return SleepNightRow.model_validate(_row_dict(_SLEEP_NIGHT_COLUMNS, row))


def _mood_entry_from_db(row: Sequence[Any]) -> MoodEntryRow:
    return MoodEntryRow.model_validate(
        _row_dict(_MOOD_ENTRY_COLUMNS, row, uuid_fields=frozenset({"log_id", "supersedes_log_id"}))
    )


def _median_absolute_deviation(values: Sequence[float], median_value: float) -> float:
    deviations = [abs(value - median_value) for value in values]
    return float(statistics.median(deviations))


def _std_fallback_z(current_value: float, history: Sequence[float]) -> tuple[float | None, str]:
    std_value = statistics.pstdev(history)
    if std_value < 1e-6:
        return None, "missing"
    mean_value = statistics.fmean(history)
    return (current_value - mean_value) / std_value, "std_fallback"


def _compute_hrv_z(
    conn: duckdb.DuckDBPyConnection,
    *,
    source: str,
    feature_date: date,
    current_value: float | None,
) -> tuple[float | None, str | None]:
    if current_value is None:
        return None, None

    recent_window_start = feature_date - timedelta(days=28)
    recent_rows = conn.execute(
        """
        SELECT hrv_avg_ms
        FROM sleep_nights
        WHERE source = ? AND sleep_date < ? AND sleep_date >= ? AND hrv_avg_ms IS NOT NULL
        ORDER BY sleep_date
        """,
        [source, feature_date, recent_window_start],
    ).fetchall()
    recent_history = [float(value) for (value,) in recent_rows]

    if len(recent_history) >= 7:
        history = recent_history
        method = "prior_28d"
    else:
        expanding_rows = conn.execute(
            """
            SELECT hrv_avg_ms
            FROM sleep_nights
            WHERE source = ? AND sleep_date < ? AND hrv_avg_ms IS NOT NULL
            ORDER BY sleep_date
            """,
            [source, feature_date],
        ).fetchall()
        history = [float(value) for (value,) in expanding_rows]
        if len(history) < 7:
            return None, None
        method = "prior_expanding_min7"

    median_value = float(statistics.median(history))
    mad = _median_absolute_deviation(history, median_value)
    scale = 1.4826 * mad
    if scale >= 1e-6:
        return (current_value - median_value) / scale, method

    std_z, fallback_kind = _std_fallback_z(current_value, history)
    if std_z is None:
        return None, None
    return std_z, f"{method}_{fallback_kind}"


def _choose_total_sleep_minutes(
    oura_row: SleepNightRow | None,
    eight_row: SleepNightRow | None,
) -> tuple[int | None, int | None, str | None]:
    oura_total = oura_row.total_sleep_min if oura_row is not None else None
    eight_total = eight_row.total_sleep_min if eight_row is not None else None
    warning = EIGHT_SLEEP_FALLBACK_IGNORED_WARNING if eight_row is not None else None

    if oura_total is not None and eight_total is not None:
        delta = abs(oura_total - eight_total)
        return oura_total, delta, warning
    if oura_total is not None:
        return oura_total, None, warning
    return None, None, warning


def _choose_stage_metrics(
    oura_row: SleepNightRow | None,
) -> tuple[float | None, str | None]:
    if oura_row is None:
        return None, None
    if oura_row.total_sleep_min is None or oura_row.total_sleep_min <= 0 or oura_row.deep_min is None:
        return None, None
    return oura_row.deep_min / oura_row.total_sleep_min, "oura"


def _choose_hrv_source(
    oura_row: SleepNightRow | None,
) -> tuple[float | None, str | None, str]:
    if oura_row is not None and oura_row.hrv_avg_ms is not None:
        return oura_row.hrv_avg_ms, "oura", "oura_primary"
    return None, None, "missing"


def _compute_prior_day_feeling(
    conn: duckdb.DuckDBPyConnection,
    *,
    feature_date: date,
    allow_imputation: bool,
) -> tuple[int | None, bool]:
    prior_date = feature_date - timedelta(days=1)
    row = conn.execute(
        """
        SELECT e.feeling
        FROM mood_current c
        JOIN mood_entries e ON e.log_id = c.log_id
        WHERE c.mood_date = ?
        """,
        [prior_date],
    ).fetchone()
    if row is not None:
        return int(row[0]), False
    if not allow_imputation:
        return None, False

    history_rows = conn.execute(
        """
        SELECT e.feeling
        FROM mood_current c
        JOIN mood_entries e ON e.log_id = c.log_id
        WHERE c.mood_date < ?
        ORDER BY c.mood_date DESC
        LIMIT 7
        """,
        [feature_date],
    ).fetchall()
    history = [int(value) for (value,) in history_rows]
    if not history:
        return None, False
    return int(round(statistics.fmean(history))), True


def apply_schema(conn: duckdb.DuckDBPyConnection, schema_path: Path = SCHEMA_PATH) -> None:
    conn.execute(schema_path.read_text(encoding="utf-8"))


def _filesystem_database_path(database: str | Path) -> Path | None:
    if isinstance(database, Path):
        return database
    if database == ":memory:" or "://" in database:
        return None
    return Path(database)


def connect_duckdb(
    database: str | Path = DEFAULT_DATABASE_PATH,
    *,
    apply_schema: bool = False,
    read_only: bool = False,
) -> duckdb.DuckDBPyConnection:
    database_path = _filesystem_database_path(database)
    if database_path is not None and not read_only:
        database_path.parent.mkdir(parents=True, exist_ok=True)
    conn = duckdb.connect(str(database), read_only=read_only)
    if apply_schema:
        globals()["apply_schema"](conn)
    return conn


def insert_sleep_night(
    conn: duckdb.DuckDBPyConnection,
    payload: SleepNightRow | Mapping[str, Any],
    *,
    quarantine_dir: Path = DEFAULT_QUARANTINE_DIR,
    general_log_path: Path = DEFAULT_GENERAL_LOG_PATH,
) -> SleepNightRow:
    row = _coerce_ingest_row(
        SleepNightRow,
        payload,
        validation_source=_extract_validation_source(payload, fallback="sleep_night"),
        quarantine_dir=quarantine_dir,
        general_log_path=general_log_path,
    )
    conn.execute(
        """
        INSERT OR REPLACE INTO sleep_nights (
            source, sleep_date, bedtime_utc, waketime_utc, total_sleep_min,
            rem_min, deep_min, light_min, awake_min, hrv_avg_ms,
            rhr_avg_bpm, body_temp_dev_c, sleep_score, ingested_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            row.source,
            row.sleep_date,
            row.bedtime_utc,
            row.waketime_utc,
            row.total_sleep_min,
            row.rem_min,
            row.deep_min,
            row.light_min,
            row.awake_min,
            row.hrv_avg_ms,
            row.rhr_avg_bpm,
            row.body_temp_dev_c,
            row.sleep_score,
            row.ingested_at_utc,
        ],
    )
    return row


def insert_mood_entry(
    conn: duckdb.DuckDBPyConnection,
    payload: MoodEntryRow | Mapping[str, Any],
    *,
    quarantine_dir: Path = DEFAULT_QUARANTINE_DIR,
    general_log_path: Path = DEFAULT_GENERAL_LOG_PATH,
) -> MoodEntryRow:
    row = _coerce_ingest_row(
        MoodEntryRow,
        payload,
        validation_source=_extract_validation_source(payload, fallback="mood_entry"),
        quarantine_dir=quarantine_dir,
        general_log_path=general_log_path,
    )
    current_row = conn.execute(
        "SELECT log_id FROM mood_current WHERE mood_date = ?",
        [row.mood_date],
    ).fetchone()
    if current_row is not None:
        current_log_id = _normalize_db_uuid(current_row[0])
        if row.supersedes_log_id is None:
            row = row.model_copy(update={"supersedes_log_id": current_log_id})
        elif row.supersedes_log_id != current_log_id:
            raise ValueError("supersedes_log_id must point to the current primary mood entry")

    conn.execute(
        """
        INSERT INTO mood_entries (
            log_id, logged_at_utc, mood_date, feeling, energy, notes,
            context_chips, source, supersedes_log_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            row.log_id,
            row.logged_at_utc,
            row.mood_date,
            row.feeling,
            row.energy,
            row.notes,
            list(row.context_chips),
            row.source,
            row.supersedes_log_id,
        ],
    )
    conn.execute(
        "INSERT OR REPLACE INTO mood_current (mood_date, log_id) VALUES (?, ?)",
        [row.mood_date, row.log_id],
    )
    return row


def insert_daily_features_row(
    conn: duckdb.DuckDBPyConnection,
    payload: DailyFeaturesRow | Mapping[str, Any],
) -> DailyFeaturesRow:
    row = _coerce_row(DailyFeaturesRow, payload)
    conn.execute(
        """
        INSERT OR REPLACE INTO daily_features (
            feature_date, total_sleep_min, hrv_z, deep_sleep_pct, prior_day_feeling,
            hrv_avg_ms, hrv_z_method, feature_version, prior_day_feeling_imputed,
            sleep_source_count, sleep_merge_warning, computed_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            row.feature_date,
            row.total_sleep_min,
            row.hrv_z,
            row.deep_sleep_pct,
            row.prior_day_feeling,
            row.hrv_avg_ms,
            row.hrv_z_method,
            row.feature_version,
            row.prior_day_feeling_imputed,
            row.sleep_source_count,
            row.sleep_merge_warning,
            row.computed_at_utc,
        ],
    )
    return row


def insert_sleep_merge_diagnostics(
    conn: duckdb.DuckDBPyConnection,
    payload: SleepMergeDiagnosticsRow | Mapping[str, Any],
) -> SleepMergeDiagnosticsRow:
    row = _coerce_row(SleepMergeDiagnosticsRow, payload)
    conn.execute(
        """
        INSERT OR REPLACE INTO sleep_merge_diagnostics (
            sleep_date, oura_present, eight_present, total_sleep_delta_min,
            hrv_merge_method, stage_source, warning, computed_at_utc
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            row.sleep_date,
            row.oura_present,
            row.eight_present,
            row.total_sleep_delta_min,
            row.hrv_merge_method,
            row.stage_source,
            row.warning,
            row.computed_at_utc,
        ],
    )
    return row


def select_current_mood_entries(
    conn: duckdb.DuckDBPyConnection,
    *,
    start_date: date | None = None,
    end_date: date | None = None,
) -> list[MoodEntryRow]:
    where_clauses: list[str] = []
    params: list[Any] = []

    if start_date is not None:
        where_clauses.append("e.mood_date >= ?")
        params.append(start_date)
    if end_date is not None:
        where_clauses.append("e.mood_date <= ?")
        params.append(end_date)

    where_sql = f"WHERE {' AND '.join(where_clauses)}" if where_clauses else ""
    rows = conn.execute(
        f"""
        SELECT e.log_id, e.logged_at_utc, e.mood_date, e.feeling, e.energy,
               e.notes, e.context_chips, e.source, e.supersedes_log_id
        FROM mood_current c
        JOIN mood_entries e ON e.log_id = c.log_id
        {where_sql}
        ORDER BY e.mood_date, e.logged_at_utc
        """,
        params,
    ).fetchall()
    return [_mood_entry_from_db(row) for row in rows]


def compute_daily_features(
    conn: duckdb.DuckDBPyConnection,
    feature_date: date,
    *,
    computed_at_utc: datetime | None = None,
    feature_version: str = DEFAULT_FEATURE_VERSION,
    allow_prior_day_imputation: bool = False,
    provider_policy: SleepProviderPolicy | None = None,
    policy_root: Path = REPO_ROOT,
) -> DailyFeaturesRow | None:
    sleep_rows = conn.execute(
        """
        SELECT source, sleep_date, bedtime_utc, waketime_utc, total_sleep_min,
               rem_min, deep_min, light_min, awake_min, hrv_avg_ms,
               rhr_avg_bpm, body_temp_dev_c, sleep_score, ingested_at_utc
        FROM sleep_nights
        WHERE sleep_date = ?
        """,
        [feature_date],
    ).fetchall()
    if not sleep_rows:
        return None

    policy = provider_policy or load_sleep_provider_policy(policy_root)
    normalized_sleep_rows = [_sleep_night_from_db(row) for row in sleep_rows]
    eligible_sleep_rows = eligible_sleep_rows_for_v1(normalized_sleep_rows, policy)
    by_source = {row.source: row for row in eligible_sleep_rows}
    raw_by_source = {row.source: row for row in normalized_sleep_rows}
    oura_row = by_source.get("oura")
    eight_row = raw_by_source.get("8sleep")

    total_sleep_min, total_sleep_delta_min, sleep_warning = _choose_total_sleep_minutes(
        oura_row,
        eight_row,
    )
    deep_sleep_pct, stage_source = _choose_stage_metrics(oura_row)
    hrv_avg_ms, hrv_source, hrv_merge_method = _choose_hrv_source(oura_row)
    hrv_z, hrv_z_method = _compute_hrv_z(
        conn,
        source=hrv_source or "oura",
        feature_date=feature_date,
        current_value=hrv_avg_ms,
    )
    prior_day_feeling, prior_day_feeling_imputed = _compute_prior_day_feeling(
        conn,
        feature_date=feature_date,
        allow_imputation=allow_prior_day_imputation,
    )
    computed_at = computed_at_utc or datetime.now(UTC)

    diagnostics_row = insert_sleep_merge_diagnostics(
        conn,
        SleepMergeDiagnosticsRow(
            sleep_date=feature_date,
            oura_present=oura_row is not None,
            eight_present=eight_row is not None,
            total_sleep_delta_min=total_sleep_delta_min,
            hrv_merge_method=hrv_merge_method,
            stage_source=stage_source,
            warning=sleep_warning,
            computed_at_utc=computed_at,
        ),
    )
    row = insert_daily_features_row(
        conn,
        DailyFeaturesRow(
            feature_date=feature_date,
            total_sleep_min=total_sleep_min,
            hrv_z=hrv_z,
            deep_sleep_pct=deep_sleep_pct,
            prior_day_feeling=prior_day_feeling,
            hrv_avg_ms=hrv_avg_ms,
            hrv_z_method=hrv_z_method,
            feature_version=feature_version,
            prior_day_feeling_imputed=prior_day_feeling_imputed,
            sleep_source_count=1 if diagnostics_row.oura_present else None,
            sleep_merge_warning=sleep_warning,
            computed_at_utc=computed_at,
        ),
    )
    return row


__all__ = [
    "DEFAULT_DATABASE_PATH",
    "DEFAULT_FEATURE_VERSION",
    "DEFAULT_GENERAL_LOG_PATH",
    "DEFAULT_QUARANTINE_DIR",
    "SCHEMA_PATH",
    "apply_schema",
    "compute_daily_features",
    "connect_duckdb",
    "insert_daily_features_row",
    "insert_mood_entry",
    "insert_sleep_merge_diagnostics",
    "insert_sleep_night",
    "select_current_mood_entries",
]

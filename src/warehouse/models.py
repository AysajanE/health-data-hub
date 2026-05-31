from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
import json
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


SleepSource = Literal["oura", "8sleep"]
MoodEntrySource = Literal["ios_shortcut", "manual", "backfill"]
ContextChip = Literal[
    "sick",
    "travel",
    "alcohol",
    "unusually_stressful",
    "unusual_workout",
    "late_meal",
    "poor_sleep_environment",
    "high_stress",
]
HrvZMethod = Literal[
    "prior_28d",
    "prior_expanding_min7",
    "prior_28d_std_fallback",
    "prior_expanding_min7_std_fallback",
]
HrvMergeMethod = Literal["oura_primary", "missing"]
StageSource = Literal["oura"]


def _normalize_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("must be timezone-aware")
    return value.astimezone(UTC)


def _serialize_for_hash(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, UUID):
        return str(value)
    raise TypeError(f"unsupported value for payload hash: {type(value)!r}")


def _payload_hash(payload: Any) -> str:
    serialized = json.dumps(
        payload,
        default=_serialize_for_hash,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _summarize_validation_error(error: ValidationError) -> str:
    entries: list[str] = []
    for detail in error.errors():
        location = ".".join(str(part) for part in detail.get("loc", ()))
        message = str(detail.get("msg", "validation error"))
        entries.append(f"{location}: {message}" if location else message)
    return "; ".join(entries)


class WarehouseRowModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class SleepNightRow(WarehouseRowModel):
    source: SleepSource
    sleep_date: date
    bedtime_utc: datetime | None = None
    waketime_utc: datetime | None = None
    total_sleep_min: int | None = Field(default=None, ge=0)
    rem_min: int | None = Field(default=None, ge=0)
    deep_min: int | None = Field(default=None, ge=0)
    light_min: int | None = Field(default=None, ge=0)
    awake_min: int | None = Field(default=None, ge=0)
    hrv_avg_ms: float | None = Field(default=None, ge=0.0)
    rhr_avg_bpm: int | None = Field(default=None, ge=0)
    body_temp_dev_c: float | None = None
    sleep_score: int | None = Field(default=None, ge=0, le=100)
    ingested_at_utc: datetime

    @field_validator("bedtime_utc", "waketime_utc", "ingested_at_utc", mode="after")
    @classmethod
    def validate_timestamps(cls, value: datetime | None) -> datetime | None:
        return _normalize_utc(value)

    @model_validator(mode="after")
    def validate_sleep_window(self) -> "SleepNightRow":
        if (
            self.bedtime_utc is not None
            and self.waketime_utc is not None
            and self.waketime_utc < self.bedtime_utc
        ):
            raise ValueError("waketime_utc must be greater than or equal to bedtime_utc")
        return self


class MoodEntryRow(WarehouseRowModel):
    log_id: UUID
    logged_at_utc: datetime
    mood_date: date
    feeling: int = Field(ge=1, le=10)
    energy: int | None = Field(default=None, ge=1, le=10)
    notes: str | None = None
    context_chips: tuple[ContextChip, ...] = Field(default_factory=tuple)
    source: MoodEntrySource | None = None
    supersedes_log_id: UUID | None = None

    @field_validator("logged_at_utc", mode="after")
    @classmethod
    def validate_logged_at(cls, value: datetime) -> datetime:
        normalized = _normalize_utc(value)
        assert normalized is not None
        return normalized

    @field_validator("context_chips", mode="after")
    @classmethod
    def validate_context_chips(cls, value: tuple[ContextChip, ...]) -> tuple[ContextChip, ...]:
        if len(set(value)) != len(value):
            raise ValueError("context_chips must not contain duplicates")
        return value

    @model_validator(mode="after")
    def validate_correction_link(self) -> "MoodEntryRow":
        if self.supersedes_log_id is not None and self.supersedes_log_id == self.log_id:
            raise ValueError("supersedes_log_id must reference a different log entry")
        return self


class MoodCurrentRow(WarehouseRowModel):
    mood_date: date
    log_id: UUID


class DailyFeaturesRow(WarehouseRowModel):
    feature_date: date
    total_sleep_min: int | None = Field(default=None, ge=0)
    hrv_z: float | None = None
    deep_sleep_pct: float | None = Field(default=None, ge=0.0, le=1.0)
    prior_day_feeling: int | None = Field(default=None, ge=1, le=10)
    hrv_avg_ms: float | None = Field(default=None, ge=0.0)
    hrv_z_method: HrvZMethod | None = None
    feature_version: str | None = Field(default=None, min_length=1)
    prior_day_feeling_imputed: bool = False
    sleep_source_count: int | None = Field(default=None, ge=1, le=2)
    sleep_merge_warning: str | None = None
    computed_at_utc: datetime

    @field_validator("computed_at_utc", mode="after")
    @classmethod
    def validate_computed_at(cls, value: datetime) -> datetime:
        normalized = _normalize_utc(value)
        assert normalized is not None
        return normalized

    @model_validator(mode="after")
    def validate_feature_metadata(self) -> "DailyFeaturesRow":
        if self.hrv_z is not None and self.hrv_z_method is None:
            raise ValueError("hrv_z_method is required when hrv_z is populated")
        if self.prior_day_feeling_imputed and self.prior_day_feeling is None:
            raise ValueError("prior_day_feeling must be present when marked imputed")
        return self


class SleepMergeDiagnosticsRow(WarehouseRowModel):
    sleep_date: date
    oura_present: bool | None = None
    eight_present: bool | None = None
    total_sleep_delta_min: int | None = Field(default=None, ge=0)
    hrv_merge_method: HrvMergeMethod | None = None
    stage_source: StageSource | None = None
    warning: str | None = None
    computed_at_utc: datetime

    @field_validator("computed_at_utc", mode="after")
    @classmethod
    def validate_computed_at(cls, value: datetime) -> datetime:
        normalized = _normalize_utc(value)
        assert normalized is not None
        return normalized


class ValidationFailureMetadata(WarehouseRowModel):
    source: str = Field(min_length=1)
    detected_at_utc: datetime
    error_summary: str = Field(min_length=1)
    payload_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @field_validator("detected_at_utc", mode="after")
    @classmethod
    def validate_detected_at(cls, value: datetime) -> datetime:
        normalized = _normalize_utc(value)
        assert normalized is not None
        return normalized

    @classmethod
    def from_validation_error(
        cls,
        *,
        source: str,
        payload: Any,
        error: ValidationError,
        detected_at_utc: datetime | None = None,
    ) -> "ValidationFailureMetadata":
        return cls(
            source=source,
            detected_at_utc=detected_at_utc or datetime.now(UTC),
            error_summary=_summarize_validation_error(error),
            payload_hash=_payload_hash(payload),
        )


__all__ = [
    "ContextChip",
    "DailyFeaturesRow",
    "HrvMergeMethod",
    "HrvZMethod",
    "MoodCurrentRow",
    "MoodEntryRow",
    "MoodEntrySource",
    "SleepMergeDiagnosticsRow",
    "SleepNightRow",
    "SleepSource",
    "StageSource",
    "ValidationFailureMetadata",
]

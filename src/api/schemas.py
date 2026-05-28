from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator

from src.warehouse.models import ContextChip


def _normalize_optional_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("must be timezone-aware")
    return value.astimezone(UTC)


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class MoodLogRequest(ApiModel):
    feeling: StrictInt = Field(ge=1, le=10)
    energy: StrictInt | None = Field(default=None, ge=1, le=10)
    notes: str | None = None
    context_chips: tuple[ContextChip, ...] = Field(default_factory=tuple)
    logged_at_utc: datetime | None = None

    @field_validator("logged_at_utc", mode="after")
    @classmethod
    def validate_logged_at_utc(cls, value: datetime | None) -> datetime | None:
        return _normalize_optional_utc(value)

    @field_validator("context_chips", mode="after")
    @classmethod
    def validate_context_chips(cls, value: tuple[ContextChip, ...]) -> tuple[ContextChip, ...]:
        if len(set(value)) != len(value):
            raise ValueError("context_chips must not contain duplicates")
        return value


class MoodLogResponse(ApiModel):
    log_id: UUID
    mood_date: date
    status: Literal["ok"] = "ok"

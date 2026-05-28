from __future__ import annotations

from datetime import UTC, date, datetime
from functools import lru_cache
from ipaddress import ip_address
import os
from pathlib import Path
from typing import Mapping, Protocol
from uuid import uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator

from src.api.schemas import MoodLogRequest, MoodLogResponse
from src.warehouse.warehouse import DEFAULT_DATABASE_PATH, connect_duckdb, insert_mood_entry


ENV_MOOD_TOKEN = "MOOD_TOKEN"
ENV_LAN_BIND_IP = "LAN_BIND_IP"
ENV_HOME_TIMEZONE = "HOME_TIMEZONE"
DEFAULT_HOME_TIMEZONE = "America/Toronto"


class ApiSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    mood_token: str = Field(min_length=1)
    lan_bind_ip: str = Field(min_length=1)
    home_timezone: str = Field(default=DEFAULT_HOME_TIMEZONE, min_length=1)

    @field_validator("lan_bind_ip")
    @classmethod
    def validate_lan_bind_ip(cls, value: str) -> str:
        return str(ip_address(value))

    @field_validator("home_timezone")
    @classmethod
    def validate_home_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError("invalid timezone") from exc
        return value


def build_api_settings(
    *,
    mood_token: str,
    lan_bind_ip: str,
    home_timezone: str = DEFAULT_HOME_TIMEZONE,
) -> ApiSettings:
    return ApiSettings(
        mood_token=mood_token,
        lan_bind_ip=lan_bind_ip,
        home_timezone=home_timezone,
    )


def _read_setting(env: Mapping[str, str], name: str, *, default: str | None = None) -> str:
    if name in env:
        return env[name]
    if default is not None:
        return default
    return ""


def load_api_settings(env: Mapping[str, str] | None = None) -> ApiSettings:
    source = os.environ if env is None else env
    return build_api_settings(
        mood_token=_read_setting(source, ENV_MOOD_TOKEN),
        lan_bind_ip=_read_setting(source, ENV_LAN_BIND_IP),
        home_timezone=_read_setting(source, ENV_HOME_TIMEZONE, default=DEFAULT_HOME_TIMEZONE),
    )


@lru_cache(maxsize=1)
def get_api_settings() -> ApiSettings:
    return load_api_settings()


def clear_api_settings_cache() -> None:
    get_api_settings.cache_clear()


def _current_settings(settings: ApiSettings | None) -> ApiSettings:
    return settings if settings is not None else get_api_settings()


def get_mood_token(settings: ApiSettings | None = None) -> str:
    return _current_settings(settings).mood_token


def get_lan_bind_ip(settings: ApiSettings | None = None) -> str:
    return _current_settings(settings).lan_bind_ip


def get_home_timezone(settings: ApiSettings | None = None) -> ZoneInfo:
    return ZoneInfo(_current_settings(settings).home_timezone)


class MoodEntryPersister(Protocol):
    def __call__(self, payload: MoodLogRequest, mood_date: date) -> MoodLogResponse: ...


def persist_mood_entry_to_warehouse(
    payload: MoodLogRequest,
    mood_date: date,
    *,
    database: str | Path = DEFAULT_DATABASE_PATH,
) -> MoodLogResponse:
    logged_at_utc = payload.logged_at_utc or datetime.now(UTC)
    conn = connect_duckdb(database, apply_schema=True)
    try:
        entry = insert_mood_entry(
            conn,
            {
                "log_id": uuid4(),
                "logged_at_utc": logged_at_utc,
                "mood_date": mood_date,
                "feeling": payload.feeling,
                "energy": payload.energy,
                "notes": payload.notes,
                "context_chips": payload.context_chips,
                "source": "ios_shortcut",
                "supersedes_log_id": None,
            },
        )
    finally:
        conn.close()

    return MoodLogResponse(log_id=entry.log_id, mood_date=entry.mood_date, status="ok")


def default_persist_mood_entry(payload: MoodLogRequest, mood_date: date) -> MoodLogResponse:
    return persist_mood_entry_to_warehouse(payload, mood_date, database=DEFAULT_DATABASE_PATH)

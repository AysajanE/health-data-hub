from __future__ import annotations

from functools import lru_cache
from ipaddress import ip_address
import os
from typing import Mapping
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, field_validator


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
        ZoneInfo(value)
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

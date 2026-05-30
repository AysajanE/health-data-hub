#!/usr/bin/env python3
"""Collect real local 8 Sleep availability evidence for S03.

The original S03 collector used the archived PyPI ``pyEight==0.3.2`` package.
That package still posts to the legacy 8 Sleep ``/login`` endpoint, which now
returns provider-side failures. This collector keeps the existing pyEight smoke
evidence surface but uses the current read-only token + trends flow observed in
maintained community clients.

Safety invariant: this script may authenticate and confirm that 8 Sleep sleep
trend data is reachable, but it must not persist raw provider payloads,
provider credentials, bearer credentials, account email, password, user IDs,
device IDs, exact dates, or exact metric values. The report is intentionally
limited to aggregate counts, booleans, and a coarse freshness bucket.
"""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import re
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from unittest import mock
from urllib.parse import urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.evidence._collector_common import write_report


REQUIRED_ENV = (
    "PYEIGHT_EMAIL",
    "PYEIGHT_PASSWORD",
    "PYEIGHT_TIMEZONE",
    "PYEIGHT_CLIENT_ID",
    "PYEIGHT_CLIENT_SECRET",
)
AUTH_URL = "https://auth-api.8slp.net/v1/tokens"
CLIENT_API_URL = "https://client-api.8slp.net/v1"
REQUEST_TIMEOUT_SECONDS = 20
RECENT_SLEEP_MAX_AGE_DAYS = 7
CREDENTIAL_CACHE = Path("data/secrets/eight_sleep_auth_cache.json")
FALLBACK_DECISION_PREFIX = "S03-pyeight-fallback"
SUCCESS_DECISION_PREFIX = "S03-pyeight-evidence"


@dataclass(frozen=True)
class EightSleepConfig:
    email: str
    password: str
    timezone_name: str
    client_id: str
    client_secret: str


class ProviderIssue(Exception):
    """A provider or account state issue that should fail closed."""

    def __init__(
        self,
        reason: str,
        *,
        status: str = "blocked_external",
        http_status: int | None = None,
        error_type: str | None = None,
    ) -> None:
        super().__init__(reason)
        self.reason = reason
        self.status = status
        self.http_status = http_status
        self.error_type = error_type


def pyeight_distribution_version() -> str | None:
    for package_name in ("pyEight", "pyeight"):
        try:
            return importlib.metadata.version(package_name)
        except importlib.metadata.PackageNotFoundError:
            continue
    return None


def fallback_decision_exists(root: Path) -> Path | None:
    decisions = sorted((root / "ops/autonomy/decisions").glob("*pyeight*json"))
    for decision in reversed(decisions):
        payload = read_json_dict(decision)
        if payload.get("status") == "fallback_accepted" and payload.get("action") == "oura_only_v1":
            return decision
    return None


def read_json_dict(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def write_json_file(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp_path, mode)
    tmp_path.replace(path)
    os.chmod(path, mode)


def write_fallback_decision(root: Path, *, evidence_status: str, reason: str) -> Path:
    existing = fallback_decision_exists(root)
    if existing:
        return existing

    decisions_dir = root / "ops/autonomy/decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    path = decisions_dir / f"{FALLBACK_DECISION_PREFIX}-{timestamp}.json"
    payload = {
        "schema_version": "autokeel.provider_evidence_decision.v1",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "slice": "S03",
        "provider": "pyeight",
        "status": "fallback_accepted",
        "tripwire": "pyeight_smoke_failure",
        "action": "oura_only_v1",
        "source": "pyeight_smoke",
        "reason": reason,
        "evidence_status": evidence_status,
        "fallback_active": True,
        "sanitized": True,
        "raw_payload_tracked": False,
        "secret_values_tracked": False,
    }
    write_json_file(path, payload)
    return path


def write_success_decision(root: Path, *, evidence_path: Path) -> Path:
    decisions_dir = root / "ops/autonomy/decisions"
    decisions_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")
    path = decisions_dir / f"{SUCCESS_DECISION_PREFIX}-{timestamp}.json"
    payload = {
        "schema_version": "autokeel.provider_evidence_decision.v1",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "slice": "S03",
        "provider": "pyeight",
        "status": "ok",
        "evidence_status": "ok",
        "evidence_path": str(evidence_path.relative_to(root)),
        "evidence_recorded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "fallback_active": False,
        "decision": "include_8_sleep_under_tripwire",
        "sanitized": True,
        "raw_payload_tracked": False,
        "secret_values_tracked": False,
        "notes": [
            "This tracked decision records only the local evidence status and relative private evidence path.",
            "The private evidence file remains gitignored and contains no tracked raw provider payload.",
            "The pyeight fallback is not active for this decision.",
        ],
    }
    write_json_file(path, payload)
    return path


def write_fallback_report(root: Path, *, decision: Path, trigger_status: str, reason: str | None) -> dict[str, object]:
    path = write_report(
        root,
        "pyeight_smoke",
        {
            "status": "fallback_accepted",
            "decision": str(decision.relative_to(root)),
            "fallback": "oura_only_v1",
            "trigger_status": trigger_status,
            "reason": reason,
        },
    )
    return {"status": "fallback_accepted", "evidence": str(path.relative_to(root)), "errors": []}


def write_status_report(
    root: Path,
    *,
    status: str,
    summary: dict[str, Any],
    decision: Path | None = None,
) -> dict[str, object]:
    reason = summary.get("reason")
    payload = {
        **summary,
        "status": status,
        "legacy_pyeight_distribution_version": pyeight_distribution_version(),
        "sanitization": "aggregate_counts_booleans_only_no_raw_payloads_or_identifiers",
    }
    if decision is not None:
        payload["decision"] = str(decision.relative_to(root))
        payload["fallback"] = "oura_only_v1"
    path = write_report(root, "pyeight_smoke", payload)
    errors = [str(reason)] if isinstance(reason, str) and reason else []
    return {"status": status, "evidence": str(path.relative_to(root)), "errors": errors}


def report_existing_fallback(
    root: Path,
    *,
    decision: Path,
    fallback_to_decision: bool,
) -> dict[str, object]:
    payload = read_json_dict(decision)
    trigger_status = str(payload.get("evidence_status") or "fallback_accepted")
    reason = payload.get("reason")
    if fallback_to_decision:
        report_reason = reason if isinstance(reason, str) and reason else "existing fallback decision in force"
        return write_fallback_report(
            root,
            decision=decision,
            trigger_status=trigger_status,
            reason=report_reason,
        )
    if trigger_status in {"blocked_external", "error"}:
        report_reason = reason if isinstance(reason, str) and reason else f"existing fallback decision still reflects {trigger_status}"
        return write_status_report(
            root,
            status=trigger_status,
            summary={"reason": report_reason},
            decision=decision,
        )
    return write_fallback_report(
        root,
        decision=decision,
        trigger_status="fallback_accepted",
        reason="existing fallback decision in force",
    )


def validate_timezone(timezone_name: str) -> ZoneInfo:
    if timezone_name.lower() == "local":
        raise ProviderIssue("invalid PYEIGHT_TIMEZONE; use an explicit IANA timezone")
    try:
        return ZoneInfo(timezone_name)
    except ZoneInfoNotFoundError as exc:
        raise ProviderIssue("invalid PYEIGHT_TIMEZONE; use an explicit IANA timezone") from exc


def cache_key(config: EightSleepConfig) -> str:
    digest = hashlib.sha256()
    digest.update(config.email.lower().encode("utf-8"))
    digest.update(b"\0")
    digest.update(config.client_id.encode("utf-8"))
    return digest.hexdigest()


def cache_path(root: Path) -> Path:
    return root / CREDENTIAL_CACHE


def parse_utc_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def load_cached_auth(root: Path, config: EightSleepConfig) -> dict[str, Any] | None:
    path = cache_path(root)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if payload.get("cache_key") != cache_key(config):
        return None
    expires_at = parse_utc_datetime(payload.get("expires_at_utc"))
    credential = payload.get("credential")
    user_id = payload.get("user_id")
    if not expires_at or not isinstance(credential, str) or not credential or not isinstance(user_id, str):
        return None
    if expires_at <= datetime.now(timezone.utc) + timedelta(minutes=2):
        return None
    return {"credential": credential, "user_id": user_id}


def write_cached_auth(root: Path, config: EightSleepConfig, credential: str, user_id: str, expires_in: Any) -> None:
    try:
        expires_seconds = int(expires_in)
    except (TypeError, ValueError):
        expires_seconds = 3600
    expires_at = datetime.now(timezone.utc) + timedelta(seconds=max(60, expires_seconds - 60))
    payload = {
        "cache_key": cache_key(config),
        "credential": credential,
        "user_id": user_id,
        "expires_at_utc": expires_at.isoformat(timespec="seconds"),
    }
    path = cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(tmp_path, 0o600)
    tmp_path.replace(path)
    os.chmod(path, 0o600)


def redact_text(text: str, secret_values: list[str]) -> str:
    redacted = text
    for value in secret_values:
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    redacted = re.sub(r"(?i)(bearer|authorization|session-token)[\s:=]+[A-Za-z0-9_.\-+/=]+", r"\1=[REDACTED]", redacted)
    redacted = re.sub(r"/users/[^/?\s]+", "/users/[REDACTED]", redacted)
    redacted = re.sub(r"/devices/[^/?\s]+", "/devices/[REDACTED]", redacted)
    redacted = re.sub(r"[A-Za-z0-9_-]{24,}", "[REDACTED]", redacted)
    return redacted[:240]


def provider_exception(exc: Exception, secret_values: list[str]) -> ProviderIssue:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code == 429:
            return ProviderIssue("8 Sleep API rate limited the smoke check", http_status=exc.code)
        if exc.code in {400, 401, 403}:
            return ProviderIssue("8 Sleep authentication or authorization was rejected", http_status=exc.code)
        if 500 <= exc.code <= 599:
            return ProviderIssue("8 Sleep API returned a server-side failure", status="error", http_status=exc.code)
        return ProviderIssue("8 Sleep API request failed", status="error", http_status=exc.code)
    if isinstance(exc, urllib.error.URLError):
        return ProviderIssue(redact_text(str(exc.reason), secret_values), status="error", error_type=type(exc).__name__)
    return ProviderIssue(redact_text(str(exc) or type(exc).__name__, secret_values), status="error", error_type=type(exc).__name__)


def read_json_response(request: urllib.request.Request, secret_values: list[str]) -> dict[str, Any]:
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            body = response.read(500_000).decode("utf-8", errors="replace")
    except Exception as exc:
        raise provider_exception(exc, secret_values) from exc
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as exc:
        raise ProviderIssue("8 Sleep API returned malformed JSON", status="error", error_type=type(exc).__name__) from exc
    if not isinstance(payload, dict):
        raise ProviderIssue("8 Sleep API returned an unexpected JSON shape", status="error")
    return payload


def auth_headers() -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "okhttp/4.9.3",
        "Connection": "keep-alive",
    }


def api_headers(credential: str) -> dict[str, str]:
    return {
        "Accept": "application/json",
        "Content-Type": "application/json; charset=UTF-8",
        "User-Agent": "okhttp/4.9.3",
        "Connection": "keep-alive",
        "Authorization": f"Bearer {credential}",
    }


def authenticate(root: Path, config: EightSleepConfig, secret_values: list[str]) -> tuple[str, str, bool]:
    cached = load_cached_auth(root, config)
    if cached:
        return str(cached["credential"]), str(cached["user_id"]), True

    body = urlencode(
        {
            "grant_type": "password",
            "username": config.email,
            "password": config.password,
            "client_id": config.client_id,
            "client_secret": config.client_secret,
        }
    ).encode("utf-8")
    request = urllib.request.Request(AUTH_URL, data=body, headers=auth_headers(), method="POST")
    payload = read_json_response(request, secret_values)
    credential = payload.get("access_token")
    user_id = payload.get("userId") or payload.get("user_id")
    if not isinstance(credential, str) or not credential:
        raise ProviderIssue("8 Sleep authentication response did not include an access credential", status="error")
    if not isinstance(user_id, str) or not user_id:
        raise ProviderIssue("8 Sleep authentication response did not include a user identifier", status="error")
    write_cached_auth(root, config, credential, user_id, payload.get("expires_in"))
    return credential, user_id, False


def api_get(path: str, credential: str, params: dict[str, str] | None, secret_values: list[str]) -> dict[str, Any]:
    query = f"?{urlencode(params)}" if params else ""
    request = urllib.request.Request(
        f"{CLIENT_API_URL}{path}{query}",
        headers=api_headers(credential),
        method="GET",
    )
    return read_json_response(request, [*secret_values, credential])


def extract_user_id(auth_user_id: str, users_me: dict[str, Any]) -> str:
    for key in ("id", "userId", "user_id"):
        value = users_me.get(key)
        if isinstance(value, str) and value:
            return value
    user = users_me.get("user")
    if isinstance(user, dict):
        for key in ("id", "userId", "user_id"):
            value = user.get(key)
            if isinstance(value, str) and value:
                return value
    return auth_user_id


def summarize_devices(users_me: dict[str, Any]) -> tuple[int, bool]:
    user = users_me.get("user")
    roots = [users_me, user] if isinstance(user, dict) else [users_me]
    device_count = 0
    current_device_present = False
    for root in roots:
        devices = root.get("devices")
        if not isinstance(devices, list):
            devices = root.get("deviceList")
        if isinstance(devices, list):
            device_count = max(device_count, len(devices))
        current_device_present = current_device_present or any(
            root.get(key)
            for key in (
                "currentDevice",
                "currentDeviceId",
                "currentDeviceID",
                "current_device",
                "current_device_id",
            )
        )
    return device_count, bool(current_device_present or device_count > 0)


def iter_dicts(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    return []


def has_any_key(value: Any, candidate_keys: set[str]) -> bool:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in candidate_keys and child not in (None, "", [], {}):
                return True
            if has_any_key(child, candidate_keys):
                return True
    elif isinstance(value, list):
        return any(has_any_key(child, candidate_keys) for child in value)
    return False


def provider_day(value: Any, tz: ZoneInfo) -> date | None:
    if isinstance(value, str):
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            parsed = parse_utc_datetime(value)
            if parsed:
                return parsed.astimezone(tz).date()
    return None


def sleep_day_age(day: dict[str, Any], tz: ZoneInfo, today: date) -> int | None:
    for key in ("date", "day", "sleepDate", "presenceStart", "sessionStart", "start", "startTime"):
        parsed = provider_day(day.get(key), tz)
        if parsed:
            return (today - parsed).days
    sessions = iter_dicts(day.get("sessions"))
    for session in sessions:
        for key in ("presenceStart", "sessionStart", "start", "startTime"):
            parsed = provider_day(session.get(key), tz)
            if parsed:
                return (today - parsed).days
    return None


def freshness_bucket(age_days: int | None) -> str:
    if age_days is None:
        return "unknown"
    if age_days <= 1:
        return "0-1d"
    if age_days <= 3:
        return "2-3d"
    if age_days <= RECENT_SLEEP_MAX_AGE_DAYS:
        return "4-7d"
    return "stale"


def summarize_trends(trends: dict[str, Any], tz: ZoneInfo) -> dict[str, Any]:
    days = iter_dicts(trends.get("days"))
    today = datetime.now(tz).date()
    sleep_days: list[dict[str, Any]] = []
    sleep_ages: list[int] = []

    for day in days:
        sessions = iter_dicts(day.get("sessions"))
        has_sleep_duration = any(
            day.get(key) not in (None, "", 0)
            for key in ("sleepDuration", "sleepDurationSeconds", "sleep_duration", "sleep_duration_seconds")
        )
        has_session = bool(sessions)
        if has_sleep_duration or has_session:
            sleep_days.append(day)
            age = sleep_day_age(day, tz, today)
            if age is not None:
                sleep_ages.append(age)

    latest_age = min((age for age in sleep_ages if age >= 0), default=None)
    sleep_score_present = any(has_any_key(day, {"sleepQualityScore", "score"}) for day in sleep_days)
    sleep_stages_present = any(has_any_key(day, {"stages", "stageDurations", "stage_duration"}) for day in sleep_days)
    heart_rate_signal_present = any(has_any_key(day, {"heartRate", "heart_rate"}) for day in sleep_days)
    resp_rate_signal_present = any(has_any_key(day, {"respiratoryRate", "respRate", "resp_rate"}) for day in sleep_days)
    hrv_signal_present = any(has_any_key(day, {"hrv", "heartRateVariability"}) for day in sleep_days)

    return {
        "trend_day_count": len(days),
        "sleep_day_count": len(sleep_days),
        "recent_complete_sleep_interval_present": latest_age is not None and latest_age <= RECENT_SLEEP_MAX_AGE_DAYS,
        "sleep_score_present": sleep_score_present,
        "sleep_stages_present": sleep_stages_present,
        "heart_rate_signal_present": heart_rate_signal_present,
        "resp_rate_signal_present": resp_rate_signal_present,
        "hrv_signal_present": hrv_signal_present,
        "freshness_bucket": freshness_bucket(latest_age),
    }


def run_current_eight_sleep_summary(root: Path, config: EightSleepConfig) -> dict[str, Any]:
    tz = validate_timezone(config.timezone_name)
    secret_values = [config.email, config.password, config.client_id, config.client_secret]
    credential, auth_user_id, cache_used = authenticate(root, config, secret_values)

    users_me = api_get("/users/me", credential, None, secret_values)
    user_id = extract_user_id(auth_user_id, users_me)
    device_count, current_device_present = summarize_devices(users_me)

    local_today = datetime.now(tz).date()
    params = {
        "tz": config.timezone_name,
        "from": (local_today - timedelta(days=RECENT_SLEEP_MAX_AGE_DAYS)).isoformat(),
        "to": (local_today + timedelta(days=1)).isoformat(),
        "include-main": "false",
        "include-all-sessions": "true",
        "model-version": "v2",
    }
    trends = api_get(f"/users/{user_id}/trends", credential, params, secret_values)
    trend_summary = summarize_trends(trends, tz)
    sleep_signal_present = any(
        bool(trend_summary[key])
        for key in (
            "sleep_score_present",
            "sleep_stages_present",
            "heart_rate_signal_present",
            "resp_rate_signal_present",
            "hrv_signal_present",
        )
    )
    status = "ok"
    reason = None
    if not user_id:
        status = "blocked_external"
        reason = "authenticated but no user was returned"
    elif not current_device_present:
        status = "blocked_external"
        reason = "authenticated but no 8 Sleep device was returned"
    elif not trend_summary["recent_complete_sleep_interval_present"]:
        status = "blocked_external"
        reason = "authenticated but no recent complete sleep interval was returned"
    elif not sleep_signal_present:
        status = "blocked_external"
        reason = "authenticated but no sleep metric signal was returned"

    return {
        "status": status,
        "auth_flow": "oauth_password_grant_current_api",
        "authenticated": True,
        "credential_cache_used": cache_used,
        "user_present": bool(user_id),
        "device_count": device_count,
        "current_device_present": current_device_present,
        "query_window_days": RECENT_SLEEP_MAX_AGE_DAYS,
        **trend_summary,
        "reason": reason,
    }


def collect(
    root: Path,
    *,
    runner: Callable[[Path, EightSleepConfig], dict[str, Any]] = run_current_eight_sleep_summary,
    fallback_to_decision: bool = False,
) -> dict[str, object]:
    decision = fallback_decision_exists(root)
    if decision:
        return report_existing_fallback(
            root,
            decision=decision,
            fallback_to_decision=fallback_to_decision,
        )

    missing_env = [key for key in REQUIRED_ENV if not os.environ.get(key)]
    if missing_env:
        reason = f"missing optional 8 Sleep credentials: {', '.join(missing_env)}"
        decision = write_fallback_decision(
            root,
            evidence_status="blocked_external",
            reason=reason,
        )
        if fallback_to_decision:
            return write_fallback_report(
                root,
                decision=decision,
                trigger_status="blocked_external",
                reason="optional 8 Sleep credentials are absent; using Oura-only v1 fallback",
            )
        return write_status_report(
            root,
            status="blocked_external",
            summary={"reason": reason},
            decision=decision,
        )

    config = EightSleepConfig(
        email=os.environ["PYEIGHT_EMAIL"],
        password=os.environ["PYEIGHT_PASSWORD"],
        timezone_name=os.environ["PYEIGHT_TIMEZONE"],
        client_id=os.environ["PYEIGHT_CLIENT_ID"],
        client_secret=os.environ["PYEIGHT_CLIENT_SECRET"],
    )
    try:
        validate_timezone(config.timezone_name)
        summary = runner(root, config)
    except ProviderIssue as exc:
        decision = write_fallback_decision(root, evidence_status=exc.status, reason=exc.reason)
        if fallback_to_decision:
            return write_fallback_report(root, decision=decision, trigger_status=exc.status, reason=exc.reason)
        return write_status_report(
            root,
            status=exc.status,
            summary={"reason": exc.reason},
            decision=decision,
        )
    except Exception as exc:
        message = redact_text(
            str(exc) or type(exc).__name__,
            [config.email, config.password, config.client_id, config.client_secret],
        )
        decision = write_fallback_decision(root, evidence_status="error", reason=message)
        if fallback_to_decision:
            return write_fallback_report(root, decision=decision, trigger_status="error", reason=message)
        return write_status_report(
            root,
            status="error",
            summary={"reason": message},
            decision=decision,
        )

    status = str(summary.get("status") or "error")
    if status not in {"ok", "blocked_external", "error"}:
        status = "error"
        summary = {**summary, "reason": "collector runner returned invalid status"}
    if status != "ok":
        reason = str(summary.get("reason") or "8 Sleep smoke evidence did not return ok")
        decision = write_fallback_decision(root, evidence_status=status, reason=reason)
        if fallback_to_decision:
            return write_fallback_report(
                root,
                decision=decision,
                trigger_status=status,
                reason=reason,
            )
        return write_status_report(
            root,
            status=status,
            summary={**summary, "reason": reason},
            decision=decision,
        )
    report = write_status_report(root, status=status, summary=summary)
    write_success_decision(root, evidence_path=root / str(report["evidence"]))
    return report


def run_self_checks() -> None:
    env = {
        "PYEIGHT_EMAIL": "self-check@example.test",
        "PYEIGHT_PASSWORD": "self-check-password",
        "PYEIGHT_TIMEZONE": "America/Toronto",
        "PYEIGHT_CLIENT_ID": "self-check-client-id",
        "PYEIGHT_CLIENT_SECRET": "self-check-client-secret",
    }

    def ok_runner(_: Path, __: EightSleepConfig) -> dict[str, object]:
        return {
            "status": "ok",
            "auth_flow": "oauth_password_grant_current_api",
            "authenticated": True,
            "credential_cache_used": False,
            "user_present": True,
            "device_count": 1,
            "current_device_present": True,
            "query_window_days": RECENT_SLEEP_MAX_AGE_DAYS,
            "trend_day_count": 3,
            "sleep_day_count": 2,
            "recent_complete_sleep_interval_present": True,
            "sleep_score_present": True,
            "sleep_stages_present": True,
            "heart_rate_signal_present": True,
            "resp_rate_signal_present": True,
            "hrv_signal_present": True,
            "freshness_bucket": "0-1d",
        }

    def failing_runner(_: Path, __: EightSleepConfig) -> dict[str, object]:
        raise ProviderIssue("8 Sleep API unavailable", status="blocked_external")

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        with mock.patch.dict(os.environ, env, clear=True):
            report = collect(root, runner=failing_runner)
        decisions = sorted((root / "ops/autonomy/decisions").glob(f"{FALLBACK_DECISION_PREFIX}-*.json"))
        if report["status"] != "blocked_external" or len(decisions) != 1:
            raise AssertionError("failed-provider self-check did not keep raw blocked_external status and record a fallback decision")

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        decision_dir = root / "ops/autonomy/decisions"
        decision_dir.mkdir(parents=True, exist_ok=True)
        decision = decision_dir / f"{FALLBACK_DECISION_PREFIX}-20260529T000000-0400.json"
        write_json_file(
            decision,
            {
                "created_at": "2026-05-29T00:00:00-04:00",
                "status": "fallback_accepted",
                "action": "oura_only_v1",
                "evidence_status": "blocked_external",
                "reason": "stored fallback still reflects missing local 8 Sleep credentials",
            },
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            raw_report = collect(root)
            cli_report = collect(root, fallback_to_decision=True)
        if raw_report["status"] != "blocked_external" or cli_report["status"] != "fallback_accepted":
            raise AssertionError("existing-fallback self-check did not preserve library raw status while keeping CLI fallback_accepted")

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        with mock.patch.dict(os.environ, env, clear=True):
            report = collect(root, runner=ok_runner)
        decisions = sorted((root / "ops/autonomy/decisions").glob(f"{SUCCESS_DECISION_PREFIX}-*.json"))
        if report["status"] != "ok" or len(decisions) != 1:
            raise AssertionError("success-path self-check did not write the tracked pyeight evidence decision")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect 8 Sleep smoke evidence without logging secrets.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        run_self_checks()
    except AssertionError as exc:
        report = {"status": "error", "errors": [f"deterministic self-check failed: {exc}"]}
        if args.json:
            print(json.dumps(report, indent=2, sort_keys=True))
        else:
            print(report["status"])
        return 1
    report = collect(Path(args.root).resolve(), fallback_to_decision=True)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["status"])
    return 0 if report["status"] in {"ok", "fallback_accepted"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

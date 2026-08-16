#!/usr/bin/env python3
"""Typed evidence contracts for behavioral AutoKeel tripwires.

Provider evidence keeps its existing generic ``status`` contract.  The three
behavioral tripwires use the builders and validators in this module so a
review document, an arbitrary file, or a self-asserted ``status: ok`` cannot
stand in for the measurement named by the design.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_ROOT = ROOT / "ops" / "autonomy" / "schemas"

MOOD_TRANSPORT_KIND = "mood_transport_v1"
MOOD_COMPLIANCE_KIND = "mood_compliance_v1"
BASELINE_GATE_KIND = "baseline_gate_v1"

EVIDENCE_KIND_BY_TRIPWIRE = {
    "on_mood_transport_failure_week_4": MOOD_TRANSPORT_KIND,
    "on_mood_compliance_failure_week_8": MOOD_COMPLIANCE_KIND,
    "on_baseline_gate_failure_week_9": BASELINE_GATE_KIND,
}

SCHEMA_PATH_BY_KIND = {
    MOOD_TRANSPORT_KIND: SCHEMA_ROOT / "tripwire_mood_transport.schema.json",
    MOOD_COMPLIANCE_KIND: SCHEMA_ROOT / "tripwire_mood_compliance.schema.json",
    BASELINE_GATE_KIND: SCHEMA_ROOT / "tripwire_baseline_gate.schema.json",
}

TRANSPORT_TRIPWIRE = "on_mood_transport_failure_week_4"
COMPLIANCE_TRIPWIRE = "on_mood_compliance_failure_week_8"
BASELINE_TRIPWIRE = "on_baseline_gate_failure_week_9"

TRANSPORT_OPPORTUNITIES = 7
TRANSPORT_FAILURE_THRESHOLD = 6
COMPLIANCE_WINDOW_DAYS = 28
COMPLIANCE_REQUIRED_DAYS = 23

_SENSITIVE_MOOD_KEYS = {
    "energy",
    "feeling",
    "note",
    "notes",
    "rating",
    "ratings",
    "raw_payload",
}
_KNOWN_MOOD_SOURCES = {"ios_shortcut", "manual", "backfill"}


def _created_at(value: datetime | None = None) -> str:
    stamp = value or datetime.now(UTC)
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("created_at must be timezone-aware")
    return stamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _coerce_date(value: date | str, *, field: str) -> date:
    if isinstance(value, datetime):
        raise ValueError(f"{field} must be a date, not a datetime")
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO date") from exc


def _payload_for_validation(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Remove only metadata injected by the local evidence evaluator.

    Evidence producers do not own an open-ended private-key namespace.  In
    particular, underscore-prefixing a rating, note, raw payload, or any other
    schema extension must not make that field invisible to validation.
    """

    clean_payload = dict(payload)
    clean_payload.pop("_report_path", None)
    return clean_payload


@lru_cache(maxsize=None)
def _schema(kind: str) -> dict[str, Any]:
    path = SCHEMA_PATH_BY_KIND.get(kind)
    if path is None:
        raise ValueError(f"unknown tripwire evidence kind: {kind}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"tripwire schema is not an object: {path}")
    return payload


def _schema_errors(kind: str, payload: Mapping[str, Any]) -> list[str]:
    validator = Draft202012Validator(_schema(kind), format_checker=FormatChecker())
    errors: list[str] = []
    for error in sorted(
        validator.iter_errors(payload),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    ):
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        errors.append(f"{location}: {error.message}")
    return errors


def _forbidden_mood_fields(value: Any, *, path: str = "$") -> list[str]:
    errors: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            key_text = str(key).lower().lstrip("_")
            child_path = f"{path}.{key}"
            if key_text in _SENSITIVE_MOOD_KEYS:
                errors.append(
                    f"{child_path}: ratings, notes, and raw payloads are forbidden in typed tripwire evidence"
                )
            errors.extend(_forbidden_mood_fields(child, path=child_path))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            errors.extend(_forbidden_mood_fields(child, path=f"{path}[{index}]"))
    return errors


def build_mood_transport_report(
    outcomes: Sequence[bool],
    *,
    window_start: date | str,
    window_end: date | str,
    fallback_verified: bool = False,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Build aggregate seven-opportunity transport evidence.

    ``True`` means that the evening opportunity successfully posted.  No mood
    value, note, token, response body, or identifier is accepted or emitted.
    Six or more failures trigger the precommitted Streamlit fallback; that
    result resolves only when ``fallback_verified`` is explicit.
    """

    if len(outcomes) != TRANSPORT_OPPORTUNITIES or any(type(item) is not bool for item in outcomes):
        raise ValueError("mood transport evidence requires exactly seven boolean opportunities")
    if type(fallback_verified) is not bool:
        raise ValueError("fallback_verified must be a boolean")
    start = _coerce_date(window_start, field="window_start")
    end = _coerce_date(window_end, field="window_end")
    if (end - start).days != TRANSPORT_OPPORTUNITIES - 1:
        raise ValueError("mood transport window must cover seven consecutive local dates")

    successful = sum(1 for item in outcomes if item)
    failed = TRANSPORT_OPPORTUNITIES - successful
    fired = failed >= TRANSPORT_FAILURE_THRESHOLD
    if not fired:
        status = "ok"
        action = "none"
        fallback_verified = False
    elif fallback_verified:
        status = "fallback_accepted"
        action = "streamlit_mobile_form"
    else:
        status = "triggered"
        action = "streamlit_mobile_form"

    return {
        "schema_version": "health_data_hub.tripwire.mood_transport.v1",
        "tripwire": TRANSPORT_TRIPWIRE,
        "created_at": _created_at(created_at),
        "status": status,
        "action": action,
        "fallback_verified": fallback_verified,
        "observation": {
            "window_start": start.isoformat(),
            "window_end": end.isoformat(),
            "opportunities": TRANSPORT_OPPORTUNITIES,
            "successful_posts": successful,
            "failed_posts": failed,
        },
        "contains_ratings": False,
        "contains_notes": False,
    }


def build_mood_compliance_report(
    rows: Iterable[Mapping[str, Any]],
    *,
    activation_date: date | str,
    as_of_date: date | str,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Build privacy-minimized mood-compliance evidence.

    Input rows may contain only ``mood_date`` and ``source``.  Callers should
    project those columns from ``mood_entries`` before invoking this builder;
    accepting a full health row would unnecessarily expose ratings or notes to
    the evidence process and is therefore rejected.
    """

    activation = _coerce_date(activation_date, field="activation_date")
    as_of = _coerce_date(as_of_date, field="as_of_date")
    if as_of < activation:
        raise ValueError("as_of_date cannot precede activation_date")

    due = activation + timedelta(days=COMPLIANCE_WINDOW_DAYS)
    complete_days = (as_of - activation).days
    opportunities = min(complete_days, COMPLIANCE_WINDOW_DAYS)
    if opportunities:
        window_end = as_of - timedelta(days=1)
        window_start = window_end - timedelta(days=opportunities - 1)
    else:
        window_start = None
        window_end = None

    eligible_dates: set[date] = set()
    backfill_rows_excluded = 0
    duplicate_rows_ignored = 0
    for index, row in enumerate(rows):
        if not isinstance(row, Mapping):
            raise ValueError(f"compliance row {index} must be an object")
        sensitive = _SENSITIVE_MOOD_KEYS.intersection(str(key).lower() for key in row)
        if sensitive:
            raise ValueError(
                f"compliance row {index} contains forbidden rating/note fields: {', '.join(sorted(sensitive))}"
            )
        if set(row) - {"mood_date", "source"}:
            raise ValueError(f"compliance row {index} may contain only mood_date and source")
        if "mood_date" not in row:
            raise ValueError(f"compliance row {index} is missing mood_date")
        if "source" not in row:
            raise ValueError(f"compliance row {index} is missing source")
        mood_date = _coerce_date(row["mood_date"], field=f"rows[{index}].mood_date")
        if window_start is None or window_end is None or not (window_start <= mood_date <= window_end):
            continue
        source = str(row.get("source") or "").strip().lower()
        if source not in _KNOWN_MOOD_SOURCES:
            raise ValueError(
                f"compliance row {index} source must be one of: {', '.join(sorted(_KNOWN_MOOD_SOURCES))}"
            )
        if source == "backfill":
            backfill_rows_excluded += 1
            continue
        if mood_date in eligible_dates:
            duplicate_rows_ignored += 1
            continue
        eligible_dates.add(mood_date)

    eligible = len(eligible_dates)
    if as_of < due:
        status = "not_due"
        action = "none"
    elif eligible >= COMPLIANCE_REQUIRED_DAYS:
        status = "ok"
        action = "none"
    else:
        status = "triggered"
        action = "stop_modeling_fix_logging"

    return {
        "schema_version": "health_data_hub.tripwire.mood_compliance.v1",
        "tripwire": COMPLIANCE_TRIPWIRE,
        "created_at": _created_at(created_at),
        "status": status,
        "action": action,
        "activation_date": activation.isoformat(),
        "as_of_date": as_of.isoformat(),
        "due_date": due.isoformat(),
        "window_start": window_start.isoformat() if window_start else None,
        "window_end": window_end.isoformat() if window_end else None,
        "window_days": COMPLIANCE_WINDOW_DAYS,
        "opportunities": opportunities,
        "minimum_required_days": COMPLIANCE_REQUIRED_DAYS,
        "eligible_unique_non_backfill_days": eligible,
        "backfill_rows_excluded": backfill_rows_excluded,
        "duplicate_rows_ignored": duplicate_rows_ignored,
        "contains_ratings": False,
        "contains_notes": False,
    }


def build_baseline_gate_report(
    runtime_gate_status: str,
    *,
    collecting_state_enforced: bool,
    created_at: datetime | None = None,
) -> dict[str, Any]:
    """Build baseline-tripwire evidence without promoting absence to a pass."""

    runtime_status = str(runtime_gate_status).strip().lower()
    allowed = {"passed", "missing", "ineligible", "failed"}
    if runtime_status not in allowed:
        raise ValueError(f"runtime_gate_status must be one of: {', '.join(sorted(allowed))}")
    if type(collecting_state_enforced) is not bool:
        raise ValueError("collecting_state_enforced must be a boolean")

    if runtime_status == "passed":
        status = "ok"
        action = "none"
    elif collecting_state_enforced:
        status = "fallback_accepted"
        action = "collecting_state_no_override"
    else:
        status = "fallback_required"
        action = "collecting_state_no_override"

    return {
        "schema_version": "health_data_hub.tripwire.baseline_gate.v1",
        "tripwire": BASELINE_TRIPWIRE,
        "created_at": _created_at(created_at),
        "status": status,
        "action": action,
        "runtime_gate_status": runtime_status,
        "collecting_state_enforced": collecting_state_enforced,
        "contains_model_metrics": False,
    }


def _semantic_transport_errors(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    observation = payload.get("observation")
    if not isinstance(observation, Mapping):
        return errors
    try:
        start = _coerce_date(observation.get("window_start"), field="observation.window_start")
        end = _coerce_date(observation.get("window_end"), field="observation.window_end")
    except ValueError as exc:
        errors.append(str(exc))
        return errors
    if (end - start).days != TRANSPORT_OPPORTUNITIES - 1:
        errors.append("transport observation must cover seven consecutive local dates")
    opportunities = observation.get("opportunities")
    successful = observation.get("successful_posts")
    failed = observation.get("failed_posts")
    if all(isinstance(item, int) and not isinstance(item, bool) for item in (opportunities, successful, failed)):
        if successful + failed != opportunities:
            errors.append("successful_posts + failed_posts must equal opportunities")
        fired = failed >= TRANSPORT_FAILURE_THRESHOLD
        fallback_verified = payload.get("fallback_verified") is True
        expected_status = "ok" if not fired else "fallback_accepted" if fallback_verified else "triggered"
        expected_action = "none" if not fired else "streamlit_mobile_form"
        if payload.get("status") != expected_status:
            errors.append(f"transport status must be {expected_status} for the recorded outcomes")
        if payload.get("action") != expected_action:
            errors.append(f"transport action must be {expected_action} for the recorded outcomes")
        if not fired and fallback_verified:
            errors.append("fallback_verified must be false when the transport tripwire did not fire")
    return errors


def _semantic_compliance_errors(payload: Mapping[str, Any], as_of: date | None) -> list[str]:
    errors: list[str] = []
    try:
        activation = _coerce_date(payload.get("activation_date"), field="activation_date")
        report_as_of = _coerce_date(payload.get("as_of_date"), field="as_of_date")
        due = _coerce_date(payload.get("due_date"), field="due_date")
    except ValueError as exc:
        errors.append(str(exc))
        return errors
    expected_due = activation + timedelta(days=COMPLIANCE_WINDOW_DAYS)
    if due != expected_due:
        errors.append(f"due_date must equal activation_date + {COMPLIANCE_WINDOW_DAYS} days")
    if as_of is not None and report_as_of != as_of:
        errors.append(f"compliance evidence as_of_date must equal evaluator date {as_of.isoformat()}")
    if report_as_of < activation:
        errors.append("as_of_date cannot precede activation_date")
        return errors

    opportunities = payload.get("opportunities")
    eligible = payload.get("eligible_unique_non_backfill_days")
    expected_opportunities = min((report_as_of - activation).days, COMPLIANCE_WINDOW_DAYS)
    if opportunities != expected_opportunities:
        errors.append(f"opportunities must be {expected_opportunities} for activation_date/as_of_date")
    if isinstance(opportunities, int) and isinstance(eligible, int) and eligible > opportunities:
        errors.append("eligible_unique_non_backfill_days cannot exceed opportunities")

    if opportunities:
        expected_end = report_as_of - timedelta(days=1)
        expected_start = expected_end - timedelta(days=opportunities - 1)
        if payload.get("window_start") != expected_start.isoformat():
            errors.append(f"window_start must be {expected_start.isoformat()}")
        if payload.get("window_end") != expected_end.isoformat():
            errors.append(f"window_end must be {expected_end.isoformat()}")
    elif payload.get("window_start") is not None or payload.get("window_end") is not None:
        errors.append("window_start/window_end must be null before the first complete opportunity")

    if report_as_of < due:
        expected_status = "not_due"
        expected_action = "none"
    elif isinstance(eligible, int) and eligible >= COMPLIANCE_REQUIRED_DAYS:
        expected_status = "ok"
        expected_action = "none"
    else:
        expected_status = "triggered"
        expected_action = "stop_modeling_fix_logging"
    if payload.get("status") != expected_status:
        errors.append(f"compliance status must be {expected_status} for the recorded window")
    if payload.get("action") != expected_action:
        errors.append(f"compliance action must be {expected_action} for the recorded window")
    return errors


def _semantic_baseline_errors(payload: Mapping[str, Any]) -> list[str]:
    runtime_status = payload.get("runtime_gate_status")
    enforced = payload.get("collecting_state_enforced") is True
    if runtime_status == "passed":
        expected_status = "ok"
        expected_action = "none"
    elif enforced:
        expected_status = "fallback_accepted"
        expected_action = "collecting_state_no_override"
    else:
        expected_status = "fallback_required"
        expected_action = "collecting_state_no_override"
    errors: list[str] = []
    if payload.get("status") != expected_status:
        errors.append(f"baseline status must be {expected_status} for runtime_gate_status={runtime_status}")
    if payload.get("action") != expected_action:
        errors.append(f"baseline action must be {expected_action} for runtime_gate_status={runtime_status}")
    return errors


def validate_typed_tripwire_report(
    kind: str,
    payload: Mapping[str, Any],
    *,
    expected_tripwire: str,
    as_of: date | None = None,
) -> dict[str, Any]:
    """Validate schema plus cross-field semantics and return gate status."""

    # Scan the original object before removing evaluator-owned metadata.  This
    # keeps hidden or underscore-prefixed sensitive fields inside the privacy
    # boundary instead of silently discarding them before schema validation.
    errors = _forbidden_mood_fields(payload)
    clean_payload = _payload_for_validation(payload)
    errors.extend(_schema_errors(kind, clean_payload))
    if clean_payload.get("tripwire") != expected_tripwire:
        errors.append(f"tripwire must be {expected_tripwire}")
    if not errors:
        if kind == MOOD_TRANSPORT_KIND:
            errors.extend(_semantic_transport_errors(clean_payload))
        elif kind == MOOD_COMPLIANCE_KIND:
            errors.extend(_semantic_compliance_errors(clean_payload, as_of))
        elif kind == BASELINE_GATE_KIND:
            errors.extend(_semantic_baseline_errors(clean_payload))

    if errors:
        return {
            "status": "invalid",
            "ok": False,
            "kind": kind,
            "errors": sorted(set(errors)),
        }

    status = str(clean_payload["status"])
    return {
        "status": status,
        "ok": status in {"ok", "fallback_accepted", "not_due"},
        "kind": kind,
        "action": clean_payload.get("action"),
        "errors": [],
    }


__all__ = [
    "BASELINE_GATE_KIND",
    "BASELINE_TRIPWIRE",
    "COMPLIANCE_REQUIRED_DAYS",
    "COMPLIANCE_TRIPWIRE",
    "COMPLIANCE_WINDOW_DAYS",
    "EVIDENCE_KIND_BY_TRIPWIRE",
    "MOOD_COMPLIANCE_KIND",
    "MOOD_TRANSPORT_KIND",
    "TRANSPORT_FAILURE_THRESHOLD",
    "TRANSPORT_OPPORTUNITIES",
    "TRANSPORT_TRIPWIRE",
    "build_baseline_gate_report",
    "build_mood_compliance_report",
    "build_mood_transport_report",
    "validate_typed_tripwire_report",
]

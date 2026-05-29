#!/usr/bin/env python3
"""Collect real local pyEight availability evidence for S03.

The safety invariant for this collector is strict: it may authenticate and
confirm that 8 Sleep data is reachable, but it must not persist raw provider
payloads, provider session tokens, account email, password, user IDs, device
IDs, or exact metric values. The evidence report is intentionally limited to
counts, booleans, and coarse freshness checks.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import importlib.util
import json
import logging
import os
import sys
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.evidence._collector_common import write_report


REQUIRED_ENV = ("PYEIGHT_EMAIL", "PYEIGHT_PASSWORD", "PYEIGHT_TIMEZONE")


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
        try:
            payload = json.loads(decision.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("status") == "fallback_accepted" and payload.get("action") == "oura_only_v1":
            return decision
    return None


def parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value:
        return None
    normalized = value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def coarse_age_days(value: Any) -> int | None:
    parsed = parse_datetime(value)
    if parsed is None:
        return None
    delta = datetime.now(timezone.utc) - parsed.astimezone(timezone.utc)
    return max(0, int(delta.total_seconds() // 86400))


def summarize_user(user: Any) -> dict[str, Any]:
    intervals = getattr(user, "intervals", []) or []
    trends = getattr(user, "trends", []) or []
    last_values = getattr(user, "last_values", {}) or {}
    last_fitness_values = getattr(user, "last_fitness_values", {}) or {}
    current_values = getattr(user, "current_values", {}) or {}

    last_session_age_days = coarse_age_days(last_values.get("date"))
    current_session_age_days = coarse_age_days(current_values.get("date"))
    latest_age_candidates = [age for age in (last_session_age_days, current_session_age_days) if age is not None]

    return {
        "side": getattr(user, "side", "unknown"),
        "interval_count": len(intervals),
        "trend_count": len(trends),
        "profile_present": bool(getattr(user, "user_profile", None)),
        "last_session_date_present": last_values.get("date") is not None,
        "current_session_date_present": current_values.get("date") is not None,
        "latest_session_age_days": min(latest_age_candidates) if latest_age_candidates else None,
        "last_sleep_score_present": last_values.get("score") is not None,
        "last_sleep_breakdown_present": isinstance(last_values.get("breakdown"), dict) and bool(last_values.get("breakdown")),
        "last_heart_rate_present": last_values.get("heart_rate") is not None,
        "last_resp_rate_present": last_values.get("resp_rate") is not None,
        "last_bed_temp_present": last_values.get("bed_temp") is not None,
        "last_fitness_session_date_present": last_fitness_values.get("date") is not None,
        "last_sleep_fitness_score_present": last_fitness_values.get("score") is not None,
    }


async def fetch_pyeight_summary(email: str, password: str, timezone_name: str) -> dict[str, Any]:
    from pyeight.constants import __version__ as pyeight_version
    from pyeight.eight import EightSleep

    device = EightSleep(email=email, password=password, timezone=timezone_name)
    try:
        started = await device.start()
        if not started:
            return {"status": "error", "reason": "pyEight start returned false"}
        await device.update_device_data()
        await device.update_user_data()
        users = [summarize_user(user) for user in device.users.values()]
        has_sleep_interval = any(user["interval_count"] > 0 for user in users)
        fresh_age_days = [
            user["latest_session_age_days"]
            for user in users
            if isinstance(user.get("latest_session_age_days"), int)
        ]
        return {
            "status": "ok" if has_sleep_interval else "blocked_external",
            "pyeight_version": pyeight_version,
            "authenticated": True,
            "device_seen": bool(device.device_id),
            "is_pod": bool(device.is_pod),
            "user_side_count": len(users),
            "has_sleep_interval": has_sleep_interval,
            "freshest_session_age_days": min(fresh_age_days) if fresh_age_days else None,
            "users": users,
            "reason": None if has_sleep_interval else "authenticated but no sleep interval was returned",
        }
    finally:
        await device.stop()


def run_pyeight_summary(email: str, password: str, timezone_name: str) -> dict[str, Any]:
    # pyEight logs provider request failures through the root logging path. Keep
    # evidence collection quiet so provider URLs or library internals do not leak
    # into general logs; structured sanitized evidence is written below instead.
    logger = logging.getLogger("pyeight")
    previous_level = logger.level
    previous_disabled = logger.disabled
    logger.setLevel(logging.CRITICAL + 1)
    logger.disabled = True
    try:
        return asyncio.run(fetch_pyeight_summary(email, password, timezone_name))
    finally:
        logger.disabled = previous_disabled
        logger.setLevel(previous_level)


def safe_exception(exc: Exception) -> dict[str, str]:
    text = str(exc) or str(exc.__cause__ or "") or type(exc).__name__
    if len(text) > 240:
        text = text[:237] + "..."
    return {"error_type": type(exc).__name__, "error": text}


def collect(
    root: Path,
    *,
    runner: Callable[[str, str, str], dict[str, Any]] = run_pyeight_summary,
) -> dict[str, object]:
    decision = fallback_decision_exists(root)
    if decision:
        path = write_report(
            root,
            "pyeight_smoke",
            {
                "status": "fallback_accepted",
                "decision": str(decision.relative_to(root)),
                "fallback": "oura_only_v1",
            },
        )
        return {"status": "fallback_accepted", "evidence": str(path.relative_to(root)), "errors": []}

    spec = importlib.util.find_spec("pyeight")
    if spec is None:
        path = write_report(root, "pyeight_smoke", {"status": "blocked_external", "missing_python_module": "pyeight"})
        return {"status": "blocked_external", "evidence": str(path.relative_to(root)), "errors": ["missing pyeight python module"]}
    distribution_version = pyeight_distribution_version()

    missing_env = [key for key in REQUIRED_ENV if not os.environ.get(key)]
    if missing_env:
        path = write_report(
            root,
            "pyeight_smoke",
            {
                "status": "blocked_external",
                "module_origin": spec.origin,
                "pyeight_distribution_version": distribution_version,
                "missing_env": missing_env,
            },
        )
        return {"status": "blocked_external", "evidence": str(path.relative_to(root)), "errors": [f"missing {', '.join(missing_env)}"]}

    try:
        summary = runner(os.environ["PYEIGHT_EMAIL"], os.environ["PYEIGHT_PASSWORD"], os.environ["PYEIGHT_TIMEZONE"])
    except Exception as exc:
        payload = {
            "status": "error",
            "module_origin": spec.origin,
            "pyeight_distribution_version": distribution_version,
            **safe_exception(exc),
        }
        path = write_report(root, "pyeight_smoke", payload)
        return {"status": "error", "evidence": str(path.relative_to(root)), "errors": [payload["error"]]}

    status = str(summary.get("status") or "error")
    if status not in {"ok", "blocked_external", "error"}:
        status = "error"
        summary = {**summary, "reason": "collector runner returned invalid status"}
    payload = {
        "status": status,
        "module_origin": spec.origin,
        "pyeight_distribution_version": distribution_version,
        "sanitization": "counts_booleans_only_no_raw_payloads_or_identifiers",
        **summary,
    }
    path = write_report(root, "pyeight_smoke", payload)
    errors: list[str] = []
    if status != "ok":
        errors.append(str(payload.get("reason") or "pyEight smoke evidence did not return ok"))
    return {"status": status, "evidence": str(path.relative_to(root)), "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect pyEight smoke evidence.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = collect(Path(args.root).resolve())
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["status"])
    return 0 if report["status"] in {"ok", "fallback_accepted"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

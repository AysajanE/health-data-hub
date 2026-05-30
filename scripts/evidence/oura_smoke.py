#!/usr/bin/env python3
"""Collect real local Oura API smoke evidence for S03."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.evidence._collector_common import coarse_freshness_bucket
from scripts.evidence._collector_common import ensure_open_failure
from scripts.evidence._collector_common import env_presence_markers
from scripts.evidence._collector_common import sanitize_text
from scripts.evidence._collector_common import write_report


COLLECTOR = "oura_smoke"
DEFAULT_WINDOW_DAYS = 7
PROVIDER_PATH = "direct_oura_api_v2_periodic_pull"
SANITIZATION_NOTE = "aggregate_counts_booleans_and_freshness_buckets_only"
REQUEST_TIMEOUT_SECONDS = 20
SLEEP_DATE_KEYS = ("day", "date", "bedtime_end", "bedtime_start")
SLICE_ID = "S03"
BLOCKED_EXTERNAL_FAILURE_CLASS = "blocked_external_missing_evidence"


def _window_days(start: date, end: date) -> int:
    return max(0, (end - start).days)


def _resolve_query_window() -> tuple[date, date]:
    end_date = os.environ.get("OURA_SLEEP_END_DATE") or date.today().isoformat()
    end = date.fromisoformat(end_date)
    start_date = os.environ.get("OURA_SLEEP_START_DATE") or (end - timedelta(days=DEFAULT_WINDOW_DAYS)).isoformat()
    start = date.fromisoformat(start_date)
    return start, end


def _report_payload(
    status: str,
    *,
    window_days: int,
    extra: dict[str, object] | None = None,
) -> dict[str, object]:
    payload: dict[str, object] = {
        "status": status,
        "provider_path": PROVIDER_PATH,
        "query_window_days": window_days,
        "sanitization": SANITIZATION_NOTE,
    }
    if extra:
        payload.update(extra)
    return payload


def _blocked_external_result(root: Path, path: Path, errors: list[str]) -> dict[str, object]:
    ensure_open_failure(
        root,
        slice_id=SLICE_ID,
        failure_class=BLOCKED_EXTERNAL_FAILURE_CLASS,
        severity="medium",
        description="Required Oura smoke evidence is missing or blocked.",
        action_taken="Recorded blocked-external Oura evidence without logging secrets or fabricating provider success.",
        evidence_path=str(path.relative_to(root)),
    )
    return {"status": "blocked_external", "evidence": str(path.relative_to(root)), "errors": errors}


def collect(root: Path, offline: bool = False) -> dict[str, object]:
    token = (os.environ.get("OURA_ACCESS_TOKEN") or "").strip()
    window_days = DEFAULT_WINDOW_DAYS
    try:
        start, end = _resolve_query_window()
        window_days = _window_days(start, end)
    except ValueError:
        start = end = date.today()
    if not token:
        path = write_report(
            root,
            COLLECTOR,
            _report_payload(
                "blocked_external",
                window_days=window_days,
                extra={
                    "env": env_presence_markers(("OURA_ACCESS_TOKEN",), {"OURA_ACCESS_TOKEN": token}),
                    "missing_env": ["OURA_ACCESS_TOKEN"],
                    "reason": "missing OURA_ACCESS_TOKEN",
                },
            ),
        )
        return _blocked_external_result(root, path, ["missing OURA_ACCESS_TOKEN"])

    if offline:
        path = write_report(
            root,
            COLLECTOR,
            _report_payload(
                "blocked_external",
                window_days=window_days,
                extra={
                    "env": env_presence_markers(("OURA_ACCESS_TOKEN",), {"OURA_ACCESS_TOKEN": token}),
                    "network": "skipped",
                    "offline": True,
                    "reason": "offline mode cannot satisfy live Oura smoke evidence",
                },
            ),
        )
        return _blocked_external_result(root, path, ["offline mode cannot satisfy tripwire evidence"])

    try:
        start, end = _resolve_query_window()
        window_days = _window_days(start, end)
    except ValueError as exc:
        message = "invalid Oura sleep window configuration"
        path = write_report(
            root,
            COLLECTOR,
            _report_payload(
                "error",
                window_days=window_days,
                extra={"error_type": type(exc).__name__, "error": message},
            ),
        )
        return {"status": "error", "evidence": str(path.relative_to(root)), "errors": [message]}

    query = urlencode({"start_date": start.isoformat(), "end_date": end.isoformat()})
    request = urllib.request.Request(
        f"https://api.ouraring.com/v2/usercollection/sleep?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
            status_code = response.status
            body = response.read(200000).decode("utf-8", errors="replace")
        payload = json.loads(body)
        records = payload.get("data")
        if not isinstance(records, list):
            path = write_report(
                root,
                COLLECTOR,
                _report_payload(
                    "error",
                    window_days=window_days,
                    extra={"http_status": status_code, "reason": "sleep response missing data list"},
                ),
            )
            return {"status": "error", "evidence": str(path.relative_to(root)), "errors": ["sleep response missing data list"]}
        if not records:
            path = write_report(
                root,
                COLLECTOR,
                _report_payload(
                    "blocked_external",
                    window_days=window_days,
                    extra={
                        "http_status": status_code,
                        "latest_sleep_bucket": "unknown",
                        "record_count": 0,
                        "reason": "no recent Oura sleep records returned",
                        "sleep_data_present": False,
                    },
                ),
            )
            return _blocked_external_result(root, path, ["no recent Oura sleep records returned"])
        path = write_report(
            root,
            COLLECTOR,
            _report_payload(
                "ok",
                window_days=window_days,
                extra={
                    "http_status": status_code,
                    "latest_sleep_bucket": coarse_freshness_bucket(records, reference_date=end, date_keys=SLEEP_DATE_KEYS),
                    "record_count": len(records),
                    "sleep_data_present": True,
                },
            ),
        )
        return {"status": "ok", "evidence": str(path.relative_to(root)), "errors": []}
    except json.JSONDecodeError as exc:
        message = "Oura API returned malformed JSON"
        path = write_report(
            root,
            COLLECTOR,
            _report_payload(
                "error",
                window_days=window_days,
                extra={"error_type": type(exc).__name__, "error": message},
            ),
        )
        return {"status": "error", "evidence": str(path.relative_to(root)), "errors": [message]}
    except urllib.error.HTTPError as exc:
        if exc.code in {400, 401, 403, 404, 429}:
            status = "blocked_external"
            reason = "Oura API rejected the smoke request"
        else:
            status = "error"
            reason = "Oura API returned a provider-side failure"
        path = write_report(
            root,
            COLLECTOR,
            _report_payload(
                status,
                window_days=window_days,
                extra={"error_type": type(exc).__name__, "http_status": exc.code, "reason": reason},
            ),
        )
        if status == "blocked_external":
            return _blocked_external_result(root, path, [reason])
        return {"status": status, "evidence": str(path.relative_to(root)), "errors": [reason]}
    except urllib.error.URLError as exc:
        message = sanitize_text(str(exc.reason) or type(exc).__name__, secret_values=[token])
        path = write_report(
            root,
            COLLECTOR,
            _report_payload(
                "blocked_external",
                window_days=window_days,
                extra={"error_type": type(exc).__name__, "reason": message},
            ),
        )
        return _blocked_external_result(root, path, [message])
    except Exception as exc:
        message = sanitize_text(str(exc) or type(exc).__name__, secret_values=[token])
        path = write_report(
            root,
            COLLECTOR,
            _report_payload(
                "error",
                window_days=window_days,
                extra={"error_type": type(exc).__name__, "error": message},
            ),
        )
        return {"status": "error", "evidence": str(path.relative_to(root)), "errors": [message]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect Oura smoke evidence without logging secrets.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = collect(Path(args.root).resolve(), offline=args.offline)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

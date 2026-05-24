#!/usr/bin/env python3
"""Collect real local Oura API smoke evidence for S03."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlencode

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.evidence._collector_common import redact_env, write_report


def collect(root: Path, offline: bool = False) -> dict[str, object]:
    token = os.environ.get("OURA_ACCESS_TOKEN")
    if not token:
        path = write_report(root, "oura_smoke", {"status": "blocked_external", "missing_env": ["OURA_ACCESS_TOKEN"]})
        return {"status": "blocked_external", "evidence": str(path.relative_to(root)), "errors": ["missing OURA_ACCESS_TOKEN"]}

    if offline:
        path = write_report(root, "oura_smoke", {"status": "blocked_external", "offline": True, "network": "skipped", "env": redact_env({"OURA_ACCESS_TOKEN": token})})
        return {"status": "blocked_external", "evidence": str(path.relative_to(root)), "errors": ["offline mode cannot satisfy tripwire evidence"]}

    end_date = os.environ.get("OURA_SLEEP_END_DATE") or date.today().isoformat()
    try:
        end = date.fromisoformat(end_date)
        start_date = os.environ.get("OURA_SLEEP_START_DATE") or (end - timedelta(days=7)).isoformat()
        date.fromisoformat(start_date)
    except ValueError as exc:
        path = write_report(root, "oura_smoke", {"status": "error", "error_type": type(exc).__name__, "error": str(exc)})
        return {"status": "error", "evidence": str(path.relative_to(root)), "errors": [str(exc)]}
    query = urlencode({"start_date": start_date, "end_date": end_date})
    request = urllib.request.Request(
        f"https://api.ouraring.com/v2/usercollection/sleep?{query}",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status_code = response.status
            body = response.read(200000).decode("utf-8", errors="replace")
        payload = json.loads(body)
        records = payload.get("data")
        if not isinstance(records, list):
            path = write_report(root, "oura_smoke", {"status": "error", "http_status": status_code, "start_date": start_date, "end_date": end_date, "reason": "sleep response missing data list"})
            return {"status": "error", "evidence": str(path.relative_to(root)), "errors": ["sleep response missing data list"]}
        if not records:
            path = write_report(root, "oura_smoke", {"status": "blocked_external", "http_status": status_code, "start_date": start_date, "end_date": end_date, "record_count": 0})
            return {"status": "blocked_external", "evidence": str(path.relative_to(root)), "errors": ["no recent Oura sleep records returned"]}
        path = write_report(root, "oura_smoke", {"status": "ok", "http_status": status_code, "start_date": start_date, "end_date": end_date, "record_count": len(records)})
        return {"status": "ok", "evidence": str(path.relative_to(root)), "errors": []}
    except json.JSONDecodeError as exc:
        path = write_report(root, "oura_smoke", {"status": "error", "error_type": type(exc).__name__, "error": str(exc)})
        return {"status": "error", "evidence": str(path.relative_to(root)), "errors": [str(exc)]}
    except Exception as exc:
        path = write_report(root, "oura_smoke", {"status": "error", "error_type": type(exc).__name__, "error": str(exc)})
        return {"status": "error", "evidence": str(path.relative_to(root)), "errors": [str(exc)]}


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

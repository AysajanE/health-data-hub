#!/usr/bin/env python3
"""Collect real local Oura API smoke evidence for S03."""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from pathlib import Path

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

    request = urllib.request.Request(
        "https://api.ouraring.com/v2/usercollection/personal_info",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            status_code = response.status
            body = response.read(4000).decode("utf-8", errors="replace")
        path = write_report(root, "oura_smoke", {"status": "ok", "http_status": status_code, "body_sample_chars": len(body)})
        return {"status": "ok", "evidence": str(path.relative_to(root)), "errors": []}
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

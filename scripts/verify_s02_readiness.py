#!/usr/bin/env python3
"""Verify that AutoKeel may start the S02 compile path.

This is a pre-launch readiness check, not a completion gate. It verifies the
reviewed lane decision and static safety prerequisites before S02 PO execution.
The S02 autonomous review artifacts are still required before completion.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_autonomous_review_exists import check_review
from scripts.check_no_tracked_data import check_no_tracked_data
from scripts.swr_lane_policy import validate_swr_lane_requirements


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def verify_s02_readiness(root: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    slices = load_json(root / "ops" / "autonomy" / "slices.json", [])
    if not isinstance(slices, list):
        return {
            "status": "error",
            "errors": ["ops/autonomy/slices.json must contain a list"],
            "warnings": [],
            "checks": checks,
        }

    s01 = next((item for item in slices if item.get("id") == "S01"), None)
    s02 = next((item for item in slices if item.get("id") == "S02"), None)
    checks["s01_status"] = s01.get("status") if isinstance(s01, dict) else None
    checks["s02_status"] = s02.get("status") if isinstance(s02, dict) else None

    if not isinstance(s01, dict):
        errors.append("S01 missing from slices.json")
    elif s01.get("status") != "complete":
        errors.append(f"S01 must be complete before S02 readiness: {s01.get('status')}")

    if not isinstance(s02, dict):
        errors.append("S02 missing from slices.json")
        s02 = {}

    if isinstance(s02, dict):
        errors.extend(validate_swr_lane_requirements(root, s02))
        checks["lane_decision"] = s02.get("lane_decision")
        checks["review_artifacts"] = s02.get("review_artifacts", [])

        review_report = check_review(root, "S02") if s02 else {"status": "error", "errors": ["S02 missing"]}
        if review_report["status"] != "ok":
            errors.extend(review_report["errors"])

    tracked = check_no_tracked_data(root)
    if tracked["status"] != "ok":
        errors.extend(tracked["errors"])
    warnings.extend(tracked.get("warnings", []))

    return {
        "status": "ok" if not errors else "error",
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify S02 AutoKeel readiness.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_s02_readiness(Path(args.root).resolve())
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        for warning in report["warnings"]:
            print(f"WARNING: {warning}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

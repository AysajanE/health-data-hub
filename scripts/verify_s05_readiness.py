#!/usr/bin/env python3
"""Verify S05 is ready for a high-risk SWR launch."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_no_tracked_data import check_no_tracked_data
from scripts.swr_lane_policy import validate_swr_lane_requirements
from scripts.verify_s05_provider_policy import verify_s05_provider_policy


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def iter_jsonl(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def verify_s05_readiness(root: Path) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    slices = load_json(root / "ops/autonomy/slices.json", [])
    if not isinstance(slices, list):
        return {"status": "error", "errors": ["ops/autonomy/slices.json must contain a list"], "warnings": [], "checks": checks}

    by_id = {item.get("id"): item for item in slices if isinstance(item, dict)}
    s05 = by_id.get("S05")
    if not isinstance(s05, dict):
        return {"status": "error", "errors": ["S05 not found in slices.json"], "warnings": [], "checks": checks}

    for dep in ("S03", "S04"):
        status = by_id.get(dep, {}).get("status")
        checks[f"{dep.lower()}_status"] = status
        if status != "complete":
            errors.append(f"{dep} must be complete before S05 launch: {status}")

    checks["s05_status"] = s05.get("status")
    if s05.get("status") not in {"pending", "replan_required", "waiting_for_playbook", "evidence_ready"}:
        errors.append(f"S05 is not in an actionable pre-launch status: {s05.get('status')}")

    errors.extend(validate_swr_lane_requirements(root, s05))

    provider = verify_s05_provider_policy(root)
    checks["provider_policy"] = provider.get("checks", {})
    if provider["status"] != "ok":
        errors.extend(f"provider policy: {error}" for error in provider["errors"])
    warnings.extend(f"provider policy: {warning}" for warning in provider.get("warnings", []))

    if not os.environ.get("OPENAI_API_KEY", "").strip():
        errors.append("OPENAI_API_KEY is required for S05 keel-swr launch; secret_values_logged=false")
    checks["swr_required_env"] = {"OPENAI_API_KEY": "[SET]" if os.environ.get("OPENAI_API_KEY", "").strip() else "[UNSET]"}

    tracked = check_no_tracked_data(root)
    if tracked["status"] != "ok":
        errors.extend(tracked["errors"])
    warnings.extend(tracked.get("warnings", []))

    failures = list(iter_jsonl(root / "ops/autonomy/failure_ledger.jsonl") or [])
    open_high = [
        row for row in failures
        if row.get("open", True)
        and row.get("severity") in {"high", "critical"}
        and row.get("slice") in {"S05", "GLOBAL"}
    ]
    checks["open_high_or_critical_failures_for_s05_or_global"] = len(open_high)
    if open_high:
        errors.append(f"open high/critical S05 or GLOBAL failures block launch: {len(open_high)}")

    state = load_json(root / "ops/autonomy/autonomy_state.json", {})
    checks["active_run"] = state.get("active_run")
    checks["active_swr_run"] = state.get("active_swr_run")
    if state.get("active_run"):
        errors.append("active_run must be null before S05 launch")
    if state.get("active_swr_run"):
        errors.append("active_swr_run must be null before S05 launch")

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify S05 launch readiness.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_s05_readiness(Path(args.root))
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

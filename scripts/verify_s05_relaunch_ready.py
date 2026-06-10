#!/usr/bin/env python3
"""Verify S05 is ready for exactly one bounded relaunch tick."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.autonomy.autokeel import AutoKeel


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else default


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run(root: Path, command: str) -> dict[str, Any]:
    proc = subprocess.run(
        shlex.split(command),
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return {
        "command": command,
        "exit_code": proc.returncode,
        "stdout_tail": proc.stdout[-3000:],
        "stderr_tail": proc.stderr[-3000:],
    }


def verify(root: Path) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    slices = load_json(root / "ops/autonomy/slices.json", [])
    s05 = next((item for item in slices if isinstance(item, dict) and item.get("id") == "S05"), None)
    if not isinstance(s05, dict):
        return {"status": "error", "errors": ["S05 not found"], "warnings": [], "checks": checks}

    checks["s05_status"] = s05.get("status")
    if s05.get("status") != "blocked_compile_inputs":
        errors.append(f"S05 must be blocked_compile_inputs for same-run review repair relaunch: {s05.get('status')}")

    repair = s05.get("swr_review_repair")
    checks["swr_review_repair"] = repair
    if not isinstance(repair, dict):
        errors.append("S05 swr_review_repair is missing")
    else:
        if repair.get("status") != "planned":
            errors.append(f"S05 repair status must be planned: {repair.get('status')}")
        if repair.get("repair_action") not in {"rerun_review_lane", "rerun_single_stage"}:
            errors.append(f"S05 repair_action must be a runnable same-run repair: {repair.get('repair_action')}")
        if repair.get("repair_stage_id") != "source_authority_map":
            errors.append(f"S05 repair_stage_id must be source_authority_map: {repair.get('repair_stage_id')}")
        manifest = str(repair.get("run_manifest") or "")
        if not manifest or not (root / manifest).exists():
            errors.append(f"S05 repair manifest missing: {manifest}")

    state = load_json(root / "ops/autonomy/autonomy_state.json", {})
    checks["active_run"] = state.get("active_run")
    checks["active_swr_run"] = state.get("active_swr_run")
    if state.get("active_run"):
        errors.append("active_run must be null")
    if state.get("active_swr_run"):
        errors.append("active_swr_run must be null before relaunching the planned repair")

    open_high = [
        row for row in iter_jsonl(root / "ops/autonomy/failure_ledger.jsonl")
        if row.get("open", True)
        and row.get("severity") in {"high", "critical"}
        and row.get("slice") in {"S05", "GLOBAL"}
    ]
    checks["open_high_or_critical_failures"] = len(open_high)
    if open_high:
        errors.append("open high/critical S05/GLOBAL failures remain: " + ", ".join(str(row.get("failure_class")) for row in open_high))

    operator = AutoKeel(root=root, dry_run=True)
    snapshots = operator.snapshot_dry_run_state()
    try:
        budget_result = operator.failure_budget_exceeded(s05)
    finally:
        operator.restore_dry_run_state(snapshots)
    checks["failure_budget"] = {
        "exit_code": budget_result.exit_code,
        "stdout": budget_result.stdout,
        "stderr": budget_result.stderr,
    }
    if budget_result.exit_code != 0:
        errors.append(f"S05 failure budget is not launch-ready: {budget_result.stderr}")

    # Hard release-artifact semantics, conditional on the actual lane count:
    # over cap -> a valid, unconsumed, unexpired release keyed to the current
    # plan is REQUIRED; within cap -> a consumed release must not linger at the
    # canonical path (it must be archived with a timestamped name).
    repair_policy = operator.policy.get("repair_budget", {})
    lane_cap = int(repair_policy.get("max_closed_swr_review_lane_repairs_per_slice", 3))
    lane_rows = [
        row
        for row in iter_jsonl(root / "ops/autonomy/failure_ledger.jsonl")
        if row.get("slice") == "S05"
        and not row.get("open", True)
        and operator.failure_closure_evidence_valid(row)
        and operator.failure_counts_against_closed_repair_budget(row)
        and operator.repair_budget_scope(row) == "swr_review_lane"
    ]
    checks["swr_review_lane_closed_repairs"] = {"count": len(lane_rows), "cap": lane_cap}
    release_path = root / "docs/evidence/s05-swr-review-lane-budget-release.json"
    release = load_json(release_path, {})
    checks["release_path"] = str(release_path.relative_to(root))
    checks["release_verdict"] = release.get("verdict") if isinstance(release, dict) else None
    checks["release_consumed_at"] = release.get("consumed_at") if isinstance(release, dict) else None
    if len(lane_rows) > lane_cap:
        release_check = operator.swr_review_lane_budget_release_valid(s05)
        if not release_check.ok:
            errors.append(f"S05 lane count exceeds cap and no valid budget release exists: {release_check.stderr}")
    elif isinstance(release, dict) and release.get("consumed_at"):
        errors.append(
            "a consumed S05 budget release lingers at the canonical path; archive it with a "
            "timestamped suffix before relaunch so future budget reasoning cannot misread it"
        )

    commands = [
        "python scripts/verify_failure_ledger.py --slice S05 --json",
        "python scripts/verify_autokeel_invariants.py --json",
        "python scripts/verify_s05_provider_policy.py --json",
        "python scripts/check_no_tracked_data.py --json",
    ]
    if isinstance(repair, dict) and repair.get("run_manifest"):
        commands.insert(0, f"python scripts/verify_swr_review_history.py {repair['run_manifest']} --json")

    command_results = [run(root, command) for command in commands]
    checks["commands"] = command_results
    for row in command_results:
        if row["exit_code"] != 0:
            errors.append(f"launch-readiness command failed: {row['command']}")

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify S05 relaunch readiness.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify(Path(args.root))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Materialize a one-time SWR review-lane budget release.

This is not a global budget increase. It authorizes exactly one next AutoKeel
tick for an already-planned same-run SWR review-lane repair.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import yaml


BUDGET_RE = re.compile(r"(?P<actual>\d+)\s*>\s*(?P<limit>\d+)")


def now() -> datetime:
    return datetime.now().astimezone()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def run_command(root: Path, command: str) -> dict[str, Any]:
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


def plan_key(slice_id: str, repair: dict[str, Any]) -> str:
    material = {
        "slice": slice_id,
        "run_id": repair.get("run_id"),
        "run_manifest": repair.get("run_manifest"),
        "repair_action": repair.get("repair_action"),
        "repair_stage_id": repair.get("repair_stage_id"),
        "created_at": repair.get("created_at"),
    }
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()


def latest_open_budget_stop(rows: list[dict[str, Any]], slice_id: str, required_text: str) -> dict[str, Any] | None:
    candidates = []
    for row in rows:
        if row.get("slice") != slice_id:
            continue
        if row.get("failure_class") != "failure_budget_exceeded":
            continue
        if not row.get("open", True):
            continue
        text = " ".join(
            str(row.get(key) or "")
            for key in ("description", "reason", "action_taken", "failure_path")
        )
        if required_text.lower() in text.lower() or "review-lane" in text.lower():
            candidates.append(row)
    return candidates[-1] if candidates else None


def verify_release(root: Path, slice_id: str, *, write_evidence: bool = False) -> dict[str, Any]:
    root = root.resolve()
    slice_id = slice_id.upper()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    policy_path = root / "ops/autonomy/policy.yaml"
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    release_policy = policy.get("swr_review_lane_budget_release") or {}
    if not release_policy.get("auto_authorized"):
        errors.append("swr_review_lane_budget_release.auto_authorized must be true")

    slices = load_json(root / "ops/autonomy/slices.json", [])
    target = next((item for item in slices if isinstance(item, dict) and item.get("id") == slice_id), None)
    if not isinstance(target, dict):
        return {"status": "error", "errors": [f"unknown slice: {slice_id}"], "warnings": [], "checks": checks}

    checks["slice_status"] = target.get("status")
    if target.get("status") not in {"blocked", "blocked_compile_inputs"}:
        errors.append(f"{slice_id} must be blocked or blocked_compile_inputs before release: {target.get('status')}")

    repair = target.get("swr_review_repair")
    checks["swr_review_repair"] = repair
    if not isinstance(repair, dict):
        errors.append(f"{slice_id} must have a planned swr_review_repair")
        repair = {}
    else:
        if repair.get("status") != "planned":
            errors.append(f"swr_review_repair.status must be planned: {repair.get('status')}")
        if repair.get("repair_action") != release_policy.get("required_repair_action", "rerun_review_lane"):
            errors.append(f"repair_action must be {release_policy.get('required_repair_action')}: {repair.get('repair_action')}")
        stage = str(repair.get("repair_stage_id") or "")
        allowed_stages = set(release_policy.get("allowed_review_lane_repair_stages") or [])
        if stage not in allowed_stages:
            errors.append(f"repair_stage_id is not allowed for review-lane budget release: {stage}")
        manifest_rel = str(repair.get("run_manifest") or "")
        if not manifest_rel or not (root / manifest_rel).exists():
            errors.append(f"repair run_manifest missing: {manifest_rel}")

    state = load_json(root / "ops/autonomy/autonomy_state.json", {})
    checks["active_run"] = state.get("active_run")
    checks["active_swr_run"] = state.get("active_swr_run")
    if release_policy.get("require_no_active_po_run", True) and state.get("active_run"):
        errors.append("active_run must be null")
    if release_policy.get("require_no_active_swr_run", True) and state.get("active_swr_run"):
        errors.append("active_swr_run must be null")

    rows = iter_jsonl(root / "ops/autonomy/failure_ledger.jsonl")
    open_high = [
        row for row in rows
        if row.get("slice") == slice_id
        and row.get("open", True)
        and row.get("severity") in {"high", "critical"}
    ]
    budget_stop = latest_open_budget_stop(
        rows,
        slice_id,
        str(release_policy.get("required_open_failure_text") or "SWR review-lane repair budget exceeded"),
    )
    if not budget_stop:
        errors.append("no matching open SWR review-lane failure_budget_exceeded row found")
    else:
        text = " ".join(str(budget_stop.get(key) or "") for key in ("description", "reason", "action_taken"))
        match = BUDGET_RE.search(text)
        if not match:
            errors.append("budget stop text does not contain an actual > limit count")
        else:
            actual = int(match.group("actual"))
            limit = int(match.group("limit"))
            checks["budget_stop"] = {"actual": actual, "limit": limit, "overage": actual - limit}
            max_overage = int(release_policy.get("max_overage", 1))
            if actual - limit > max_overage:
                errors.append(f"budget overage exceeds policy: {actual} > {limit} with max_overage={max_overage}")

    other_high = [
        row for row in open_high
        if row is not budget_stop
    ]
    checks["other_open_high_or_critical_failures"] = len(other_high)
    if release_policy.get("require_no_other_open_high_or_critical_failures", True) and other_high:
        errors.append(
            "other open high/critical failures remain: "
            + ", ".join(str(row.get("failure_class") or "unknown") for row in other_high)
        )

    commands = [
        "git diff --check",
        "python scripts/check_no_tracked_data.py --json",
    ]

    manifest_rel = str(repair.get("run_manifest") or "")
    if manifest_rel:
        commands.append(f"python scripts/verify_swr_review_history.py {manifest_rel} --json")

    commands.append(f"python scripts/verify_autokeel_stability_checkpoint.py {slice_id} --json")

    command_results = [run_command(root, command) for command in commands]
    checks["commands"] = command_results
    for row in command_results:
        if row["exit_code"] != 0:
            errors.append(f"release prerequisite command failed: {row['command']}")

    release_key = plan_key(slice_id, repair) if repair else ""
    created_at = now()
    expires_at = created_at + timedelta(hours=int(release_policy.get("expires_hours", 24)))
    evidence_rel = f"docs/evidence/{slice_id.lower()}-swr-review-lane-budget-release.json"
    release_payload = {
        "schema_version": release_policy.get("schema_version", "autokeel.swr_review_lane_budget_release.v1"),
        "created_at": created_at.isoformat(timespec="seconds"),
        "expires_at": expires_at.isoformat(timespec="seconds"),
        "slice": slice_id,
        "release_type": "swr_review_lane_budget",
        "verdict": "pass" if not errors else "fail",
        "allow_one_next_repair_tick": not errors,
        "consumed_at": None,
        "release_key": release_key,
        "repair_plan": {
            "run_id": repair.get("run_id"),
            "run_manifest": repair.get("run_manifest"),
            "repair_action": repair.get("repair_action"),
            "repair_stage_id": repair.get("repair_stage_id"),
            "created_at": repair.get("created_at"),
        },
        "budget_stop": checks.get("budget_stop"),
        "commands": command_results,
        "errors": errors,
        "warnings": warnings,
        "safety": {
            "does_not_raise_global_budget": True,
            "does_not_authorize_fresh_swr_launch": True,
            "does_not_authorize_po_start": True,
            "same_run_review_lane_only": True,
        },
    }

    if write_evidence:
        write_json_atomic(root / evidence_rel, release_payload)

    report = {
        "status": "ok" if not errors else "error",
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
        "evidence_path": evidence_rel if write_evidence else None,
        "release_key": release_key,
    }
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify and optionally write SWR review-lane budget release evidence.")
    parser.add_argument("slice_id")
    parser.add_argument("--root", default=".")
    parser.add_argument("--write-evidence", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_release(Path(args.root), args.slice_id, write_evidence=args.write_evidence)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

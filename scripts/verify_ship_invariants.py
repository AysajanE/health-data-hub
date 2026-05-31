#!/usr/bin/env python3
"""Verify terminal ship invariants for a completed AutoKeel slice."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


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


def git_stdout(root: Path, *argv: str) -> tuple[bool, str]:
    proc = subprocess.run(["git", *argv], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return proc.returncode == 0, proc.stdout.strip() if proc.returncode == 0 else proc.stderr.strip()


def verify_ship_invariants(root: Path, slice_id: str) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}
    normalized = slice_id.upper()
    branch = f"ship/{normalized.lower()}"
    slices = load_json(root / "ops/autonomy/slices.json", [])
    slice_ = next((item for item in slices if isinstance(item, dict) and item.get("id") == normalized), None)
    if not isinstance(slice_, dict):
        return {"status": "error", "errors": [f"slice not found: {normalized}"], "warnings": [], "checks": checks}

    recorded_branch = str(slice_.get("ship_branch") or "")
    recorded_commit = str(slice_.get("ship_commit") or "")
    run_id = str(slice_.get("run_id") or "")
    checks["recorded_branch"] = recorded_branch
    checks["recorded_commit"] = recorded_commit
    checks["run_id"] = run_id
    if recorded_branch != branch:
        errors.append(f"ship branch mismatch: {recorded_branch} != {branch}")
    ok, branch_head = git_stdout(root, "rev-parse", f"{branch}^{{commit}}")
    if not ok:
        errors.append(f"{branch} does not exist: {branch_head}")
    checks["branch_head"] = branch_head if ok else None
    if ok and recorded_commit and branch_head != recorded_commit:
        errors.append(f"{branch} HEAD differs from recorded ship_commit: {branch_head} != {recorded_commit}")
    if recorded_commit:
        reachable, message = git_stdout(root, "cat-file", "-e", recorded_commit)
        checks["ship_commit_reachable"] = reachable
        if not reachable:
            errors.append(f"ship_commit is not reachable: {message}")
    if run_id:
        run_state = load_json(root / ".local/automation/plan_orchestrator/runs" / run_id / "run_state.json", {})
        run_branch = str(run_state.get("run_branch_name") or f"orchestrator/run/{run_id}")
        ok_run, run_head = git_stdout(root, "rev-parse", f"{run_branch}^{{commit}}")
        checks["run_state_branch"] = run_branch
        checks["run_state_branch_head"] = run_head if ok_run else None
        if ok_run and recorded_commit and run_head != recorded_commit:
            errors.append(f"ship_commit does not match terminal run branch HEAD: {recorded_commit} != {run_head}")
        elif not ok_run:
            warnings.append(f"could not resolve run_state branch: {run_branch}")

    events = list(iter_jsonl(root / "ops/autonomy/events.jsonl") or [])
    review_events = [
        event
        for event in events
        if event.get("slice") == normalized
        and event.get("event") == "review_artifacts_validated"
        and ".local/autokeel/ship-checkouts" in str((event.get("details") or {}).get("cwd") or "")
    ]
    acceptance_events = [
        event
        for event in events
        if event.get("slice") == normalized
        and event.get("event") == "slice_acceptance_passed"
        and ".local/autokeel/ship-checkouts" in str((event.get("details") or {}).get("cwd") or "")
    ]
    review_artifacts = slice_.get("review_artifacts") if isinstance(slice_, dict) else []
    checks["review_artifacts_required"] = bool(review_artifacts)
    if review_artifacts and not review_events:
        errors.append("review validation did not record a detached ship worktree cwd")
    if not acceptance_events:
        errors.append("verify_slice did not record a detached ship worktree cwd")
    ship_events = [
        event for event in events if event.get("slice") == normalized and event.get("event") == "slice_ship_branch_created"
    ]
    if ship_events:
        details = ship_events[-1].get("details") or {}
        before = details.get("operator_branch_before")
        after = details.get("operator_branch_after")
        if before and after and before != after:
            errors.append(f"operator checkout branch changed during ship: {before} -> {after}")
        elif not before or not after:
            warnings.append("ship event lacks operator branch before/after fields")
    else:
        errors.append("slice_ship_branch_created event missing")

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify AutoKeel ship invariants.")
    parser.add_argument("slice_id")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_ship_invariants(Path(args.root).resolve(), args.slice_id)
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

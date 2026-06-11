#!/usr/bin/env python3
"""Verify the repository is authorized to start the fully autonomous S05-S09 loop."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


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
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }


def next_actionable_slice(slices: list[dict[str, Any]], completed: set[str]) -> str | None:
    blocked = {"blocked", "blocked_external", "blocked_external_waiting_for_evidence", "blocked_compile_inputs", "complete"}
    actionable = {"pending", "waiting_for_playbook", "replan_required", "evidence_ready"}
    for item in slices:
        if not item.get("required"):
            continue
        if item.get("status", "pending") in blocked:
            continue
        deps = set(item.get("depends_on", []))
        if deps and not deps.issubset(completed):
            continue
        if item.get("status", "pending") in actionable:
            return str(item.get("id") or "")
    return None


def verify_launch(root: Path) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    policy_path = root / "ops/autonomy/authorization_policy.yaml"
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8")) or {}
    launch = policy.get("remaining_v1_autonomous_launch") or {}
    if not launch.get("auto_authorized"):
        errors.append("remaining_v1_autonomous_launch.auto_authorized must be true")

    slices = load_json(root / "ops/autonomy/slices.json", [])
    state = load_json(root / "ops/autonomy/autonomy_state.json", {})
    if not isinstance(slices, list):
        return {"status": "error", "errors": ["slices.json must contain a list"], "warnings": [], "checks": checks}

    by_id = {item.get("id"): item for item in slices if isinstance(item, dict)}
    completed = {sid for sid, item in by_id.items() if item.get("status") == "complete"}
    required_completed = set(launch.get("required_completed_slices") or [])
    missing = sorted(required_completed - completed)
    checks["completed_slices"] = sorted(completed)
    if missing:
        errors.append("missing required completed slices: " + ", ".join(missing))

    next_slice = next_actionable_slice(slices, completed)
    checks["next_actionable_slice"] = next_slice
    required_next = launch.get("required_next_slice")
    if required_next and next_slice != required_next:
        # Pinning a specific slice is optional; when unpinned, any next
        # actionable required slice authorizes launch (the per-slice gates
        # still apply downstream).
        errors.append(f"next actionable slice must be {required_next}: {next_slice}")
    if next_slice is None:
        errors.append("no actionable next slice")

    checks["active_run"] = state.get("active_run")
    checks["active_swr_run"] = state.get("active_swr_run")
    if launch.get("require_no_active_run") and state.get("active_run"):
        errors.append("active_run must be null")
    if launch.get("require_no_active_swr_run") and state.get("active_swr_run"):
        errors.append("active_swr_run must be null")

    command_results = [run_command(root, command) for command in launch.get("required_commands", [])]
    checks["commands"] = command_results
    for row in command_results:
        if row["exit_code"] != 0:
            errors.append(f"launch command failed: {row['command']}")

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify remaining-v1 autonomous launch authorization.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_launch(Path(args.root))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

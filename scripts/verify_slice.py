#!/usr/bin/env python3
"""Verify one AutoKeel slice against its acceptance contract.

This script is intended to run BEFORE a slice is marked complete. Use
--require-complete only for post-hoc auditing.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.acceptance_policy import command_allowed
from scripts.check_autonomous_review_exists import check_review
from scripts.check_no_tracked_data import check_no_tracked_data
from scripts.swr_lane_policy import validate_swr_lane_requirements
from scripts.validate_playbook_autonomous import validate_playbook


SECRET_RE = re.compile(
    r"(?i)(access_token|refresh_token|mood_token|x-mood-token|client_secret|password|authorization)"
    r"([\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]{8,})"
)

REQUIRED_ACCEPTANCE_COMMANDS = {
    "S03": {"python scripts/verify_s03_readiness.py --json"},
}


def redact_text(text: str) -> str:
    return SECRET_RE.sub(r"\1\2[REDACTED]", text)


def load_slices(root: Path) -> list[dict[str, Any]]:
    return json.loads((root / "ops" / "autonomy" / "slices.json").read_text(encoding="utf-8"))


def git_stdout(root: Path, *argv: str) -> tuple[bool, str, str]:
    proc = subprocess.run(
        ["git", *argv],
        cwd=str(root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip()


def run_command(root: Path, command: str, timeout: int) -> dict[str, Any]:
    if "mark-manual-gate" in command:
        return {
            "command": command,
            "exit_code": 99,
            "stdout_tail": "",
            "stderr_tail": "forbidden manual-gate command",
        }
    if not command_allowed(command, root):
        return {
            "command": command,
            "exit_code": 98,
            "stdout_tail": "",
            "stderr_tail": "acceptance command is not allowlisted",
        }

    try:
        proc = subprocess.run(
            shlex.split(command),
            cwd=str(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "command": command,
            "exit_code": proc.returncode,
            "stdout_tail": redact_text(proc.stdout[-2000:]),
            "stderr_tail": redact_text(proc.stderr[-2000:]),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "exit_code": 124,
            "stdout_tail": redact_text((exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else ""),
            "stderr_tail": f"timeout after {timeout}s",
        }


def verify_slice(
    root: Path,
    slice_id: str,
    dry_run: bool = False,
    require_complete: bool = False,
    timeout: int = 600,
) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    command_results: list[dict[str, Any]] = []

    target = next((item for item in load_slices(root) if item.get("id") == slice_id), None)
    if not target:
        return {"status": "error", "errors": [f"unknown slice: {slice_id}"], "warnings": [], "commands": []}

    if require_complete and target.get("status") != "complete":
        errors.append(f"slice {slice_id} is not marked complete")
    if require_complete and target.get("status") == "complete":
        ship_branch = target.get("ship_branch")
        ship_commit = target.get("ship_commit")
        if not ship_branch:
            errors.append(f"completed slice missing ship_branch")
        if not ship_commit:
            errors.append(f"completed slice missing ship_commit")
        if ship_branch and ship_commit:
            ok, branch_head, stderr = git_stdout(root, "rev-parse", "--verify", str(ship_branch))
            if not ok:
                errors.append(f"could not resolve ship_branch {ship_branch}: {stderr}")
            elif branch_head != str(ship_commit):
                errors.append(
                    f"ship_branch {ship_branch} points to {branch_head} "
                    f"but recorded ship_commit is {ship_commit}"
                )

    errors.extend(validate_swr_lane_requirements(root, target))

    playbook_rel = target.get("playbook")
    if playbook_rel:
        playbook = root / playbook_rel
        report = validate_playbook(playbook, risk=target.get("risk"))
        if report["status"] != "ok":
            errors.extend([f"playbook validation failed: {err}" for err in report["errors"]])
            warnings.extend([f"playbook warning: {warn}" for warn in report.get("warnings", [])])
    else:
        errors.append(f"slice {slice_id} has no playbook path")

    for rel in target.get("deliverables", []):
        if not (root / rel).exists():
            errors.append(f"missing deliverable: {rel}")

    required_acceptance = REQUIRED_ACCEPTANCE_COMMANDS.get(slice_id, set())
    configured_acceptance = set(str(command) for command in target.get("acceptance", []))
    for command in sorted(required_acceptance - configured_acceptance):
        errors.append(f"missing required acceptance command: {command}")

    tracked = check_no_tracked_data(root)
    errors.extend(tracked["errors"])
    warnings.extend(tracked.get("warnings", []))

    review = check_review(root, slice_id)
    errors.extend(review["errors"])

    for command in target.get("acceptance", []):
        if not command_allowed(command, root):
            errors.append(f"acceptance command is not allowlisted: {command}")
            command_results.append({"command": command, "exit_code": 98, "stderr_tail": "acceptance command is not allowlisted"})
            continue
        if dry_run:
            command_results.append({"command": command, "exit_code": 0, "dry_run": True})
            continue
        result = run_command(root, command, timeout=timeout)
        command_results.append(result)
        if result["exit_code"] != 0:
            errors.append(f"acceptance command failed ({result['exit_code']}): {command}")

    return {
        "status": "ok" if not errors else "error",
        "errors": errors,
        "warnings": warnings,
        "commands": command_results,
        "slice": slice_id,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify one AutoKeel slice.")
    parser.add_argument("slice_id")
    parser.add_argument("--root", default=".")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_slice(
        Path(args.root).resolve(),
        args.slice_id,
        dry_run=args.dry_run,
        require_complete=args.require_complete,
        timeout=args.timeout,
    )

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

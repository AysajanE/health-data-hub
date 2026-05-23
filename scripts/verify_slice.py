#!/usr/bin/env python3
"""Verify one AutoKeel slice against its acceptance contract."""

from __future__ import annotations

import argparse
import json
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_autonomous_review_exists import check_review
from scripts.check_no_tracked_data import check_no_tracked_data


def load_slices(root: Path) -> list[dict[str, Any]]:
    return json.loads((root / "ops" / "autonomy" / "slices.json").read_text(encoding="utf-8"))


def verify_slice(root: Path, slice_id: str, dry_run: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    command_results: list[dict[str, Any]] = []
    target = next((item for item in load_slices(root) if item.get("id") == slice_id), None)
    if not target:
        return {"status": "error", "errors": [f"unknown slice: {slice_id}"], "commands": []}

    if target.get("status") != "complete":
        errors.append(f"slice {slice_id} is not marked complete")

    tracked = check_no_tracked_data(root)
    errors.extend(tracked["errors"])
    review = check_review(root, slice_id)
    errors.extend(review["errors"])

    for command in target.get("acceptance", []):
        if "mark-manual-gate" in command:
            errors.append(f"forbidden command in acceptance: {command}")
            continue
        if dry_run:
            command_results.append({"command": command, "exit_code": 0, "dry_run": True})
            continue
        proc = subprocess.run(shlex.split(command), cwd=str(root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        command_results.append({"command": command, "exit_code": proc.returncode, "stdout_tail": proc.stdout[-2000:], "stderr_tail": proc.stderr[-2000:]})
        if proc.returncode != 0:
            errors.append(f"acceptance command failed ({proc.returncode}): {command}")

    return {"status": "ok" if not errors else "error", "errors": errors, "commands": command_results}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify one AutoKeel slice.")
    parser.add_argument("slice_id")
    parser.add_argument("--root", default=".")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = verify_slice(Path(args.root).resolve(), args.slice_id, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

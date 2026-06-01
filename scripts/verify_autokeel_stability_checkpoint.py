#!/usr/bin/env python3
"""Verify AutoKeel is stable enough to relaunch one bounded SWR repair tick."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from datetime import datetime
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
        "stdout_tail": proc.stdout[-2000:],
        "stderr_tail": proc.stderr[-2000:],
    }


def verify_checkpoint(root: Path, slice_id: str) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    slices = load_json(root / "ops/autonomy/slices.json", [])
    target = next((item for item in slices if isinstance(item, dict) and item.get("id") == slice_id), None)
    if not isinstance(target, dict):
        return {"status": "error", "errors": [f"unknown slice: {slice_id}"], "warnings": [], "checks": checks}

    checks["slice_status"] = target.get("status")
    checks["slice_reason"] = target.get("reason")
    if target.get("status") not in {"blocked", "blocked_compile_inputs"}:
        errors.append(f"{slice_id} must be blocked at checkpoint time: {target.get('status')}")

    repair = target.get("swr_review_repair")
    checks["swr_review_repair"] = repair
    if not isinstance(repair, dict) or repair.get("status") != "planned":
        errors.append(f"{slice_id} must retain a planned swr_review_repair")
    else:
        manifest_rel = str(repair.get("run_manifest") or "")
        if not manifest_rel or not (root / manifest_rel).exists():
            errors.append(f"{slice_id} planned repair manifest is missing: {manifest_rel}")
        if repair.get("repair_action") not in {"rerun_review_lane", "rerun_stage"}:
            errors.append(f"{slice_id} repair_action is unsupported: {repair.get('repair_action')}")

    state = load_json(root / "ops/autonomy/autonomy_state.json", {})
    checks["active_run"] = state.get("active_run")
    checks["active_swr_run"] = state.get("active_swr_run")
    if state.get("active_run"):
        errors.append("active_run must be null before bounded repair relaunch")

    open_rows = [
        row for row in iter_jsonl(root / "ops/autonomy/failure_ledger.jsonl")
        if row.get("slice") == slice_id and row.get("open", True)
    ]
    checks["open_failures"] = [
        {"failure_class": row.get("failure_class"), "severity": row.get("severity"), "description": row.get("description")}
        for row in open_rows
    ]
    non_budget_open = [row for row in open_rows if row.get("failure_class") != "failure_budget_exceeded"]
    if non_budget_open:
        errors.append("non-budget open failures remain: " + ", ".join(str(row.get("failure_class")) for row in non_budget_open))

    commands = [
        "git diff --check",
        "python scripts/check_no_tracked_data.py --json",
        "python -m pytest tests/autonomy -q",
        "python -m pytest tests/model -q",
    ]
    command_results = [run(root, command) for command in commands]
    checks["commands"] = command_results
    for row in command_results:
        if row["exit_code"] != 0:
            errors.append(f"checkpoint command failed: {row['command']}")

    return {
        "status": "ok" if not errors else "error",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "slice": slice_id,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify S05/SWR stability before one bounded relaunch.")
    parser.add_argument("slice_id")
    parser.add_argument("--root", default=".")
    parser.add_argument("--write-evidence", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_checkpoint(Path(args.root), args.slice_id.upper())
    if args.write_evidence:
        out = Path(args.root).resolve() / "docs/evidence" / f"{args.slice_id.lower()}-autokeel-stability-checkpoint.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        tmp = out.with_suffix(out.suffix + ".tmp")
        tmp.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp, out)
        report["evidence_path"] = str(out.relative_to(Path(args.root).resolve()))

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

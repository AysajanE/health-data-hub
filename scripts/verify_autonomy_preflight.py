#!/usr/bin/env python3
"""Preflight checks for running AutoKeel on Health Data Hub."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_no_tracked_data import check_no_tracked_data


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def preflight(root: Path, keel_root: Path, strict_tools: bool = False, run_keel_smoke: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}
    checks["product_repo_exists"] = root.exists()
    if not root.exists():
        errors.append(f"product repo missing: {root}")
    checks["keel_root_exists"] = keel_root.exists()
    if not keel_root.exists():
        errors.append(f"keel root missing: {keel_root}")

    for wrapper in ("keel-smoke", "keel-compile", "keel-run", "keel-doctor", "keel-swr"):
        exists = (keel_root / "bin" / wrapper).exists()
        checks[f"{wrapper}_exists"] = exists
        if not exists:
            errors.append(f"missing Keel wrapper: {keel_root / 'bin' / wrapper}")

    for command in ("git", "python"):
        if not command_exists(command):
            errors.append(f"missing required command: {command}")
    for command in ("claude", "codex", "gh", "jq"):
        if not command_exists(command):
            (errors if strict_tools else warnings).append(f"optional tool missing: {command}")

    for rel in ("ops/autonomy/policy.yaml", "ops/autonomy/slices.json", "ops/autonomy/autonomy_state.json", "ops/autonomy/events.jsonl", "ops/autonomy/failure_ledger.jsonl"):
        if not (root / rel).exists():
            errors.append(f"missing autonomy file: {rel}")

    tracked = check_no_tracked_data(root)
    errors.extend(tracked["errors"])
    warnings.extend(tracked.get("warnings", []))

    if run_keel_smoke and (keel_root / "bin" / "keel-smoke").exists():
        proc = subprocess.run([str(keel_root / "bin" / "keel-smoke")], cwd=str(keel_root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        checks["keel_smoke_exit_code"] = proc.returncode
        if proc.returncode != 0:
            errors.append("keel-smoke failed")

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run AutoKeel preflight checks.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--keel-root", default="/Users/aeziz-local/keel")
    parser.add_argument("--strict-tools", action="store_true")
    parser.add_argument("--run-keel-smoke", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = preflight(Path(args.root).resolve(), Path(args.keel_root).resolve(), strict_tools=args.strict_tools, run_keel_smoke=args.run_keel_smoke)
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

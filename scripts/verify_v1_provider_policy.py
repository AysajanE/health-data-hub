#!/usr/bin/env python3
"""Verify final v1 provider-policy invariants."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.verify_s05_provider_policy import scan_model_training_code, warehouse_provider_violations
from src.warehouse.features import load_sleep_provider_policy


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def eight_sleep_required_for_v1(root: Path) -> tuple[bool, list[str]]:
    slices = load_json(root / "ops/autonomy/slices.json", [])
    offending_commands: list[str] = []
    if not isinstance(slices, list):
        return False, offending_commands
    for item in slices:
        if not isinstance(item, dict) or item.get("id") not in {"S04", "S05", "S06", "S07", "S08", "S09"}:
            continue
        for command in item.get("acceptance") or []:
            command_text = str(command).lower()
            if "pyeight" in command_text or "eight_sleep" in command_text:
                offending_commands.append(f"{item.get('id')}: {command}")
    return bool(offending_commands), offending_commands


def verify_v1_provider_policy(root: Path) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    try:
        policy = load_sleep_provider_policy(root)
    except ValueError as error:
        errors.append(str(error))
        policy = None

    if policy is not None:
        checks["active_sleep_provider"] = policy.active_sleep_source
        checks["eight_sleep_state"] = policy.eight_sleep_state
        if policy.active_sleep_source != "oura":
            errors.append(f"active_sleep_provider must be oura: {policy.active_sleep_source}")
        if policy.eight_sleep_state != "fallback_active":
            errors.append(f"eight_sleep_state must be fallback_active: {policy.eight_sleep_state}")
        if policy.eight_sleep_allowed_for_features:
            errors.append("eight_sleep_allowed_for_features must be false for v1")

    model_violations = scan_model_training_code(root)
    warehouse_violations, warehouse_checks = warehouse_provider_violations(root)
    checks.update(warehouse_checks)
    checks["model_training_forbidden_8sleep_references"] = model_violations
    eight_sleep_used = bool(model_violations or warehouse_violations)
    checks["eight_sleep_used_in_model_features"] = eight_sleep_used
    if eight_sleep_used:
        errors.extend(f"8 Sleep appears in model feature path: {path}" for path in model_violations)
        errors.extend(warehouse_violations)

    required, offending_commands = eight_sleep_required_for_v1(root)
    checks["eight_sleep_required_for_v1"] = required
    checks["eight_sleep_required_commands"] = offending_commands
    if required:
        errors.append("v1 acceptance commands must not require pyEight or 8 Sleep")

    if not any((root / rel_dir).exists() for rel_dir in ("src/model", "src/models", "src/ml")):
        warnings.append("model source directory is not present yet; source scan skipped")

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify v1 provider-policy invariants.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_v1_provider_policy(Path(args.root).resolve())
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

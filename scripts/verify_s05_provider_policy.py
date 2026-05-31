#!/usr/bin/env python3
"""Verify S05 cannot train on inactive 8 Sleep fallback data."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.warehouse.features import load_sleep_provider_policy


MODEL_DIRS = ("src/model", "src/models", "src/ml")
FORBIDDEN_MODEL_PATTERNS = (
    re.compile(r"""source\s*={1,2}\s*['"]8sleep['"]""", re.I),
    re.compile(r"""hrv_merge_method\s*={1,2}\s*['"]eight_fallback['"]""", re.I),
    re.compile(r"""eight_fallback""", re.I),
)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def scan_model_training_code(root: Path) -> list[str]:
    violations: list[str] = []
    for rel_dir in MODEL_DIRS:
        directory = root / rel_dir
        if not directory.exists():
            continue
        for path in sorted(directory.rglob("*.py")):
            text = path.read_text(encoding="utf-8", errors="replace")
            if any(pattern.search(text) for pattern in FORBIDDEN_MODEL_PATTERNS):
                violations.append(str(path.relative_to(root)))
    return violations


def warehouse_provider_violations(root: Path) -> tuple[list[str], dict[str, Any]]:
    checks: dict[str, Any] = {"warehouse_database": None}
    database = root / "data/warehouse.duckdb"
    if not database.exists():
        return [], checks

    import duckdb

    checks["warehouse_database"] = str(database.relative_to(root))
    violations: list[str] = []
    conn = duckdb.connect(str(database), read_only=True)
    try:
        blended_count = conn.execute(
            "SELECT count(*) FROM daily_features WHERE sleep_source_count > 1"
        ).fetchone()[0]
        eight_hrv_count = conn.execute(
            "SELECT count(*) FROM sleep_merge_diagnostics WHERE hrv_merge_method = 'eight_fallback'"
        ).fetchone()[0]
    finally:
        conn.close()

    checks["daily_features_source_count_gt_1"] = int(blended_count)
    checks["diagnostics_eight_fallback_hrv"] = int(eight_hrv_count)
    if blended_count:
        violations.append("daily_features contains rows with sleep_source_count > 1")
    if eight_hrv_count:
        violations.append("sleep_merge_diagnostics contains hrv_merge_method = eight_fallback")
    return violations, checks


def verify_s05_provider_policy(root: Path) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    slices = load_json(root / "ops/autonomy/slices.json", [])
    by_id = {item.get("id"): item for item in slices if isinstance(item, dict)} if isinstance(slices, list) else {}
    for slice_id in ("S03", "S04"):
        status = by_id.get(slice_id, {}).get("status")
        checks[f"{slice_id.lower()}_status"] = status
        if status != "complete":
            errors.append(f"{slice_id} must be complete before S05 provider-policy preflight: {status}")

    try:
        policy = load_sleep_provider_policy(root)
    except ValueError as error:
        errors.append(str(error))
        policy = None

    if policy is not None:
        checks["active_sleep_provider"] = policy.active_sleep_source
        checks["eight_sleep_state"] = policy.eight_sleep_state
        checks["eight_sleep_allowed_for_features"] = policy.eight_sleep_allowed_for_features
        if policy.active_sleep_source != "oura":
            errors.append(f"active sleep provider must be oura for v1: {policy.active_sleep_source}")
        if policy.eight_sleep_state != "fallback_active":
            errors.append(f"8 Sleep state must be fallback_active for v1: {policy.eight_sleep_state}")
        if policy.eight_sleep_allowed_for_features:
            errors.append("8 Sleep is not allowed for S05 model features under Oura-only v1")

    model_violations = scan_model_training_code(root)
    checks["model_training_forbidden_8sleep_references"] = model_violations
    errors.extend(f"model training may not depend on inactive 8 Sleep fallback data: {path}" for path in model_violations)
    if not any((root / rel_dir).exists() for rel_dir in MODEL_DIRS):
        warnings.append("model source directory is not present yet; source scan skipped")

    warehouse_violations, warehouse_checks = warehouse_provider_violations(root)
    checks.update(warehouse_checks)
    errors.extend(warehouse_violations)

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify S05 provider-policy preflight.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_s05_provider_policy(Path(args.root).resolve())
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

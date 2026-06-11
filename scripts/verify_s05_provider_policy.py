#!/usr/bin/env python3
"""Verify S05 cannot train on inactive 8 Sleep fallback data."""

from __future__ import annotations

import argparse
from collections.abc import Iterable, Mapping
import json
import re
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.warehouse.features import load_sleep_provider_policy


MODEL_DIRS = ("src/model", "src/models", "src/ml")
EIGHT_SLEEP_SOURCES = {"8sleep", "eight_sleep", "pyeight"}
FORBIDDEN_MODEL_PATTERNS = (
    re.compile(r"""source\s*={1,2}\s*['"]8sleep['"]""", re.I),
    re.compile(r"""hrv_merge_method\s*={1,2}\s*['"]eight_fallback['"]""", re.I),
    re.compile(r"""eight_fallback""", re.I),
)
REQUIRED_TRAINING_INPUT_FIELDS = ("source", "sleep_source_count", "hrv_merge_method")


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


def _row_value(row: Any, field: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(field)
    return getattr(row, field, None)


def _normalized_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def collect_model_training_input_violations(rows: Iterable[Any]) -> list[str]:
    violations: list[str] = []
    for index, row in enumerate(rows):
        missing_fields = [field for field in REQUIRED_TRAINING_INPUT_FIELDS if _row_value(row, field) is None]
        if missing_fields:
            violations.append(
                f"training input row {index} is missing required fields: {', '.join(missing_fields)}"
            )
            continue

        source = _normalized_text(_row_value(row, "source"))
        sleep_source_count = _row_value(row, "sleep_source_count")
        hrv_merge_method = _normalized_text(_row_value(row, "hrv_merge_method"))

        if source in EIGHT_SLEEP_SOURCES:
            violations.append(f"training input row {index} uses source=8sleep under Oura-only v1")
        if not isinstance(sleep_source_count, int):
            violations.append(
                f"training input row {index} has non-integer sleep_source_count: {sleep_source_count!r}"
            )
        elif sleep_source_count > 1:
            violations.append(f"training input row {index} has sleep_source_count > 1: {sleep_source_count}")
        if hrv_merge_method == "eight_fallback":
            violations.append(
                f"training input row {index} uses hrv_merge_method=eight_fallback under Oura-only v1"
            )
    return violations


def training_input_policy_contract_report() -> dict[str, Any]:
    allowed_rows = [
        {
            "feature_date": "2026-06-01",
            "source": "oura",
            "sleep_source_count": 1,
            "hrv_merge_method": "oura_primary",
        }
    ]
    rejected_rows = [
        {
            "feature_date": "2026-06-02",
            "source": "8sleep",
            "sleep_source_count": 1,
            "hrv_merge_method": "missing",
        },
        {
            "feature_date": "2026-06-03",
            "source": "oura",
            "sleep_source_count": 2,
            "hrv_merge_method": "oura_primary",
        },
        {
            "feature_date": "2026-06-04",
            "source": "oura",
            "sleep_source_count": 1,
            "hrv_merge_method": "eight_fallback",
        },
    ]
    expected_rejections = [
        "training input row 0 uses source=8sleep under Oura-only v1",
        "training input row 1 has sleep_source_count > 1: 2",
        "training input row 2 uses hrv_merge_method=eight_fallback under Oura-only v1",
    ]
    allowed_violations = collect_model_training_input_violations(allowed_rows)
    rejected_violations = collect_model_training_input_violations(rejected_rows)

    errors: list[str] = []
    if allowed_violations:
        errors.append(
            "training input policy contract rejected an allowed Oura-only fixture row"
        )
    if rejected_violations != expected_rejections:
        errors.append("training input policy contract did not reject the expected fallback breakers")

    return {
        "status": "ok" if not errors else "error",
        "errors": errors,
        "checks": {
            "allowed_fixture_violations": allowed_violations,
            "rejected_fixture_violations": rejected_violations,
        },
    }


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
        try:
            blended_count = conn.execute(
                "SELECT count(*) FROM daily_features WHERE sleep_source_count > 1"
            ).fetchone()[0]
            eight_hrv_count = conn.execute(
                "SELECT count(*) FROM sleep_merge_diagnostics WHERE hrv_merge_method = 'eight_fallback'"
            ).fetchone()[0]
        except duckdb.Error as error:
            checks["warehouse_policy_query_error"] = str(error)
            return [f"warehouse provider-policy evidence query failed: {error}"], checks
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

    contract_report = training_input_policy_contract_report()
    checks["training_input_policy_contract"] = contract_report["checks"]
    errors.extend(contract_report["errors"])

    warehouse_violations, warehouse_checks = warehouse_provider_violations(root)
    checks.update(warehouse_checks)
    errors.extend(warehouse_violations)

    entrypoint_errors, entrypoint_checks = retrain_entrypoint_violations(root)
    checks.update(entrypoint_checks)
    errors.extend(entrypoint_errors)

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "checks": checks}


def retrain_entrypoint_violations(root: Path) -> tuple[list[str], dict[str, Any]]:
    """Inspect the shipped retrain entrypoint for verified-loading discipline.

    The nightly entrypoint must source training rows exclusively through the
    verified S04 model-ready loader: no CLI flag may inject alternate feature
    rows, and the verified loader must be the default data path.
    """
    errors: list[str] = []
    checks: dict[str, Any] = {}
    entrypoint = root / "scripts" / "retrain_model.py"
    checks["retrain_entrypoint"] = "scripts/retrain_model.py"
    if not entrypoint.exists():
        checks["retrain_entrypoint_present"] = False
        return errors, checks
    checks["retrain_entrypoint_present"] = True
    source = entrypoint.read_text(encoding="utf-8")
    banned_flags = ("--feature-rows-json", "--rows-json", "--input-rows", "--feature-rows")
    exposed = [flag for flag in banned_flags if flag in source]
    checks["retrain_entrypoint_bypass_flags"] = exposed
    if exposed:
        errors.append(
            "retrain entrypoint exposes alternate feature-row input paths that bypass "
            "verified S04 row loading: " + ", ".join(exposed)
        )
    if "load_verified_feature_rows" not in source:
        errors.append("retrain entrypoint does not use the verified S04 model-ready row loader")
    checks["retrain_entrypoint_uses_verified_loader"] = "load_verified_feature_rows" in source
    return errors, checks


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

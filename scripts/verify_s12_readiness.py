#!/usr/bin/env python3
"""Offline technical readiness check for the personal Oura production sync.

This command performs no network call, reads no credential, and opens no
private evidence. It checks only committed build inputs, dependency order, the
Oura-only product contract, and the two-boundary activation configuration.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


BRIEF_REL = "docs/briefs/s12-oura-production-sync.autonomous-brief.md"
AUTOPLAN_REL = "docs/gstack/s12-oura-production-sync-autoplan.md"
VERIFIER_REL = "scripts/verify_s12_readiness.py"
TEST_REL = "tests/autonomy/test_verify_s12_readiness.py"
SLICES_REL = "ops/autonomy/slices.json"
POLICY_REL = "ops/autonomy/policy.yaml"

REQUIRED_DEPENDENCIES = ("S03", "S04", "S05", "S11")
REQUIRED_INPUTS = (BRIEF_REL, AUTOPLAN_REL, VERIFIER_REL, TEST_REL, SLICES_REL, POLICY_REL)
REQUIRED_DELIVERABLES = {
    "src/ingestion/oura_auth.py",
    "src/ingestion/oura_sync.py",
    "src/warehouse/locking.py",
    "scripts/sync_oura.py",
    "scripts/evidence/oura_sync_attestation.py",
    "scripts/verify_s12_sync.py",
    "tests/ingestion/test_oura_retention.py",
    "docs/reviews/s12-autonomous-security-privacy-review.md",
    "docs/reviews/s12-autonomous-ingestion-integrity-review.md",
    "docs/evidence/s12-oura-production-sync-command-evidence.json",
}
ACTIVATION_ACCEPTANCE = {
    "command": "python scripts/verify_s12_sync.py --runtime-root-env HEALTH_HUB_RUNTIME_ROOT --json",
    "runtime_root": "canonical",
    "runtime_root_env": "HEALTH_HUB_RUNTIME_ROOT",
    "reads_private_aggregate_evidence": True,
    "live_warehouse_access": "read_only",
    "required_after_ship_acceptance": True,
    "completion_requires_status": "ok",
}
READINESS_COMMAND = "python scripts/verify_s12_readiness.py --json"

REQUIRED_CONTRACT_TERMS = (
    "private, single-user personal",
    "oura remains the only active v1 sleep source",
    "8 sleep remains inactive",
    "waketime_utc",
    "home_timezone",
    "zoneinfo",
    "fcntl.flock",
    "checkpoint",
    "chronological",
    "aggregate",
    "raw provider responses are never written to disk",
    "autonomous_gate_review",
)
def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else default


def run_git(root: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *argv],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def committed_input_state(root: Path, rel: str) -> dict[str, Any]:
    tracked = run_git(root, "ls-files", "--error-unmatch", "--", rel)
    head = run_git(root, "show", f"HEAD:{rel}")
    path = root / rel
    exists = path.is_file() and not path.is_symlink()
    matches_head = bool(exists and tracked.returncode == 0 and head.returncode == 0 and path.read_bytes() == head.stdout.encode())
    return {
        "tracked": tracked.returncode == 0,
        "regular_file": exists,
        "present_at_head": head.returncode == 0,
        "matches_head": matches_head,
    }


def validate_contract_document(path: Path) -> list[str]:
    if not path.is_file() or path.is_symlink():
        return [f"missing S12 contract document: {path.name}"]
    text = " ".join(path.read_text(encoding="utf-8").lower().split())
    return [
        f"{path.name} missing technical contract term: {term}"
        for term in REQUIRED_CONTRACT_TERMS
        if term not in text
    ]


def validate_autoplan_write_scope(path: Path) -> list[str]:
    errors: list[str] = []
    if not path.is_file():
        return errors
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().startswith("Files:"):
            continue
        for protected in (VERIFIER_REL, TEST_REL):
            if protected in line:
                errors.append(f"S12 readiness control appears in generated Files scope: {protected}")
    return errors


def validate_slice_contract(slices: Any) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    checks: dict[str, Any] = {}
    if not isinstance(slices, list):
        return ["slices.json must be an array"], checks
    matches = [item for item in slices if isinstance(item, Mapping) and item.get("id") == "S12"]
    if len(matches) != 1:
        return ["slices.json must contain exactly one S12 row"], checks
    s12 = dict(matches[0])
    checks["slice_status"] = s12.get("status")
    checks["lane"] = s12.get("lane")
    checks["dependencies"] = list(s12.get("depends_on") or [])

    if s12.get("required") is not True:
        errors.append("S12 must remain required")
    if s12.get("lane") != "compiler":
        errors.append("S12 lane must be compiler")
    if tuple(s12.get("depends_on") or []) != REQUIRED_DEPENDENCIES:
        errors.append("S12 dependencies must be exactly S03, S04, S05, S11")
    if s12.get("activation_acceptance") != ACTIVATION_ACCEPTANCE:
        errors.append("S12 activation_acceptance must match the canonical read-only sync-proof contract")
    if s12.get("pre_po_commands") != [READINESS_COMMAND]:
        errors.append("S12 pre_po_commands must contain the exact technical readiness command")
    if s12.get("pre_ship_commands") != [READINESS_COMMAND]:
        errors.append("S12 pre_ship_commands must contain the exact technical readiness command")
    deliverables = set(s12.get("deliverables") or [])
    if deliverables != REQUIRED_DELIVERABLES:
        errors.append("S12 deliverables must match the frozen production-sync set")
    if set(s12.get("review_artifacts") or []) != {
        "docs/reviews/s12-autonomous-security-privacy-review.md",
        "docs/reviews/s12-autonomous-ingestion-integrity-review.md",
    }:
        errors.append("S12 must require exactly the security/privacy and ingestion-integrity reviews")
    if s12.get("evidence_dirs") != ["private/evidence/S12/oura_sync"]:
        errors.append("S12 evidence must use only the private aggregate sync root")
    for removed_key in ("failure_path", "reason"):
        if removed_key in s12:
            errors.append(f"S12 must not retain obsolete blocker field: {removed_key}")
    return errors, checks


def verify_s12_readiness(root: Path) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {
        "phase": "pre_compiler_or_pre_ship",
        "network_accessed": False,
        "credentials_inspected": False,
        "private_evidence_read": False,
        "provider_policy": "oura_only_v1_not_reopened",
    }

    committed_inputs = {rel: committed_input_state(root, rel) for rel in REQUIRED_INPUTS}
    checks["committed_inputs"] = committed_inputs
    for rel, state in committed_inputs.items():
        if not state["matches_head"]:
            errors.append(f"S12 input must be tracked and byte-identical to HEAD: {rel}")

    slices = load_json(root / SLICES_REL, [])
    slice_errors, slice_checks = validate_slice_contract(slices)
    errors.extend(slice_errors)
    checks.update(slice_checks)

    by_id = {item.get("id"): item for item in slices if isinstance(item, Mapping)} if isinstance(slices, list) else {}
    dependency_status = {dep: (by_id.get(dep) or {}).get("status") for dep in REQUIRED_DEPENDENCIES}
    checks["dependency_status"] = dependency_status
    for dep, status in dependency_status.items():
        if status != "complete":
            errors.append(f"{dep} must be complete before S12 launch: {status}")

    for rel in (BRIEF_REL, AUTOPLAN_REL):
        errors.extend(validate_contract_document(root / rel))
    errors.extend(validate_autoplan_write_scope(root / AUTOPLAN_REL))

    return {
        "status": "ok" if not errors else "error",
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
    }


def run_s12_sync_proof(root: Path) -> dict[str, Any]:
    """Fail closed until the trusted outer aggregate-receipt validator exists."""

    del root
    return {
        "status": "blocked_external",
        "errors": ["trusted current S12 sync-receipt validation is not implemented; generated sync code was not executed"],
        "checks": {
            "network_accessed": False,
            "credentials_inspected": False,
            "private_health_rows_read": False,
            "generated_verifier_executed": False,
        },
    }


def verify_s12_continuity(root: Path) -> dict[str, Any]:
    """Require current technical readiness and current aggregate sync proof."""

    readiness = verify_s12_readiness(root)
    if readiness.get("status") != "ok":
        return {
            "status": "error",
            "errors": [f"S12 current readiness: {error}" for error in readiness.get("errors", [])],
            "checks": {
                "readiness_status": readiness.get("status"),
                "sync_proof_status": "not_run_readiness_failed",
            },
        }
    sync_proof = run_s12_sync_proof(root)
    return {
        "status": sync_proof.get("status", "error"),
        "errors": [f"S12 current sync proof: {error}" for error in sync_proof.get("errors", [])],
        "checks": {
            "readiness_status": "ok",
            "sync_proof_status": sync_proof.get("status"),
            "sync_proof": sync_proof.get("checks", {}),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify offline S12 technical readiness.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = verify_s12_readiness(Path(args.root))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

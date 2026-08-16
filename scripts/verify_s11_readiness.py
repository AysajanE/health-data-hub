#!/usr/bin/env python3
"""Fail-closed, zero-spend readiness gate for the S11 compiler lane.

This verifier inspects only committed control inputs, public contract text, and
filesystem metadata.  It deliberately does not load environment files, open a
DuckDB database, enumerate evidence records, or parse any private payload.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

import yaml

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

ROOT = Path(__file__).resolve().parents[1]
ACTIONABLE_STATUSES = {"pending", "waiting_for_playbook", "replan_required", "evidence_ready"}
S11_BRIEF = "docs/briefs/s11-mood-logging-recovery.autonomous-brief.md"
S11_AUTOPLAN = "docs/gstack/s11-mood-logging-recovery-autoplan.md"

REQUIRED_COMMITTED_INPUTS = (
    "docs/gstack/health-data-hub-office-hours.md",
    S11_BRIEF,
    S11_AUTOPLAN,
    "ops/autonomy/autokeel.py",
    "ops/autonomy/policy.yaml",
    "ops/autonomy/slices.json",
    "ops/autonomy/schemas/tripwire_baseline_gate.schema.json",
    "ops/autonomy/schemas/tripwire_mood_compliance.schema.json",
    "ops/autonomy/schemas/tripwire_mood_transport.schema.json",
    "scripts/setup_permissions.py",
    "scripts/tripwire_evidence.py",
    "scripts/verify_s11_readiness.py",
)

REQUIRED_DELIVERABLES = {
    "app/mood_form.py",
    "src/model/display_gate.py",
    "src/warehouse/locking.py",
    "scripts/setup_permissions.py",
    "scripts/evidence/baseline_gate_report.py",
    "scripts/evidence/mood_transport_report.py",
    "scripts/evidence/mood_compliance_report.py",
    "scripts/verify_mood_logging_recovery.py",
}

ACTIVATION_ACCEPTANCE = {
    "command": "python scripts/verify_mood_logging_recovery.py --runtime-root-env HEALTH_HUB_RUNTIME_ROOT --json",
    "runtime_root": "canonical",
    "runtime_root_env": "HEALTH_HUB_RUNTIME_ROOT",
    "reads_private_aggregate_evidence": True,
    "live_warehouse_access": "read_only",
    "required_after_ship_acceptance": True,
    "completion_requires_status": "ok",
}

TRIPWIRE_BRIDGE = {
    "on_mood_transport_failure_week_4": ("mood_transport_v1", "private/evidence/S11/mood_transport"),
    "on_mood_compliance_failure_week_8": ("mood_compliance_v1", "private/evidence/S11/mood_compliance"),
    "on_baseline_gate_failure_week_9": ("baseline_gate_v1", "private/evidence/S11/baseline_gate"),
}

CONTRACT_MARKERS = (
    "runtime/evidence bridge",
    "two-boundary",
    "hermetic ship acceptance",
    "activation_acceptance",
    "canonical runtime root",
    "health_hub_runtime_root",
    "ship code",
    "aggregate/private runtime evidence",
    "read-only",
    "must not complete",
)

# Neither the compiler nor PO currently has an enforceable read sandbox, and
# the post-ship activation result does not yet have a trusted outer receipt
# validator.  Keep these controls explicit so this precompiler gate cannot
# silently treat a self-reported activation payload as launch authority.
SENSITIVE_COMPILER_PATHS = (
    ".env",
    ".env.local",
    "data/secrets",
    "private",
)
FIXED_REQUIRED_RUNTIME_DIRECTORIES = (
    "data",
    "private",
    "private/evidence",
    "models",
)
FIXED_OPTIONAL_SENSITIVE_NODES = (
    ".env",
    ".env.local",
    "data/secrets",
)


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _git_output(root: Path, *argv: str) -> tuple[bool, str]:
    proc = subprocess.run(
        ["git", *argv],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode == 0, (proc.stdout if proc.returncode == 0 else proc.stderr).strip()


def _committed_input_state(root: Path, relative_path: str) -> dict[str, Any]:
    rel = Path(relative_path)
    path = root / rel
    state: dict[str, Any] = {
        "tracked_at_head": False,
        "worktree_matches_head": False,
        "regular_file": False,
        "symlink": False,
    }
    if rel.is_absolute() or ".." in rel.parts:
        state["error"] = "unsafe repository-relative path"
        return state
    state["symlink"] = path.is_symlink()
    if state["symlink"] or not path.is_file():
        state["error"] = "missing, non-regular, or symlink input"
        return state
    state["regular_file"] = True
    head_ok, head_blob = _git_output(root, "rev-parse", f"HEAD:{relative_path}")
    state["tracked_at_head"] = head_ok
    if not head_ok:
        state["error"] = "not tracked at HEAD"
        return state
    worktree_ok, worktree_blob = _git_output(root, "hash-object", "--", relative_path)
    state["worktree_matches_head"] = worktree_ok and worktree_blob == head_blob
    if not state["worktree_matches_head"]:
        state["error"] = "worktree bytes differ from HEAD"
    return state


def _validate_autoplan(text: str) -> list[str]:
    lowered = text.lower()
    errors: list[str] = []
    wrapper_markers = (
        "write permission was denied",
        "write wasn't approved",
        "write was not approved",
        "here is the autoplan",
        "i attempted to save",
        "approve the write",
        "let me know if",
    )
    if any(marker in lowered for marker in wrapper_markers) or "```" in text:
        errors.append("S11 autoplan contains assistant wrapper/refusal text")
    for marker, error in (
        ("s11", "S11 autoplan is missing slice id S11"),
        ("deliverable", "S11 autoplan is missing deliverables section or term"),
        ("verification", "S11 autoplan is missing verification expectations"),
        ("implementation tasks", "S11 autoplan is missing Implementation Tasks section"),
        ("autonomous_gate_review", "S11 high-risk autoplan is missing autonomous_gate_review"),
    ):
        if marker not in lowered:
            errors.append(error)
    if "manual gate" not in lowered and "manual_gate" not in lowered:
        errors.append("S11 autoplan is missing explicit no manual gate policy")
    if not (re.search(r"(?im)^\s*files\s*:", text) or re.search(r"(?im)^\s*\|.*\bfiles\b.*\|", text)):
        errors.append("S11 autoplan is missing compiler-parseable Files fields")
    if not (re.search(r"(?im)^\s*verify\s*:", text) or re.search(r"(?im)^\s*\|.*\bverify\b.*\|", text)):
        errors.append("S11 autoplan is missing compiler-parseable Verify fields")
    return errors


def _validate_contract_text(brief_text: str, autoplan_text: str) -> list[str]:
    combined = f"{brief_text}\n{autoplan_text}".lower()
    errors = [
        f"S11 inputs are missing required two-boundary contract marker: {marker}"
        for marker in CONTRACT_MARKERS
        if marker not in combined
    ]
    if ACTIVATION_ACCEPTANCE["command"].lower() not in combined:
        errors.append("S11 inputs do not bind the exact activation_acceptance command")
    return errors


def _validate_tripwire_bridge(policy: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    checks: dict[str, Any] = {}
    deadlines = policy.get("tripwire_deadlines") if isinstance(policy, dict) else None
    if not isinstance(deadlines, dict):
        return ["policy tripwire_deadlines mapping is missing"], checks
    for name, (evidence_kind, evidence_path) in TRIPWIRE_BRIDGE.items():
        row = deadlines.get(name)
        valid = (
            isinstance(row, dict)
            and row.get("recovery_slice") == "S11"
            and row.get("evidence_kind") == evidence_kind
            and row.get("evidence") == evidence_path
        )
        checks[name] = valid
        if not valid:
            errors.append(
                f"policy runtime/evidence bridge is invalid for {name}: expected S11/{evidence_kind}/{evidence_path}"
            )
    return errors, checks


def _lstat_fixed_path(root: Path, relative_path: str) -> tuple[str, os.stat_result | None, str, str | None]:
    """lstat a fixed path one component at a time, stopping at symlinks."""

    current = root
    for part in Path(relative_path).parts:
        current = current / part
        stopped_at = current.relative_to(root).as_posix()
        try:
            path_stat = current.lstat()
        except FileNotFoundError:
            return "absent", None, stopped_at, None
        except OSError as exc:
            return "error", None, stopped_at, type(exc).__name__
        if stat.S_ISLNK(path_stat.st_mode):
            return "symlink", path_stat, stopped_at, None
    return "ok", path_stat, relative_path, None


def _inspect_fixed_permission_contract(root: Path) -> dict[str, Any]:
    """Check modes for named nodes only; never enumerate runtime descendants."""

    errors: list[str] = []
    path_checks: dict[str, Any] = {}
    for relative_path in FIXED_REQUIRED_RUNTIME_DIRECTORIES:
        status, path_stat, stopped_at, error_kind = _lstat_fixed_path(root, relative_path)
        check: dict[str, Any] = {"status": status, "expected_mode": "0o700"}
        if stopped_at != relative_path:
            check["stopped_at"] = stopped_at
        if error_kind:
            check["metadata_error"] = error_kind
        path_checks[relative_path] = check
        if status == "absent":
            errors.append(f"managed runtime directory is missing: {relative_path}")
        elif status == "symlink":
            errors.append(f"managed runtime path has a symlink boundary at: {stopped_at}")
        elif status == "error" or path_stat is None:
            errors.append(f"cannot inspect managed runtime path metadata: {relative_path}")
        elif not stat.S_ISDIR(path_stat.st_mode):
            errors.append(f"expected directory but found non-directory: {relative_path}")
        else:
            actual_mode = stat.S_IMODE(path_stat.st_mode)
            check["actual_mode"] = oct(actual_mode)
            if actual_mode != 0o700:
                errors.append(
                    f"unsafe directory mode for {relative_path}: {oct(actual_mode)} (expected 0o700)"
                )

    for relative_path in FIXED_OPTIONAL_SENSITIVE_NODES:
        status, path_stat, stopped_at, error_kind = _lstat_fixed_path(root, relative_path)
        check = {"status": status, "required": False}
        if stopped_at != relative_path:
            check["stopped_at"] = stopped_at
        if error_kind:
            check["metadata_error"] = error_kind
        path_checks[relative_path] = check
        if status == "absent":
            continue
        if status == "symlink":
            errors.append(f"sensitive runtime path has a symlink boundary at: {stopped_at}")
            continue
        if status == "error" or path_stat is None:
            errors.append(f"cannot inspect sensitive runtime path metadata: {relative_path}")
            continue
        is_directory = stat.S_ISDIR(path_stat.st_mode)
        if relative_path in {".env", ".env.local"} and is_directory:
            errors.append(f"expected sensitive file but found directory: {relative_path}")
            continue
        expected_mode = 0o700 if is_directory else 0o600
        actual_mode = stat.S_IMODE(path_stat.st_mode)
        check.update(
            {
                "kind": "directory" if is_directory else "file",
                "expected_mode": oct(expected_mode),
                "actual_mode": oct(actual_mode),
            }
        )
        if actual_mode != expected_mode:
            errors.append(
                f"unsafe {'directory' if is_directory else 'file'} mode for {relative_path}: "
                f"{oct(actual_mode)} (expected {oct(expected_mode)})"
            )

    return {
        "status": "ok" if not errors else "error",
        "errors": errors,
        "checks": {
            "paths": path_checks,
            "file_contents_read": False,
            "directories_enumerated": False,
            "symlinks_followed": False,
        },
    }


def _inspect_sensitive_path_isolation(root: Path) -> dict[str, Any]:
    """Inspect exact sensitive-path metadata without opening or enumerating it."""

    path_checks: dict[str, Any] = {}
    directly_readable: list[str] = []
    for relative_path in SENSITIVE_COMPILER_PATHS:
        path = root / relative_path
        status, path_stat, stopped_at, error_kind = _lstat_fixed_path(root, relative_path)
        if status == "absent":
            path_checks[relative_path] = {
                "present": False,
                "symlink": False,
                "kind": "absent",
                "directly_readable": False,
            }
            continue
        if status == "error" or path_stat is None:
            # An indeterminate boundary is not evidence of isolation.
            path_checks[relative_path] = {
                "present": "unknown",
                "symlink": False,
                "kind": "unknown",
                "directly_readable": True,
                "metadata_error": error_kind or "unknown",
                "stopped_at": stopped_at,
            }
            directly_readable.append(relative_path)
            continue

        is_symlink = status == "symlink"
        is_directory = stat.S_ISDIR(path_stat.st_mode)
        if is_symlink:
            # Do not resolve or follow a sensitive symlink. An unsandboxed
            # route could follow it, so fail closed on its mere presence.
            readable = True
            kind = "symlink" if stopped_at == relative_path else "symlink_ancestor"
        elif is_directory:
            readable = os.access(path, os.R_OK | os.X_OK)
            kind = "directory"
        else:
            readable = os.access(path, os.R_OK)
            kind = "file"
        path_checks[relative_path] = {
            "present": True if stopped_at == relative_path else "unknown",
            "symlink": is_symlink,
            "kind": kind,
            "directly_readable": readable,
        }
        if stopped_at != relative_path:
            path_checks[relative_path]["stopped_at"] = stopped_at
        if readable:
            directly_readable.append(relative_path)

    return {
        "route": "compiler_po_unsandboxed_same_identity",
        "enforceable": not directly_readable,
        "directly_readable_paths": directly_readable,
        "paths": path_checks,
        "file_contents_read": False,
        "directories_enumerated": False,
        "symlinks_followed": False,
    }


def verify_s11_readiness(root: Path) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {
        "phase": "pre_compiler",
        "paid_execution_performed": False,
        "private_payload_values_read": False,
        "phone_evidence_required": False,
        "environment_values_read": False,
    }

    checks["trusted_outer_activation_receipt_validator"] = {
        "status": "unavailable",
        "reason": "not_implemented",
        "activation_receipt_values_read": False,
    }
    # This is deliberately unconditional: there is no config flag or
    # self-attested payload that can claim the absent validator exists.
    errors.append(
        "S11 trusted outer activation receipt validator is unavailable; "
        "paid compiler/PO launch is forbidden until ship-bound activation evidence "
        "can be independently validated"
    )

    isolation = _inspect_sensitive_path_isolation(root)
    checks["sensitive_path_isolation"] = isolation
    for relative_path in isolation["directly_readable_paths"]:
        errors.append(
            "S11 sensitive-path isolation is not enforceable for the unsandboxed compiler/PO route: "
            f"{relative_path} is directly readable by the current process identity"
        )

    input_state: dict[str, Any] = {}
    for relative_path in REQUIRED_COMMITTED_INPUTS:
        state = _committed_input_state(root, relative_path)
        input_state[relative_path] = state
        if state.get("error"):
            errors.append(f"S11 compiler input is not sealed at HEAD: {relative_path}: {state['error']}")
    checks["committed_inputs"] = input_state

    def input_is_safe(relative_path: str) -> bool:
        state = input_state.get(relative_path, {})
        return bool(state.get("regular_file")) and not bool(state.get("symlink"))

    try:
        slices = _load_json(root / "ops/autonomy/slices.json") if input_is_safe("ops/autonomy/slices.json") else None
    except (OSError, json.JSONDecodeError) as exc:
        slices = None
        errors.append(f"cannot load S11 slice contract: {exc}")
    s11: dict[str, Any] | None = None
    if not isinstance(slices, list):
        errors.append("ops/autonomy/slices.json must contain a list")
    else:
        by_id = {item.get("id"): item for item in slices if isinstance(item, dict)}
        candidate = by_id.get("S11")
        if isinstance(candidate, dict):
            s11 = candidate
        else:
            errors.append("S11 is missing from slices.json")
        for dependency in ("S01", "S02"):
            status = by_id.get(dependency, {}).get("status")
            checks[f"{dependency.lower()}_status"] = status
            if status != "complete":
                errors.append(f"{dependency} must be complete before S11 compiler launch: {status}")

    if s11 is not None:
        slice_contract = {
            "status": s11.get("status"),
            "lane": s11.get("lane"),
            "risk": s11.get("risk"),
            "required": s11.get("required"),
            "brief": s11.get("brief"),
            "autoplan": s11.get("autoplan"),
            "activation_acceptance": s11.get("activation_acceptance"),
        }
        checks["slice_contract"] = slice_contract
        if s11.get("status") not in ACTIONABLE_STATUSES:
            errors.append(f"S11 is not in an actionable pre-compiler status: {s11.get('status')}")
        if s11.get("lane") != "compiler":
            errors.append(f"S11 lane must be compiler: {s11.get('lane')}")
        if s11.get("risk") != "high":
            errors.append(f"S11 risk must be high: {s11.get('risk')}")
        if s11.get("required") is not True:
            errors.append("S11 must remain required")
        if s11.get("brief") != S11_BRIEF:
            errors.append(f"S11 brief must be {S11_BRIEF}")
        if s11.get("autoplan") != S11_AUTOPLAN:
            errors.append(f"S11 autoplan must be {S11_AUTOPLAN}")
        if not {"S01", "S02"}.issubset(set(s11.get("depends_on") or [])):
            errors.append("S11 must depend on completed S01 and S02")
        if "private/evidence/S11" not in set(s11.get("evidence_dirs") or []):
            errors.append("S11 must keep private/evidence/S11 as its isolated runtime evidence root")

        deliverables = set(s11.get("deliverables") or [])
        missing_deliverables = sorted(REQUIRED_DELIVERABLES - deliverables)
        checks["missing_required_deliverables"] = missing_deliverables
        if missing_deliverables:
            errors.append("S11 is missing required deliverables: " + ", ".join(missing_deliverables))

        activation = s11.get("activation_acceptance")
        checks["activation_acceptance_exact"] = activation == ACTIVATION_ACCEPTANCE
        if activation != ACTIVATION_ACCEPTANCE:
            errors.append("S11 activation_acceptance must exactly match the canonical post-ship runtime contract")

        acceptance = s11.get("acceptance")
        if not isinstance(acceptance, list) or not acceptance or not all(isinstance(item, str) and item.strip() for item in acceptance):
            errors.append("S11 hermetic ship acceptance must be a non-empty command list")
            acceptance = []
        forbidden_ship_fragments = (
            "private/",
            "data/warehouse",
            "health_hub_runtime_root",
            "--runtime-root",
            "setup_permissions.py",
            "verify_mood_logging_recovery.py",
        )
        unsafe_acceptance = [
            command
            for command in acceptance
            if any(fragment in command.lower() for fragment in forbidden_ship_fragments)
        ]
        checks["hermetic_ship_acceptance"] = {
            "commands": acceptance,
            "unsafe_runtime_commands": unsafe_acceptance,
        }
        if unsafe_acceptance:
            errors.append("S11 ship acceptance is not hermetic; runtime/private activation belongs only in activation_acceptance")

    brief_text = ""
    autoplan_text = ""
    try:
        brief_text = (root / S11_BRIEF).read_text(encoding="utf-8") if input_is_safe(S11_BRIEF) else ""
    except OSError as exc:
        errors.append(f"cannot read S11 brief: {exc}")
    try:
        autoplan_text = (root / S11_AUTOPLAN).read_text(encoding="utf-8") if input_is_safe(S11_AUTOPLAN) else ""
    except OSError as exc:
        errors.append(f"cannot read S11 autoplan: {exc}")
    if autoplan_text:
        errors.extend(_validate_autoplan(autoplan_text))
    if brief_text and autoplan_text:
        contract_errors = _validate_contract_text(brief_text, autoplan_text)
        checks["two_boundary_contract"] = "ok" if not contract_errors else "error"
        errors.extend(contract_errors)

    try:
        policy = (
            yaml.safe_load((root / "ops/autonomy/policy.yaml").read_text(encoding="utf-8"))
            if input_is_safe("ops/autonomy/policy.yaml")
            else None
        )
    except (OSError, yaml.YAMLError) as exc:
        policy = None
        errors.append(f"cannot load autonomy policy: {exc}")
    bridge_errors, bridge_checks = _validate_tripwire_bridge(policy if isinstance(policy, dict) else {})
    checks["runtime_evidence_bridge"] = bridge_checks
    errors.extend(bridge_errors)

    permission_report = _inspect_fixed_permission_contract(root)
    checks["permissions"] = permission_report["checks"]
    if permission_report["status"] != "ok":
        errors.extend(f"runtime permission precondition: {error}" for error in permission_report["errors"])

    return {
        "status": "ok" if not errors else "error",
        "errors": sorted(dict.fromkeys(errors)),
        "warnings": warnings,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify S11 readiness without compiler or private evidence access.")
    parser.add_argument("--root", default=str(ROOT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_s11_readiness(Path(args.root))
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

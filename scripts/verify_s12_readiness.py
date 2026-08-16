#!/usr/bin/env python3
"""Fail-closed, offline readiness gate for S12 Oura production sync.

The gate runs before any compiler, OAuth, token, network, or provider-data
operation.  A tracked decision and opaque local bytes are not issuer authority,
so the current implementation cannot authorize S12.  It validates sealed
control inputs and safe private-source metadata only, leaving issuer-
authenticated authority as an explicit external prerequisite.
"""

from __future__ import annotations

import argparse
import json
import stat
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Mapping

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parents[1]
SCHEMA_REL = "ops/autonomy/schemas/provider_authority_decision.schema.json"
DECISION_REL = "docs/evidence/s12-oura-provider-authority-decision.json"
BRIEF_REL = "docs/briefs/s12-oura-production-sync.autonomous-brief.md"
AUTOPLAN_REL = "docs/gstack/s12-oura-production-sync-autoplan.md"
SLICES_REL = "ops/autonomy/slices.json"
POLICY_REL = "ops/autonomy/policy.yaml"
VERIFIER_REL = "scripts/verify_s12_readiness.py"
TEST_REL = "tests/autonomy/test_verify_s12_readiness.py"
PRIVATE_AUTHORITY_PREFIX = "private/evidence/S12/authority/"

ACTIONABLE_OR_EXTERNAL_STATUSES = {
    "pending",
    "waiting_for_playbook",
    "replan_required",
    "evidence_ready",
    "blocked_external",
    "complete",
}
REQUIRED_DEPENDENCIES = frozenset({"S03", "S04", "S05", "S11"})
REQUIRED_COMMITTED_INPUTS = (
    BRIEF_REL,
    AUTOPLAN_REL,
    SLICES_REL,
    POLICY_REL,
    SCHEMA_REL,
    VERIFIER_REL,
    TEST_REL,
)
IMMUTABLE_AUTHORITY_INPUTS = frozenset({SCHEMA_REL, VERIFIER_REL, TEST_REL})
PRE_READINESS_COMMANDS = ["python scripts/verify_s12_readiness.py --json"]
ACTIVATION_ACCEPTANCE = {
    "command": "python scripts/verify_s12_sync.py --runtime-root-env HEALTH_HUB_RUNTIME_ROOT --json",
    "runtime_root": "canonical",
    "runtime_root_env": "HEALTH_HUB_RUNTIME_ROOT",
    "reads_private_aggregate_evidence": True,
    "live_warehouse_access": "read_only",
    "required_after_ship_acceptance": True,
    "completion_requires_status": "ok",
}
REQUIRED_DELIVERABLES = frozenset(
    {
        "src/ingestion/oura_auth.py",
        "src/ingestion/oura_sync.py",
        "src/warehouse/locking.py",
        "scripts/sync_oura.py",
        "scripts/evidence/oura_sync_attestation.py",
        "scripts/verify_s12_sync.py",
        "docs/reviews/s12-autonomous-authority-review.md",
        "docs/reviews/s12-autonomous-security-privacy-review.md",
        "docs/reviews/s12-autonomous-ingestion-integrity-review.md",
        "docs/evidence/s12-oura-production-sync-command-evidence.json",
    }
)
AUTHENTICATED_AUTHORITY_BLOCKER = (
    "issuer-authenticated authority validation is not implemented; a self-authored tracked decision "
    "and opaque local bytes cannot authorize S12"
)

REQUIRED_COVERED_USES = frozenset(
    {
        "provider_data_acquisition",
        "local_storage",
        "finite_retention",
        "mood_correlation",
        "ridge_training",
        "ridge_evaluation",
        "retrospective_explanations",
        "local_backups",
        "raw_audit_retention",
    }
)

REQUIRED_BRIEF_TERMS = (
    "oura api/mcp agreement",
    "2026-06-08",
    "section 4(a)(iii)",
    "section 4(d)",
    "section 6(g)",
    "ordinary prior written consent",
    "separate written agreement",
    "non-api acquisition route",
    "prior written consent",
    "blocked_external",
    "not provider reopening",
    "oauth",
    "refresh-token",
    "waketime_utc",
    "home_timezone",
    "dst",
    "data/.healthhub.lock",
    "checkpoint",
    "chronological",
    "0700",
    "0600",
    "aggregate",
    "autonomous_gate_review",
)

REQUIRED_AUTOPLAN_TERMS = (
    *REQUIRED_BRIEF_TERMS,
    "deliverables",
    "verification expectations",
    "manual gates are forbidden",
    "implementation tasks",
    "files:",
    "verify:",
    "ridge training and evaluation",
    "raw audit retention",
    "explicitly superseding sections 4(d) and 6(g)",
    "issuer-authenticated",
    "immutable sealed inputs",
    "pre_ship_commands",
    "self-authored",
)


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def run_git(root: Path, *argv: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *argv],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def tracked_and_matches_head(root: Path, rel: str) -> tuple[bool, str]:
    tracked = run_git(root, "cat-file", "-e", f"HEAD:{rel}")
    if tracked.returncode != 0:
        return False, "not tracked at HEAD"
    head_blob = run_git(root, "rev-parse", f"HEAD:{rel}")
    worktree_blob = run_git(root, "hash-object", "--", rel)
    if head_blob.returncode != 0 or worktree_blob.returncode != 0:
        return False, "could not bind tracked and worktree blobs"
    if head_blob.stdout.strip() != worktree_blob.stdout.strip():
        return False, "worktree bytes do not match HEAD"
    return True, "tracked at HEAD and worktree bytes match"


def committed_input_state(root: Path, rel: str) -> dict[str, Any]:
    path = root / rel
    state: dict[str, Any] = {
        "tracked_at_head": False,
        "worktree_matches_head": False,
        "regular_file": False,
        "symlink": path.is_symlink(),
    }
    if Path(rel).is_absolute() or ".." in Path(rel).parts:
        state["error"] = "unsafe repository-relative path"
        return state
    if state["symlink"] or not path.is_file():
        state["error"] = "missing, non-regular, or symlink input"
        return state
    state["regular_file"] = True
    tracked, binding = tracked_and_matches_head(root, rel)
    state["tracked_at_head"] = tracked
    state["worktree_matches_head"] = tracked
    state["binding"] = binding
    if not tracked:
        state["error"] = binding
    return state


def git_path_is_tracked(root: Path, rel: str) -> bool:
    return run_git(root, "ls-files", "--error-unmatch", "--", rel).returncode == 0


def contained_path(root: Path, rel: str) -> Path | None:
    raw = Path(rel)
    if not rel or raw.is_absolute() or ".." in raw.parts:
        return None
    candidate = (root / raw).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def has_symlink_component(root: Path, rel: str) -> bool:
    current = root
    for part in Path(rel).parts:
        current = current / part
        if current.is_symlink():
            return True
    return False


def schema_errors(schema: Mapping[str, Any], payload: Any) -> list[str]:
    if not isinstance(payload, Mapping):
        return ["$: authority decision must be a JSON object"]
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    errors: list[str] = []
    for error in sorted(
        validator.iter_errors(payload),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    ):
        location = ".".join(str(part) for part in error.absolute_path) or "$"
        errors.append(f"{location}: {error.message}")
    return errors


def validate_contract_document(path: Path, required_terms: tuple[str, ...]) -> list[str]:
    if not path.is_file():
        return [f"missing S12 contract document: {path.name}"]
    text = " ".join(path.read_text(encoding="utf-8").lower().split())
    return [
        f"{path.name} missing required contract term: {term}"
        for term in required_terms
        if " ".join(term.split()) not in text
    ]


def validate_autoplan_write_scope(path: Path) -> list[str]:
    """Reject immutable authority controls from compiler task Files fields."""

    if not path.is_file():
        return []
    errors: list[str] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.lstrip().lower().startswith("files:"):
            continue
        lowered = line.lower()
        for rel in sorted(IMMUTABLE_AUTHORITY_INPUTS):
            if rel.lower() in lowered:
                errors.append(f"S12 immutable authority input appears in PO Files scope: {rel}")
    return errors


def validate_slice_contract(slices: Any) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    checks: dict[str, Any] = {}
    if not isinstance(slices, list):
        return ["ops/autonomy/slices.json must contain a list"], checks
    by_id = {item.get("id"): item for item in slices if isinstance(item, Mapping)}
    s12 = by_id.get("S12")
    if not isinstance(s12, Mapping):
        return ["S12 is missing from slices.json"], checks

    checks["slice_contract"] = {
        "status": s12.get("status"),
        "lane": s12.get("lane"),
        "risk": s12.get("risk"),
        "required": s12.get("required"),
        "brief": s12.get("brief"),
        "autoplan": s12.get("autoplan"),
        "pre_po_commands": s12.get("pre_po_commands"),
        "pre_ship_commands": s12.get("pre_ship_commands"),
        "activation_acceptance": s12.get("activation_acceptance"),
    }
    if s12.get("status") not in ACTIONABLE_OR_EXTERNAL_STATUSES:
        errors.append(f"S12 is not in an allowed pre-completion lifecycle status: {s12.get('status')}")
    if s12.get("lane") != "compiler_external_evidence":
        errors.append(f"S12 lane must be compiler_external_evidence: {s12.get('lane')}")
    if s12.get("risk") != "high":
        errors.append(f"S12 risk must be high: {s12.get('risk')}")
    if s12.get("required") is not True:
        errors.append("S12 must remain required")
    if s12.get("brief") != BRIEF_REL:
        errors.append(f"S12 brief must be {BRIEF_REL}")
    if s12.get("autoplan") != AUTOPLAN_REL:
        errors.append(f"S12 autoplan must be {AUTOPLAN_REL}")
    if not REQUIRED_DEPENDENCIES.issubset(set(s12.get("depends_on") or [])):
        errors.append("S12 must depend on S03, S04, S05, and S11")
    for dependency in sorted(REQUIRED_DEPENDENCIES):
        status = by_id.get(dependency, {}).get("status")
        checks[f"{dependency.lower()}_status"] = status
        if status != "complete":
            errors.append(f"{dependency} must be complete before S12 launch: {status}")
    if s12.get("pre_po_commands") != PRE_READINESS_COMMANDS:
        errors.append("S12 pre_po_commands must rerun the exact current readiness gate")
    if s12.get("pre_ship_commands") != PRE_READINESS_COMMANDS:
        errors.append("S12 pre_ship_commands must rerun the exact current readiness gate")
    if s12.get("activation_acceptance") != ACTIVATION_ACCEPTANCE:
        errors.append("S12 activation_acceptance must exactly match the canonical read-only sync-proof contract")
    if "private/evidence/S12/authority" not in set(s12.get("evidence_dirs") or []):
        errors.append("S12 must keep its private authority evidence in the isolated S12 authority root")

    missing_deliverables = sorted(REQUIRED_DELIVERABLES - set(s12.get("deliverables") or []))
    checks["missing_required_deliverables"] = missing_deliverables
    if missing_deliverables:
        errors.append("S12 is missing required deliverables: " + ", ".join(missing_deliverables))

    acceptance = s12.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance or not all(
        isinstance(item, str) and item.strip() for item in acceptance
    ):
        errors.append("S12 hermetic ship acceptance must be a non-empty command list")
        acceptance = []
    forbidden_fragments = (
        "private/",
        "health_hub_runtime_root",
        "--runtime-root",
        "verify_s12_sync.py",
        "sync_oura.py",
    )
    unsafe_acceptance = [
        command
        for command in acceptance
        if any(fragment in command.lower() for fragment in forbidden_fragments)
    ]
    checks["hermetic_ship_acceptance"] = {
        "commands": acceptance,
        "unsafe_runtime_commands": unsafe_acceptance,
    }
    if unsafe_acceptance:
        errors.append("S12 ship acceptance is not hermetic; runtime/private proof belongs only in activation_acceptance")
    return errors, checks


def validate_authority_semantics(decision: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if decision.get("status") != "authorized":
        errors.append("authority decision status must be authorized")

    authority = decision.get("authority_basis")
    authority = authority if isinstance(authority, Mapping) else {}
    basis = authority.get("basis_type")
    confirmed_by = authority.get("confirmed_by")
    source = decision.get("source_evidence")
    source = source if isinstance(source, Mapping) else {}
    document_type = source.get("document_type")
    valid_pairs = {
        "oura_separate_written_agreement_superseding_ai_restrictions": (
            "oura",
            "oura_separate_written_ai_data_agreement",
        ),
        "legally_confirmed_non_api_acquisition_route": (
            "qualified_legal_review",
            "legal_confirmation_of_non_api_route_outside_agreement_restrictions",
        ),
    }
    expected = valid_pairs.get(str(basis))
    if expected is None:
        errors.append(
            "authority basis must be a separate Oura agreement superseding Sections 4(d) and 6(g), "
            "or a legally confirmed non-API route outside those restrictions; ordinary prior written consent is insufficient"
        )
    elif (confirmed_by, document_type) != expected:
        errors.append("authority confirmer and private source document type do not match the selected basis")

    explicit_supersedes = authority.get("explicitly_supersedes")
    superseded_set = (
        set(explicit_supersedes)
        if isinstance(explicit_supersedes, list)
        and all(isinstance(item, str) for item in explicit_supersedes)
        else set()
    )
    required_supersession = {
        "section_4d_ai_model_prohibition",
        "section_6g_user_data_training_prohibition",
    }
    if basis == "oura_separate_written_agreement_superseding_ai_restrictions":
        if authority.get("uses_oura_api") is not True:
            errors.append("the separate Oura agreement route must explicitly identify Oura API use")
        if superseded_set != required_supersession:
            errors.append(
                "the separate Oura agreement must explicitly supersede both Section 4(d) and Section 6(g)"
            )
        if authority.get("outside_api_mcp_agreement_restrictions") is not False:
            errors.append("the Oura API route must not claim to be outside the API/MCP Agreement restrictions")
    elif basis == "legally_confirmed_non_api_acquisition_route":
        if authority.get("uses_oura_api") is not False:
            errors.append("the alternative authority route must be non-API")
        if superseded_set:
            errors.append("the non-API route must not claim contractual supersession")
        if authority.get("outside_api_mcp_agreement_restrictions") is not True:
            errors.append("the non-API route must be legally confirmed outside the API/MCP Agreement restrictions")

    if authority.get("revoked") is not False:
        errors.append("authority decision must explicitly state that authority is not revoked")
    try:
        valid_from = date.fromisoformat(str(authority.get("valid_from") or ""))
        raw_valid_through = authority.get("valid_through")
        valid_through = date.fromisoformat(str(raw_valid_through)) if raw_valid_through is not None else None
    except ValueError:
        errors.append("authority validity dates must be ISO dates")
    else:
        today = datetime.now(UTC).date()
        if valid_from > today:
            errors.append("authority is not yet effective")
        if valid_through is not None and valid_through < valid_from:
            errors.append("authority valid_through precedes valid_from")
        if valid_through is not None and valid_through < today:
            errors.append("authority has expired")

    covered = decision.get("covered_uses")
    covered_set = (
        set(covered)
        if isinstance(covered, list) and all(isinstance(item, str) for item in covered)
        else set()
    )
    if covered_set != REQUIRED_COVERED_USES:
        missing = sorted(REQUIRED_COVERED_USES - covered_set)
        unexpected = sorted(covered_set - REQUIRED_COVERED_USES)
        detail: list[str] = []
        if missing:
            detail.append("missing=" + ",".join(missing))
        if unexpected:
            detail.append("unexpected=" + ",".join(unexpected))
        suffix = ": " + "; ".join(detail) if detail else ""
        errors.append("authority decision does not cover the exact S12 use set" + suffix)

    agreement = decision.get("agreement")
    agreement = agreement if isinstance(agreement, Mapping) else {}
    if agreement.get("effective_date") != "2026-06-08":
        errors.append("authority decision is not grounded in the 2026-06-08 Oura API/MCP Agreement")
    if agreement.get("section_4a_iii_rule") != "prior_written_consent_exception_does_not_override_sections_4d_or_6g":
        errors.append("authority decision incorrectly treats Section 4(a)(iii) prior consent as overriding Sections 4(d) or 6(g)")
    if agreement.get("section_4d_rule") != "oura_api_prohibited_for_ai_model_development_training_evaluation_or_input":
        errors.append("authority decision does not preserve the Section 4(d) Oura API AI Model prohibition")
    if agreement.get("section_6g_rule") != "user_data_training_prohibited":
        errors.append("authority decision does not preserve the Section 6(g) User Data training prohibition")
    if agreement.get("retention_rule") != "agreement_constrained":
        errors.append("authority decision does not preserve the Agreement's retention constraint")

    policy = decision.get("provider_policy")
    policy = policy if isinstance(policy, Mapping) else {}
    if (
        policy.get("active_sleep_source") != "oura"
        or policy.get("eight_sleep_status") != "fallback_only"
        or policy.get("provider_reopening") is not False
    ):
        errors.append("S12 must preserve Oura-only v1 and must not reopen provider policy")
    return errors


def validate_private_source(root: Path, decision: Mapping[str, Any]) -> tuple[list[str], dict[str, Any]]:
    errors: list[str] = []
    checks: dict[str, Any] = {
        "path": None,
        "exists": False,
        "regular_file": False,
        "symlink_free": False,
        "tracked": None,
        "mode": None,
        "size_bytes": None,
        "sha256_validated": False,
        "contents_read": False,
    }
    source = decision.get("source_evidence")
    if not isinstance(source, Mapping):
        return ["source_evidence must be an object"], checks

    rel = str(source.get("path") or "")
    checks["path"] = rel or None
    if not rel.startswith(PRIVATE_AUTHORITY_PREFIX):
        return [f"private authority source must be under {PRIVATE_AUTHORITY_PREFIX}"], checks
    path = contained_path(root, rel)
    if path is None:
        return ["private authority source path is unsafe or escapes the repository"], checks
    if has_symlink_component(root, rel):
        errors.append("private authority source path must not contain symlinks")
        return errors, checks
    checks["symlink_free"] = True
    if not path.exists():
        errors.append("private authority source is missing")
        return errors, checks
    checks["exists"] = True

    try:
        file_stat = path.stat()
    except OSError as exc:
        errors.append(f"private authority source metadata cannot be inspected: {exc}")
        return errors, checks
    is_regular = stat.S_ISREG(file_stat.st_mode)
    checks["regular_file"] = is_regular
    if not is_regular:
        errors.append("private authority source must be a regular file")
        return errors, checks

    tracked = git_path_is_tracked(root, rel)
    checks["tracked"] = tracked
    if tracked:
        errors.append("private authority source must not be tracked by git")

    mode = stat.S_IMODE(file_stat.st_mode)
    mode_text = f"0o{mode:03o}"
    checks["mode"] = mode_text
    checks["size_bytes"] = file_stat.st_size
    if mode != 0o600:
        errors.append(f"private authority source mode must be 0o600, found {mode_text}")
    if file_stat.st_size != source.get("size_bytes"):
        errors.append("private authority source size does not match tracked decision metadata")

    # Local bytes and a self-authored digest cannot authenticate an issuer.
    # Do not read the opaque source here; a future issuer-authenticated
    # validation amendment must define and verify the real proof format.
    return errors, checks


def verify_s12_readiness(root: Path) -> dict[str, Any]:
    root = root.resolve()
    control_errors: list[str] = []
    authority_errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {
        "phase": "pre_compiler_or_pre_ship",
        "paid_execution_performed": False,
        "network_accessed": False,
        "oauth_or_token_inspected": False,
        "private_source_contents_read": False,
        "private_source_contents_parsed": False,
        "issuer_authenticated_authority_validator": "not_implemented",
    }

    input_state: dict[str, Any] = {}
    for rel in REQUIRED_COMMITTED_INPUTS:
        state = committed_input_state(root, rel)
        input_state[rel] = state
        if state.get("error"):
            control_errors.append(f"S12 immutable compiler input is not sealed at HEAD: {rel}: {state['error']}")
    checks["committed_inputs"] = input_state

    def input_is_safe(rel: str) -> bool:
        state = input_state.get(rel, {})
        return bool(state.get("regular_file")) and not bool(state.get("symlink"))

    schema_path = root / SCHEMA_REL
    if not schema_path.is_file():
        control_errors.append(f"missing authority schema: {SCHEMA_REL}")
        schema: Mapping[str, Any] | None = None
    else:
        try:
            raw_schema = load_json(schema_path)
            if not isinstance(raw_schema, Mapping):
                raise ValueError("schema must be a JSON object")
            Draft202012Validator.check_schema(raw_schema)
            schema = raw_schema
            checks["authority_schema"] = "valid"
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            schema = None
            control_errors.append(f"invalid authority schema: {exc}")
        except Exception as exc:  # jsonschema raises version-specific schema exceptions
            schema = None
            control_errors.append(f"invalid authority schema: {exc}")

    control_errors.extend(validate_contract_document(root / BRIEF_REL, REQUIRED_BRIEF_TERMS))
    control_errors.extend(validate_contract_document(root / AUTOPLAN_REL, REQUIRED_AUTOPLAN_TERMS))
    control_errors.extend(validate_autoplan_write_scope(root / AUTOPLAN_REL))
    try:
        slices = load_json(root / SLICES_REL) if input_is_safe(SLICES_REL) else None
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        slices = None
        control_errors.append(f"cannot load S12 lifecycle contract: {exc}")
    lifecycle_errors, lifecycle_checks = validate_slice_contract(slices)
    control_errors.extend(lifecycle_errors)
    checks.update(lifecycle_checks)

    decision_path = root / DECISION_REL
    checks["authority_decision"] = DECISION_REL if decision_path.is_file() else None
    if not decision_path.is_file():
        authority_errors.append(
            f"missing sanitized tracked authority decision: {DECISION_REL}; require either a separate Oura agreement "
            "explicitly superseding Sections 4(d) and 6(g), or qualified legal confirmation of a non-API route "
            "outside those restrictions"
        )
    else:
        tracked, binding = tracked_and_matches_head(root, DECISION_REL)
        checks["authority_decision_git_binding"] = binding
        if not tracked:
            authority_errors.append(f"authority decision is not durably tracked: {binding}")
        try:
            raw_decision = load_json(decision_path)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raw_decision = None
            authority_errors.append(f"authority decision is not valid JSON: {exc}")

        if raw_decision is not None and schema is not None:
            validation_errors = schema_errors(schema, raw_decision)
            authority_errors.extend(f"authority schema: {error}" for error in validation_errors)
            if isinstance(raw_decision, Mapping):
                authority_errors.extend(validate_authority_semantics(raw_decision))
                private_errors, private_checks = validate_private_source(root, raw_decision)
                authority_errors.extend(private_errors)
                checks["private_source"] = private_checks

    # A local decision and local bytes are self-attestation.  Keep this as an
    # unconditional external blocker until a separately reviewed amendment
    # implements issuer-authenticated proof verification.
    authority_errors.append(AUTHENTICATED_AUTHORITY_BLOCKER)

    errors = [*control_errors, *authority_errors]
    if control_errors:
        status = "error"
    elif authority_errors:
        status = "blocked_external"
    else:
        status = "ok"
    checks["control_error_count"] = len(control_errors)
    checks["authority_blocker_count"] = len(authority_errors)
    checks["provider_policy"] = "oura_only_v1_not_reopened"
    return {
        "status": status,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
    }


def run_s12_sync_proof(root: Path) -> dict[str, Any]:
    """Refuse to execute generated sync code without a trusted proof boundary.

    A future amendment may validate an immutable, ship-bound aggregate receipt
    produced under the activation sandbox.  Running a future PO-generated
    verifier directly from this trusted readiness process would give that code
    ambient filesystem and network authority, so this implementation remains
    deliberately unavailable.
    """

    return {
        "status": "blocked_external",
        "errors": [
            "trusted current S12 sync-receipt validation is not implemented; generated sync code was not executed"
        ],
        "checks": {
            "executed": False,
            "network_accessed": False,
            "runtime_written": False,
            "trusted_receipt_validator": "not_implemented",
        },
    }


def verify_s12_continuity(root: Path) -> dict[str, Any]:
    """Revalidate current authority, then current aggregate activation proof."""

    authority = verify_s12_readiness(root)
    if authority.get("status") != "ok":
        return {
            "status": authority.get("status", "error"),
            "errors": [f"S12 current authority: {error}" for error in authority.get("errors", [])],
            "checks": {
                "authority_status": authority.get("status"),
                "sync_proof_status": "not_run_authority_blocked",
            },
        }
    sync_proof = run_s12_sync_proof(root)
    return {
        "status": sync_proof.get("status", "error"),
        "errors": [f"S12 current sync proof: {error}" for error in sync_proof.get("errors", [])],
        "checks": {
            "authority_status": "ok",
            "sync_proof_status": sync_proof.get("status"),
            "sync_proof": sync_proof.get("checks", {}),
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify offline S12 Oura authority readiness.")
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
    if report["status"] == "ok":
        return 0
    if report["status"] == "blocked_external":
        return 2
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Verify S06 launch readiness BEFORE any usage-billed SWR call.

SWR stage generations are the only usage-billed resource in this project;
this gate exists to stop a launch at zero marginal cost when any
deterministic precondition is missing — most critically when S05's
deliverables (which S06 builds on) are not tracked at HEAD, a condition the
paid pipeline would otherwise discover only after a full five-stage spend.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.check_no_tracked_data import check_no_tracked_data  # noqa: E402
from ops.autonomy.autokeel import compute_state_digest  # noqa: E402
from scripts.evaluate_tripwires import evaluate_tripwires  # noqa: E402
from scripts.lane_decision_policy import validate_lane_decision  # noqa: E402
from scripts.slice_integration import verify_slice_integration  # noqa: E402
from scripts.verify_autokeel_invariants import verify_autokeel_invariants  # noqa: E402
from scripts.verify_s12_readiness import verify_s12_continuity  # noqa: E402

# Surfaces S06 consumes that must be tracked at HEAD before any paid
# generation grounds itself in the repo.
REQUIRED_DEPENDENCY_SURFACES = (
    "src/model/ridge.py",
    "src/model/baseline_gate.py",
    "src/model/eval_log.py",
    "scripts/retrain_model.py",
    "docs/reviews/s05-autonomous-model-gate-review.md",
    "docs/reviews/s05-autonomous-statistical-validity-review.md",
)

# Product/runtime surfaces delivered by the pre-S06 recovery slices. They do
# not exist yet while S11/S12 are incomplete; their absence is an intentional
# zero-spend stop, and a future S06 lane decision must bind their committed
# blobs after both slices have shipped and activated.
REQUIRED_RECOVERY_DEPENDENCY_SURFACES = (
    "app/mood_form.py",
    "src/model/display_gate.py",
    "src/warehouse/locking.py",
    "scripts/run_mood_form.py",
    "scripts/evidence/baseline_gate_report.py",
    "scripts/evidence/mood_transport_report.py",
    "scripts/evidence/mood_compliance_report.py",
    "scripts/verify_mood_logging_recovery.py",
    "src/ingestion/oura_auth.py",
    "src/ingestion/oura_sync.py",
    "scripts/sync_oura.py",
    "scripts/evidence/oura_sync_attestation.py",
    "scripts/verify_s12_sync.py",
)

REQUIRED_INPUT_DOCS = (
    "docs/gstack/s06-counterfactual-generator-autoplan.md",
    "docs/briefs/s06-counterfactual-generator.autonomous-brief.md",
    "docs/gstack/health-data-hub-office-hours.md",
)

REQUIRED_CONTROL_SURFACES = (
    "docs/briefs/s11-mood-logging-recovery.autonomous-brief.md",
    "docs/briefs/s12-oura-production-sync.autonomous-brief.md",
    "docs/evidence/event-log-legacy-id-reconciliation-20260816.json",
    "docs/gstack/s11-mood-logging-recovery-autoplan.md",
    "docs/gstack/s12-oura-production-sync-autoplan.md",
    "ops/autonomy/autokeel.py",
    "ops/autonomy/policy.yaml",
    "ops/autonomy/schemas/slice_integration_receipt_v2.schema.json",
    "ops/autonomy/schemas/slices.schema.json",
    "ops/autonomy/schemas/tripwire_baseline_gate.schema.json",
    "ops/autonomy/schemas/tripwire_mood_compliance.schema.json",
    "ops/autonomy/schemas/tripwire_mood_transport.schema.json",
    "scripts/close_failure.py",
    "scripts/build_slice_integration_receipt.py",
    "scripts/evaluate_tripwires.py",
    "scripts/lane_decision_policy.py",
    "scripts/materialize_swr_lane_decision.py",
    "scripts/setup_permissions.py",
    "scripts/slice_integration.py",
    "scripts/tripwire_evidence.py",
    "scripts/verify_autokeel_invariants.py",
    "scripts/verify_event_log.py",
    "scripts/verify_s06_readiness.py",
    "scripts/verify_s11_readiness.py",
    "scripts/verify_s12_readiness.py",
    "scripts/verify_ship_invariants.py",
    "scripts/verify_slice.py",
    "scripts/verify_slice_integration.py",
    "scripts/verify_v1.py",
)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else default


def iter_jsonl(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def tracked_at_head(root: Path, rel: str) -> bool:
    proc = subprocess.run(
        ["git", "cat-file", "-e", f"HEAD:{rel}"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode == 0


def git_stdout(root: Path, *argv: str) -> tuple[bool, str]:
    proc = subprocess.run(
        ["git", *argv],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode == 0, (proc.stdout if proc.returncode == 0 else proc.stderr).strip()


def dependency_closure(by_id: dict[str, dict[str, Any]], slice_id: str) -> tuple[list[str], list[str]]:
    ordered: list[str] = []
    errors: list[str] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(current: str) -> None:
        if current in visited:
            return
        if current in visiting:
            errors.append(f"dependency cycle reaches {current}")
            return
        visiting.add(current)
        item = by_id.get(current)
        if not isinstance(item, dict):
            errors.append(f"dependency is missing from slices.json: {current}")
        else:
            for dependency in item.get("depends_on", []) or []:
                visit(str(dependency))
            if current != slice_id:
                ordered.append(current)
        visiting.remove(current)
        visited.add(current)

    visit(slice_id)
    return ordered, errors


def verify_s06_readiness(root: Path) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    slices = load_json(root / "ops/autonomy/slices.json", [])
    by_id = {item.get("id"): item for item in slices if isinstance(item, dict)}
    s06 = by_id.get("S06")
    if not isinstance(s06, dict):
        return {"status": "error", "errors": ["S06 not found in slices.json"], "warnings": [], "checks": checks}

    dependencies, dependency_errors = dependency_closure(by_id, "S06")
    errors.extend(dependency_errors)
    checks["dependency_closure"] = dependencies
    for dep in dependencies:
        status = by_id.get(dep, {}).get("status")
        checks[f"{str(dep).lower()}_status"] = status
        if status != "complete":
            errors.append(f"{dep} must be complete before S06 launch: {status}")

    # Revalidate the current technical S12 contract and its read-only
    # aggregate activation/sync proof before any paid S06 work.
    s12_continuity = verify_s12_continuity(root)
    checks["s12_continuity"] = {
        "status": s12_continuity.get("status"),
        **s12_continuity.get("checks", {}),
    }
    if s12_continuity.get("status") != "ok":
        continuity_errors = s12_continuity.get("errors", []) or [
            "S12 current readiness and activation/sync proof are not ok"
        ]
        errors.extend(f"S06 production-sync continuity gate: {error}" for error in continuity_errors)

    checks["s06_status"] = s06.get("status")
    if s06.get("status") not in {"pending", "replan_required", "waiting_for_playbook", "evidence_ready"}:
        errors.append(f"S06 is not in an actionable pre-launch status: {s06.get('status')}")

    # Dependency surfaces tracked at HEAD: the paid SWR pipeline and the PO
    # worktrees ground in HEAD, not in unmerged ship branches.
    surface_state = {}
    for rel in (*REQUIRED_DEPENDENCY_SURFACES, *REQUIRED_RECOVERY_DEPENDENCY_SURFACES):
        present = tracked_at_head(root, rel)
        surface_state[rel] = "tracked" if present else "MISSING_AT_HEAD"
        if not present:
            errors.append(
                f"dependency surface is not tracked at HEAD: {rel} "
                "(land every completed dependency before spending any SWR generation)"
            )
    checks["dependency_surfaces"] = surface_state

    for rel in (*REQUIRED_INPUT_DOCS, *REQUIRED_CONTROL_SURFACES):
        if not tracked_at_head(root, rel):
            errors.append(f"S06 prerequisite is not tracked at HEAD: {rel}")

    primary_input_state: dict[str, Any] = {}
    for rel in REQUIRED_INPUT_DOCS:
        ok_head_blob, head_blob = git_stdout(root, "rev-parse", f"HEAD:{rel}")
        ok_worktree_blob, worktree_blob = git_stdout(root, "hash-object", "--", rel)
        matches_head = ok_head_blob and ok_worktree_blob and worktree_blob == head_blob
        primary_input_state[rel] = {
            "head_blob": head_blob if ok_head_blob else None,
            "worktree_blob": worktree_blob if ok_worktree_blob else None,
            "matches_head": matches_head,
        }
        if not matches_head:
            errors.append(
                f"S06 primary input worktree bytes do not match HEAD: {rel} "
                "(commit the final contract or restore the committed bytes before launch)"
            )
    checks["primary_input_worktree_bindings"] = primary_input_state

    integration_checks: dict[str, Any] = {}
    for dep in dependencies:
        if by_id.get(dep, {}).get("status") != "complete":
            continue
        integration = verify_slice_integration(root, dep)
        integration_checks[dep] = {
            "status": integration.get("status"),
            "classification": integration.get("classification"),
        }
        if integration.get("status") != "ok":
            errors.extend(
                f"{dep} is not durably integrated: {error}"
                for error in integration.get("errors", [])
            )
    checks["dependency_integration"] = integration_checks

    checks["lane_decision"] = s06.get("lane_decision")
    if not s06.get("lane_decision"):
        errors.append(
            "S06 lane_decision artifact missing: run scripts/materialize_swr_lane_decision.py S06 "
            "after the final recovery commit; the sanctioned writer records its event and refreshes "
            "the digest before the next tick"
        )
    else:
        decision_rel = str(s06["lane_decision"])
        lane_decision_errors = validate_lane_decision(root, s06)
        checks["lane_decision_policy"] = {
            "status": "ok" if not lane_decision_errors else "error",
            "errors": lane_decision_errors,
        }
        if lane_decision_errors:
            errors.extend(lane_decision_errors)
        else:
            decision_path = root / decision_rel
            decision = load_json(decision_path, {})
            ok_head, head_commit = git_stdout(root, "rev-parse", "HEAD^{commit}")
            checks["head_commit"] = head_commit if ok_head else None
            if not ok_head or decision.get("head_commit") != head_commit:
                errors.append(
                    "S06 lane_decision is not bound to current HEAD; materialize a fresh decision "
                    "after all recovery and contract changes are committed"
                )
            expected_inputs: dict[str, str] = {}
            for rel in (
                *REQUIRED_DEPENDENCY_SURFACES,
                *REQUIRED_RECOVERY_DEPENDENCY_SURFACES,
                *REQUIRED_INPUT_DOCS,
                *REQUIRED_CONTROL_SURFACES,
            ):
                ok_blob, blob = git_stdout(root, "rev-parse", f"HEAD:{rel}")
                if ok_blob:
                    expected_inputs[rel] = blob
            checks["lane_decision_expected_input_tree"] = expected_inputs
            if decision.get("input_tree") != expected_inputs:
                errors.append("S06 lane_decision input_tree does not match the current committed prerequisites")

    state_digest = load_json(root / "ops/autonomy/state_digest.json", {})
    recorded_digests = state_digest.get("digests") if isinstance(state_digest, dict) else None
    current_digests = compute_state_digest(root)
    digest_mismatches = sorted(
        rel for rel, value in current_digests.items()
        if not isinstance(recorded_digests, dict) or recorded_digests.get(rel) != value
    )
    checks["state_digest_mismatches"] = digest_mismatches
    if digest_mismatches:
        errors.append("state digest is stale or missing for: " + ", ".join(digest_mismatches))

    tripwire_report = evaluate_tripwires(root)
    checks["tripwires"] = {
        "status": tripwire_report.get("status"),
        "fired": [item.get("name") for item in tripwire_report.get("fired", []) if isinstance(item, dict)],
    }
    if tripwire_report.get("status") != "ok":
        errors.append("tripwires are unresolved: " + ", ".join(checks["tripwires"]["fired"]))

    invariant_report = verify_autokeel_invariants(root)
    checks["autokeel_invariants"] = invariant_report.get("status")
    if invariant_report.get("status") != "ok":
        errors.extend(f"AutoKeel invariant: {error}" for error in invariant_report.get("errors", []))

    # Diagnostics never open repo-local credential files. Only an explicitly
    # supplied process value can satisfy readiness; the separately authorized
    # billed SWR boundary owns any later exact-name repo-env lookup.
    readiness_marker = os.environ.get("AUTOKEEL_READINESS_OPENAI_API_KEY_PRESENT")
    if readiness_marker not in {None, "0", "1"}:
        errors.append("AUTOKEEL_READINESS_OPENAI_API_KEY_PRESENT must be exactly 0 or 1")
    openai_api_key = os.environ.get("OPENAI_API_KEY", "")
    openai_api_key_set = bool(openai_api_key.strip()) or readiness_marker == "1"
    checks["swr_required_env"] = {"OPENAI_API_KEY": "[SET]" if openai_api_key_set else "[UNSET]"}
    checks["swr_required_env_attestation"] = "parent_presence_only" if readiness_marker is not None else "direct_process"
    checks["process_environment_mutated"] = False
    if not openai_api_key_set:
        errors.append(
            "OPENAI_API_KEY must be explicitly present in the S06 readiness process environment; "
            "repo-local env files were not opened; secret_values_logged=false"
        )
    reviewer_clis = {}
    for cli in ("codex", "claude"):
        located = shutil.which(cli)
        reviewer_clis[cli] = "[FOUND]" if located else "[MISSING]"
        if not located:
            errors.append(f"reviewer CLI '{cli}' not found on PATH; the SWR review lane cannot run")
    checks["reviewer_clis"] = reviewer_clis

    tracked = check_no_tracked_data(root)
    if tracked["status"] != "ok":
        errors.extend(tracked["errors"])

    failures = list(iter_jsonl(root / "ops/autonomy/failure_ledger.jsonl") or [])
    open_high = [
        row for row in failures
        if row.get("open", True)
        and row.get("severity") in {"high", "critical"}
        and row.get("slice") in {"S06", "GLOBAL"}
    ]
    checks["open_high_or_critical_failures_for_s06_or_global"] = len(open_high)
    if open_high:
        errors.append(f"open high/critical S06 or GLOBAL failures block launch: {len(open_high)}")

    state = load_json(root / "ops/autonomy/autonomy_state.json", {})
    checks["active_run"] = state.get("active_run")
    checks["active_swr_run"] = state.get("active_swr_run")
    if state.get("active_run"):
        errors.append("active_run must be null before S06 launch")
    if state.get("active_swr_run"):
        errors.append("active_swr_run must be null before S06 launch")

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify S06 launch readiness (zero-spend pre-SWR gate).")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_s06_readiness(Path(args.root).resolve())
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Verify terminal ship invariants for a completed AutoKeel slice."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_autonomous_review_exists import check_review
from scripts.slice_integration import verify_slice_integration


POST_COMPLETION_REVIEW_INTERVENTION_SUFFIX = "post-completion-review-integration"


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


def git_stdout(root: Path, *argv: str) -> tuple[bool, str]:
    proc = subprocess.run(["git", *argv], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return proc.returncode == 0, proc.stdout.strip() if proc.returncode == 0 else proc.stderr.strip()


def repo_relative_file(root: Path, rel_path: str) -> Path | None:
    path = Path(rel_path)
    if path.is_absolute() or ".." in path.parts:
        return None
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None
    return resolved if resolved.is_file() else None


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_json_sha256(payload: Any) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_bound_hashes(
    root: Path,
    details: dict[str, Any],
    *,
    field: str,
    expected_paths: list[str],
) -> bool:
    bindings = details.get(field)
    if not isinstance(bindings, dict) or sorted(str(item) for item in bindings) != sorted(expected_paths):
        return False
    for rel_path in expected_paths:
        path = repo_relative_file(root, rel_path)
        expected = bindings.get(rel_path)
        if (
            path is None
            or not isinstance(expected, str)
            or len(expected) != 64
            or file_sha256(path) != expected
        ):
            return False
    return True


def classify_review_artifacts(
    root: Path,
    recorded_commit: str,
    artifacts: list[str],
) -> tuple[list[str], list[str]]:
    """Separate ship-time review blobs from reviews added or changed later."""

    historical: list[str] = []
    post_completion: list[str] = []
    for artifact in artifacts:
        current_path = repo_relative_file(root, artifact)
        historical_ok, historical_blob = git_stdout(root, "rev-parse", f"{recorded_commit}:{artifact}")
        current_ok = False
        current_blob = ""
        if current_path is not None:
            current_ok, current_blob = git_stdout(root, "hash-object", "--no-filters", artifact)
        if historical_ok and current_ok and historical_blob == current_blob:
            historical.append(artifact)
        else:
            post_completion.append(artifact)
    return historical, post_completion


def validate_post_completion_review_ratification(
    root: Path,
    events: list[dict[str, Any]],
    *,
    slice_id: str,
    run_id: str,
    ship_branch: str,
    ship_commit: str,
    review_artifacts: list[str],
    integration_receipt: str,
    slice_config: dict[str, Any],
) -> tuple[bool, str | None]:
    expected_name = f"{slice_id.lower()}-{POST_COMPLETION_REVIEW_INTERVENTION_SUFFIX}"
    candidates = [
        event
        for event in events
        if event.get("slice") == slice_id
        and event.get("event") == "manual_intervention_ratified"
        and str((event.get("details") or {}).get("name") or "") == expected_name
    ]
    if not candidates:
        return False, f"post-completion review artifacts require ratified intervention: {expected_name}"

    reasons: list[str] = []
    for event in reversed(candidates):
        event_details = event.get("details") or {}
        artifact_rel = str(event_details.get("artifact") or "")
        artifact_path = repo_relative_file(root, artifact_rel)
        if artifact_path is None:
            reasons.append(f"ratification artifact is missing or unsafe: {artifact_rel}")
            continue
        artifact_sha256 = event_details.get("artifact_sha256")
        if not isinstance(artifact_sha256, str) or file_sha256(artifact_path) != artifact_sha256:
            reasons.append(f"ratification artifact SHA-256 is missing or stale: {artifact_rel}")
            continue
        try:
            payload = json.loads(artifact_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            reasons.append(f"ratification artifact is not valid JSON: {artifact_rel}: {exc}")
            continue
        if not isinstance(payload, dict):
            reasons.append(f"ratification artifact must contain an object: {artifact_rel}")
            continue
        details = payload.get("details")
        if not isinstance(details, dict):
            reasons.append(f"ratification artifact is missing details: {artifact_rel}")
            continue

        errors: list[str] = []
        if payload.get("schema_version") != "autokeel.manual_intervention.v1":
            errors.append("schema_version")
        if payload.get("name") != expected_name:
            errors.append("name")
        if payload.get("slice") != slice_id:
            errors.append("slice")
        if details.get("historical_run_id") != run_id:
            errors.append("historical_run_id")
        if details.get("historical_ship_branch") != ship_branch:
            errors.append("historical_ship_branch")
        if details.get("historical_ship_commit") != ship_commit:
            errors.append("historical_ship_commit")
        if not integration_receipt or details.get("integration_receipt") != integration_receipt:
            errors.append("integration_receipt")
        else:
            receipt_path = repo_relative_file(root, integration_receipt)
            if (
                receipt_path is None
                or details.get("integration_receipt_sha256") != file_sha256(receipt_path)
            ):
                errors.append("integration_receipt_sha256")
        if details.get("slice_config_sha256") != canonical_json_sha256(slice_config):
            errors.append("slice_config_sha256")
        configured_reviews = details.get("review_artifacts")
        if not isinstance(configured_reviews, list) or sorted(str(item) for item in configured_reviews) != sorted(review_artifacts):
            errors.append("review_artifacts")
        elif not validate_bound_hashes(
            root,
            details,
            field="review_artifact_sha256",
            expected_paths=review_artifacts,
        ):
            errors.append("review_artifact_sha256")
        command_evidence = details.get("command_evidence")
        if not isinstance(command_evidence, list) or not command_evidence:
            errors.append("command_evidence")
        elif any(repo_relative_file(root, str(item)) is None for item in command_evidence):
            errors.append("command_evidence_paths")
        elif not validate_bound_hashes(
            root,
            details,
            field="command_evidence_sha256",
            expected_paths=[str(item) for item in command_evidence],
        ):
            errors.append("command_evidence_sha256")
        if details.get("verification_status") != "ok":
            errors.append("verification_status")
        if errors:
            reasons.append(f"ratification artifact has invalid fields ({', '.join(errors)}): {artifact_rel}")
            continue
        return True, artifact_rel

    return False, reasons[-1] if reasons else f"no valid ratification artifact found for {expected_name}"


def verify_ship_invariants(root: Path, slice_id: str) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}
    normalized = slice_id.upper()
    branch = f"ship/{normalized.lower()}"
    slices = load_json(root / "ops/autonomy/slices.json", [])
    slice_ = next((item for item in slices if isinstance(item, dict) and item.get("id") == normalized), None)
    if not isinstance(slice_, dict):
        return {"status": "error", "errors": [f"slice not found: {normalized}"], "warnings": [], "checks": checks}

    recorded_branch = str(slice_.get("ship_branch") or "")
    recorded_commit = str(slice_.get("ship_commit") or "")
    run_id = str(slice_.get("run_id") or "")
    checks["recorded_branch"] = recorded_branch
    checks["recorded_commit"] = recorded_commit
    checks["run_id"] = run_id
    if recorded_branch != branch:
        errors.append(f"ship branch mismatch: {recorded_branch} != {branch}")
    ok, branch_head = git_stdout(root, "rev-parse", f"{branch}^{{commit}}")
    if not ok:
        errors.append(f"{branch} does not exist: {branch_head}")
    checks["branch_head"] = branch_head if ok else None
    if ok and recorded_commit and branch_head != recorded_commit:
        errors.append(f"{branch} HEAD differs from recorded ship_commit: {branch_head} != {recorded_commit}")
    if recorded_commit:
        reachable, message = git_stdout(root, "cat-file", "-e", recorded_commit)
        checks["ship_commit_reachable"] = reachable
        if not reachable:
            errors.append(f"ship_commit is not reachable: {message}")
    if run_id:
        run_state = load_json(root / ".local/automation/plan_orchestrator/runs" / run_id / "run_state.json", {})
        run_branch = str(run_state.get("run_branch_name") or f"orchestrator/run/{run_id}")
        ok_run, run_head = git_stdout(root, "rev-parse", f"{run_branch}^{{commit}}")
        checks["run_state_branch"] = run_branch
        checks["run_state_branch_head"] = run_head if ok_run else None
        if ok_run and recorded_commit and run_head != recorded_commit:
            errors.append(f"ship_commit does not match terminal run branch HEAD: {recorded_commit} != {run_head}")
        elif not ok_run:
            warnings.append(f"could not resolve run_state branch: {run_branch}")

    events = list(iter_jsonl(root / "ops/autonomy/events.jsonl") or [])
    review_events = [
        event
        for event in events
        if event.get("slice") == normalized
        and event.get("event") == "review_artifacts_validated"
        and ".local/autokeel/ship-checkouts" in str((event.get("details") or {}).get("cwd") or "")
    ]
    acceptance_events = [
        event
        for event in events
        if event.get("slice") == normalized
        and event.get("event") == "slice_acceptance_passed"
        and ".local/autokeel/ship-checkouts" in str((event.get("details") or {}).get("cwd") or "")
    ]
    configured_review_artifacts = slice_.get("review_artifacts") if isinstance(slice_, dict) else []
    review_artifacts = [str(item) for item in configured_review_artifacts] if isinstance(configured_review_artifacts, list) else []
    historical_reviews, post_completion_reviews = classify_review_artifacts(root, recorded_commit, review_artifacts)
    checks["review_artifacts_required"] = bool(review_artifacts)
    checks["historical_review_artifacts"] = historical_reviews
    checks["post_completion_review_artifacts"] = post_completion_reviews
    if historical_reviews and not review_events:
        errors.append("review validation did not record a detached ship worktree cwd")
    if post_completion_reviews:
        review_report = check_review(root, normalized)
        checks["current_review_validation_status"] = review_report.get("status")
        if review_report.get("status") != "ok":
            errors.extend(f"post-completion review validation failed: {error}" for error in review_report.get("errors", []))
        ratified, ratification = validate_post_completion_review_ratification(
            root,
            events,
            slice_id=normalized,
            run_id=run_id,
            ship_branch=recorded_branch,
            ship_commit=recorded_commit,
            review_artifacts=post_completion_reviews,
            integration_receipt=str(slice_.get("integration_receipt") or ""),
            slice_config=slice_,
        )
        checks["post_completion_review_ratified"] = ratified
        checks["post_completion_review_ratification"] = ratification if ratified else None
        if not ratified:
            errors.append(str(ratification))
    if not acceptance_events:
        errors.append("verify_slice did not record a detached ship worktree cwd")
    ship_events = [
        event for event in events if event.get("slice") == normalized and event.get("event") == "slice_ship_branch_created"
    ]
    if ship_events:
        details = ship_events[-1].get("details") or {}
        before = details.get("operator_branch_before")
        after = details.get("operator_branch_after")
        if before and after and before != after:
            errors.append(f"operator checkout branch changed during ship: {before} -> {after}")
        elif not before or not after:
            warnings.append("ship event lacks operator branch before/after fields")
    else:
        errors.append("slice_ship_branch_created event missing")

    integration = verify_slice_integration(root, normalized)
    checks["continuation_integration"] = {
        "status": integration.get("status"),
        "classification": integration.get("classification"),
        "continuation_ref": integration.get("continuation_ref"),
    }
    if integration.get("status") != "ok":
        errors.extend(
            f"completed slice is not durably integrated: {error}"
            for error in integration.get("errors", [])
        )

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify AutoKeel ship invariants.")
    parser.add_argument("slice_id")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_ship_invariants(Path(args.root).resolve(), args.slice_id)
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

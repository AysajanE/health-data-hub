#!/usr/bin/env python3
"""Validate an AutoKeel SWR review bundle before next-stage continuation."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any


REQUIRED_REVIEWERS = {"operator_codex", "codex_review_agent", "claude_review_agent"}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def repo_path(root: Path, value: str) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return path


def parse_reviewer_notes_records(root: Path, bundle: dict[str, Any]) -> dict[str, str]:
    notes_rel = str(bundle.get("reviewer_notes") or "")
    notes_path = repo_path(root, notes_rel)
    if notes_path is None or not notes_path.exists():
        return {}
    records: dict[str, str] = {}
    for line in notes_path.read_text(encoding="utf-8").splitlines():
        match = re.match(r"-\s+([A-Za-z0-9_]+):\s+`([^`]+)`", line.strip())
        if match:
            records[match.group(1)] = match.group(2)
    return records


def decision_record_errors(
    root: Path,
    path_value: str,
    *,
    label: str,
    expected_actor: str,
    expected_review_kind: str,
    allowed_approvals: set[str],
    bundle: dict[str, Any],
    required_next_action: str | None = None,
) -> list[str]:
    path = repo_path(root, path_value)
    if path is None:
        return [f"{label} path is missing or outside repo: {path_value}"]
    decision = load_json(path, {})
    if not isinstance(decision, dict):
        return [f"{label} is not a JSON object: {path_value}"]

    errors: list[str] = []
    if decision.get("schema_version") != "responses_runner_v2.review_decision.v1":
        errors.append(f"{label} has unexpected schema_version")
    if decision.get("actor_role") != expected_actor:
        errors.append(f"{label} actor_role must be {expected_actor}")
    if decision.get("review_kind") != expected_review_kind:
        errors.append(f"{label} review_kind must be {expected_review_kind}")
    if decision.get("status") != "succeeded":
        errors.append(f"{label} status must be succeeded")
    approval = str(decision.get("approval_decision") or "")
    if approval not in allowed_approvals:
        errors.append(f"{label} approval_decision must be one of {sorted(allowed_approvals)}")
    if required_next_action is not None and decision.get("next_action") != required_next_action:
        errors.append(f"{label} next_action must be {required_next_action}")
    if decision.get("validation_errors"):
        errors.append(f"{label} contains validation_errors")
    if decision.get("blocking_issues"):
        errors.append(f"{label} contains blocking_issues")

    run_id = str(bundle.get("run_id") or bundle.get("source_run_id") or "")
    stage_id = str(bundle.get("stage_id") or bundle.get("source_stage_id") or "")
    if run_id and decision.get("run_id") not in {run_id, None, ""}:
        errors.append(f"{label} run_id does not match bundle run_id")
    if stage_id and decision.get("stage_id") not in {stage_id, None, ""}:
        errors.append(f"{label} stage_id does not match bundle stage_id")
    return errors


def review_decision_record_errors(root: Path, path: Path, bundle: dict[str, Any]) -> list[str]:
    structured = bundle.get("review_decision_records")
    structured = structured if isinstance(structured, dict) else {}
    parsed = parse_reviewer_notes_records(root, bundle)

    def record_for(key: str) -> str:
        value = structured.get(key)
        if isinstance(value, str) and value:
            return value
        parsed_value = parsed.get(key)
        if isinstance(parsed_value, str) and parsed_value:
            return parsed_value
        if key == "operator_acceptance":
            acceptance = bundle.get("acceptance_record")
            if isinstance(acceptance, str) and acceptance:
                return acceptance
        if key == "consolidated_review":
            candidate = path.parent / "consolidated_review.json"
            try:
                return str(candidate.resolve().relative_to(root)) if candidate.exists() else ""
            except ValueError:
                return ""
        return ""

    checks = [
        ("operator provisional review", "operator_review", "operator_codex", "stage_output", {"approve", "approve_with_conditions"}, None),
        ("Codex reviewer decision", "codex_review", "codex_review_agent", "stage_output", {"approve", "approve_with_conditions"}, None),
        ("Claude reviewer decision", "claude_review", "claude_review_agent", "stage_output", {"approve", "approve_with_conditions"}, None),
        ("consolidated review", "consolidated_review", "consolidation_pass", "consolidation", {"approve", "approve_with_conditions"}, "proceed_to_operator_acceptance"),
        ("operator acceptance", "operator_acceptance", "operator_codex", "operator_acceptance", {"approve"}, "create_review_bundle"),
    ]
    errors: list[str] = []
    for label, key, actor, kind, approvals, next_action in checks:
        record = record_for(key)
        if not record:
            errors.append(f"{label} record is missing")
            continue
        errors.extend(
            decision_record_errors(
                root,
                record,
                label=label,
                expected_actor=actor,
                expected_review_kind=kind,
                allowed_approvals=approvals,
                bundle=bundle,
                required_next_action=next_action,
            )
        )
    return errors


def validate_swr_review_bundle(path: Path, root: Path | None = None) -> dict[str, Any]:
    root = (root or Path.cwd()).resolve()
    errors: list[str] = []
    bundle = load_json(path, {})
    if not isinstance(bundle, dict):
        return {"status": "error", "errors": [f"bundle is not a JSON object: {path}"], "warnings": []}

    required = [
        "slice",
        "run_id",
        "stage_id",
        "response_id",
        "input_sha256",
        "output_sha256",
        "reviewer_results",
        "consolidated_verdict",
        "accepted_at",
    ]
    for key in required:
        if not bundle.get(key):
            errors.append(f"missing required field: {key}")

    reviewers = bundle.get("reviewer_results")
    seen_reviewers: set[str] = set()
    if not isinstance(reviewers, list):
        errors.append("reviewer_results must be a list")
    else:
        for index, item in enumerate(reviewers, start=1):
            if not isinstance(item, dict):
                errors.append(f"reviewer_results[{index}] must be an object")
                continue
            reviewer = str(item.get("reviewer") or "")
            verdict = str(item.get("verdict") or "")
            seen_reviewers.add(reviewer)
            if reviewer not in REQUIRED_REVIEWERS:
                errors.append(f"unknown reviewer: {reviewer}")
            if verdict != "pass":
                errors.append(f"reviewer {reviewer or index} verdict must be pass")
    missing_reviewers = sorted(REQUIRED_REVIEWERS - seen_reviewers)
    if missing_reviewers:
        errors.append("missing reviewer_results for: " + ", ".join(missing_reviewers))

    if bundle.get("consolidated_verdict") != "pass":
        errors.append("consolidated_verdict must be pass")

    input_rel = str(bundle.get("input_artifact") or bundle.get("input_manifest_json") or "")
    output_rel = str(bundle.get("response_artifact_json") or bundle.get("output_artifact") or "")
    hash_checks = [
        ("input_sha256", input_rel),
        ("output_sha256", output_rel),
    ]
    for key, rel in hash_checks:
        if not rel:
            errors.append(f"missing artifact path for {key}")
            continue
        artifact = repo_path(root, rel)
        if artifact is None or not artifact.exists():
            errors.append(f"artifact path for {key} does not exist under repo: {rel}")
            continue
        expected = str(bundle.get(key) or "")
        if expected and expected != file_sha256(artifact):
            errors.append(f"{key} does not match artifact: {rel}")

    errors.extend(review_decision_record_errors(root, path, bundle))

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": []}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an SWR review bundle.")
    parser.add_argument("bundle")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = validate_swr_review_bundle(Path(args.bundle), root=Path(args.root).resolve())
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

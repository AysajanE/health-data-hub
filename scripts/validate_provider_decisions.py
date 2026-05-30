#!/usr/bin/env python3
"""Validate tracked provider evidence decisions before PO, ship, or readiness gates."""

from __future__ import annotations

import argparse
import hashlib
import json
import stat
import sys
from pathlib import Path
from typing import Any


VALID_PROVIDERS = {"oura", "pyeight", "8sleep", "eight_sleep"}
VALID_STATUSES = {"ok", "blocked_external", "fallback_accepted", "error"}
REQUIRED_FIELDS = {
    "schema_version",
    "created_at",
    "slice",
    "provider",
    "status",
    "evidence_status",
    "evidence_path",
    "fallback_active",
    "supersedes",
    "sanitized",
    "raw_payload_tracked",
    "secret_values_tracked",
}


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


def decision_paths(root: Path, slice_id: str) -> list[Path]:
    decisions = root / "ops/autonomy/decisions"
    if not decisions.exists():
        return []
    paths: list[Path] = []
    for path in sorted(decisions.glob("*.json")):
        payload = load_json(path, {})
        if not isinstance(payload, dict):
            continue
        provider = str(payload.get("provider") or payload.get("service") or "").lower()
        name = path.name.lower()
        explicit_provider_decision = provider in VALID_PROVIDERS or any(token in name for token in ("pyeight", "oura", "8sleep", "eight_sleep"))
        if payload.get("slice") == slice_id and (
            payload.get("schema_version") == "autokeel.provider_evidence_decision.v1" or explicit_provider_decision
        ):
            paths.append(path)
    return paths


def contained_repo_path(root: Path, rel: str) -> Path | None:
    if not rel or Path(rel).is_absolute() or ".." in Path(rel).parts:
        return None
    candidate = (root / rel).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def validate_decision(root: Path, path: Path) -> list[str]:
    rel = str(path.relative_to(root))
    errors: list[str] = []
    payload = load_json(path, {})
    if not isinstance(payload, dict):
        return [f"{rel}: decision is not a JSON object"]

    missing = sorted(field for field in REQUIRED_FIELDS if field not in payload)
    errors.extend(f"{rel}: missing required field: {field}" for field in missing)

    if payload.get("schema_version") != "autokeel.provider_evidence_decision.v1":
        errors.append(f"{rel}: invalid schema_version")
    if str(payload.get("provider") or "").lower() not in VALID_PROVIDERS:
        errors.append(f"{rel}: invalid provider")
    if str(payload.get("status") or "").lower() not in VALID_STATUSES:
        errors.append(f"{rel}: invalid status")
    if str(payload.get("evidence_status") or "").lower() not in VALID_STATUSES:
        errors.append(f"{rel}: invalid evidence_status")
    if not isinstance(payload.get("fallback_active"), bool):
        errors.append(f"{rel}: fallback_active must be boolean")
    if not isinstance(payload.get("supersedes"), list):
        errors.append(f"{rel}: supersedes must be a list")
    if payload.get("sanitized") is not True:
        errors.append(f"{rel}: sanitized must be true")
    if payload.get("raw_payload_tracked") is not False:
        errors.append(f"{rel}: raw_payload_tracked must be false")
    if payload.get("secret_values_tracked") is not False:
        errors.append(f"{rel}: secret_values_tracked must be false")

    evidence_rel = str(payload.get("evidence_path") or "")
    evidence_path = contained_repo_path(root, evidence_rel)
    if evidence_path is None:
        errors.append(f"{rel}: evidence_path must be repo-relative and contained")
    elif evidence_path.exists():
        if evidence_rel.startswith("private/evidence/"):
            expected_hash = str(payload.get("private_evidence_sha256") or "")
            expected_size = payload.get("private_evidence_size_bytes")
            expected_mode = str(payload.get("private_evidence_mode") or "")
            if not expected_hash:
                errors.append(f"{rel}: private evidence requires private_evidence_sha256")
            elif file_sha256(evidence_path) != expected_hash:
                errors.append(f"{rel}: private_evidence_sha256 does not match evidence_path")
            if expected_size != evidence_path.stat().st_size:
                errors.append(f"{rel}: private_evidence_size_bytes does not match evidence_path")
            actual_mode = oct(stat.S_IMODE(evidence_path.stat().st_mode))
            if expected_mode != actual_mode:
                errors.append(f"{rel}: private_evidence_mode does not match evidence_path")
        elif payload.get("evidence_sha256") and file_sha256(evidence_path) != payload.get("evidence_sha256"):
            errors.append(f"{rel}: evidence_sha256 does not match evidence_path")
    elif evidence_rel.startswith("private/evidence/"):
        if not payload.get("private_evidence_sha256"):
            errors.append(f"{rel}: missing private evidence file also lacks private_evidence_sha256")
        if payload.get("private_evidence_size_bytes") is None:
            errors.append(f"{rel}: missing private evidence file also lacks private_evidence_size_bytes")
        if not payload.get("private_evidence_mode"):
            errors.append(f"{rel}: missing private evidence file also lacks private_evidence_mode")
    else:
        errors.append(f"{rel}: evidence_path missing: {evidence_rel}")

    superseded_by = payload.get("superseded_by")
    if superseded_by is not None:
        superseded_path = contained_repo_path(root, str(superseded_by))
        if superseded_path is None or not superseded_path.exists():
            errors.append(f"{rel}: superseded_by path is missing or invalid")
    for supersedes in payload.get("supersedes") or []:
        supersedes_path = contained_repo_path(root, str(supersedes))
        if supersedes_path is None or not supersedes_path.exists():
            errors.append(f"{rel}: supersedes path is missing or invalid: {supersedes}")
    return errors


def validate_state_machine(root: Path, paths: list[Path]) -> list[str]:
    errors: list[str] = []
    pyeight: list[tuple[Path, dict[str, Any]]] = []
    for path in paths:
        payload = load_json(path, {})
        if isinstance(payload, dict) and str(payload.get("provider") or "").lower() in {"pyeight", "8sleep", "eight_sleep"}:
            pyeight.append((path, payload))

    active_positive = [
        (path, payload)
        for path, payload in pyeight
        if payload.get("status") == "ok" and payload.get("fallback_active") is False and not payload.get("superseded_by")
    ]
    active_fallback = [
        (path, payload)
        for path, payload in pyeight
        if payload.get("status") == "fallback_accepted" and payload.get("fallback_active") is True and not payload.get("superseded_by")
    ]
    if active_positive and active_fallback:
        for fallback_path, fallback_payload in active_fallback:
            supersedes = set(str(item) for item in (fallback_payload.get("supersedes") or []))
            for positive_path, _positive_payload in active_positive:
                rel = str(positive_path.relative_to(root))
                if rel not in supersedes:
                    errors.append(
                        f"{fallback_path.relative_to(root)}: active fallback must supersede active positive pyeight decision {rel}"
                    )
        for positive_path, positive_payload in active_positive:
            expected = {str(path.relative_to(root)) for path, _payload in active_fallback}
            if str(positive_payload.get("superseded_by") or "") not in expected:
                errors.append(
                    f"{positive_path.relative_to(root)}: active positive pyeight decision must be superseded_by active fallback"
                )
    return errors


def validate_provider_decisions(root: Path, slice_id: str) -> dict[str, Any]:
    paths = decision_paths(root, slice_id)
    errors: list[str] = []
    for path in paths:
        errors.extend(validate_decision(root, path))
    errors.extend(validate_state_machine(root, paths))
    return {
        "status": "ok" if not errors else "error",
        "errors": errors,
        "warnings": [],
        "slice": slice_id,
        "decision_count": len(paths),
        "decisions": [str(path.relative_to(root)) for path in paths],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate provider evidence decisions.")
    parser.add_argument("slice_id")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = validate_provider_decisions(Path(args.root).resolve(), args.slice_id)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

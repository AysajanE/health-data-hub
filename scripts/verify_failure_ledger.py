#!/usr/bin/env python3
"""Verify AutoKeel failure-ledger safety semantics."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


CRITICAL_CLASSES = {"state_divergence", "provider_auth_failure", "review_artifact_invalid"}
HISTORICAL_V2_REQUIRED_FIELDS = {
    "root_cause_id",
    "failure_origin",
    "supersedes",
    "superseded_by",
    "false_positive",
    "closure_validation_command",
}
NEW_V2_REQUIRED_FIELDS = {
    "failure_id",
    "ts",
    "slice",
    "run_id",
    "failure_class",
    "severity",
    "description",
    "action_taken",
    "evidence_path",
    "evidence_sha256",
    "root_cause_id",
    "failure_origin",
    "supersedes",
    "superseded_by",
    "false_positive",
    "closure_validation_command",
    "open",
}


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_regular_evidence_path(root: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value:
        return None
    relative = Path(value)
    if relative.is_absolute() or ".." in relative.parts or relative.parts[0] == ".git":
        return None
    lexical = root / relative
    if lexical.is_symlink() or not lexical.is_file():
        return None
    resolved = lexical.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return None
    return resolved


def iter_jsonl(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def _load_slices(root: Path) -> list[dict[str, Any]]:
    path = root / "ops/autonomy/slices.json"
    if not path.exists():
        return []
    payload = json.loads(path.read_text(encoding="utf-8") or "[]")
    return [item for item in payload if isinstance(item, dict)] if isinstance(payload, list) else []


def is_scoped_external_blocker(
    root: Path,
    row: dict[str, Any],
    *,
    slices: list[dict[str, Any]] | None = None,
) -> bool:
    """Return true only for the narrow S12 provider-authority stop.

    This failure must block S12 and its dependants without deadlocking the
    independent predecessor recovery slice S11.  It is deliberately not a
    general exemption for external or high-severity failures.
    """

    if not (
        row.get("open") is True
        and row.get("severity") == "high"
        and row.get("slice") == "S12"
        and row.get("failure_class") == "provider_terms_conflict"
        and row.get("failure_origin") == "external_provider"
        and row.get("schema_version") == "autokeel.failure_ledger.v2"
        and isinstance(row.get("failure_id"), str)
        and bool(row.get("failure_id"))
        and isinstance(row.get("root_cause_id"), str)
        and bool(row.get("root_cause_id"))
    ):
        return False
    slice_rows = slices if slices is not None else _load_slices(root)
    matches = [item for item in slice_rows if item.get("id") == "S12"]
    if len(matches) != 1 or matches[0].get("status") != "blocked_external":
        return False
    evidence = row.get("evidence_path")
    failure_path = matches[0].get("failure_path")
    if not isinstance(evidence, str) or not evidence or evidence != failure_path:
        return False
    candidate = _safe_regular_evidence_path(root, evidence)
    evidence_sha = row.get("evidence_sha256")
    return (
        candidate is not None
        and isinstance(evidence_sha, str)
        and len(evidence_sha) == 64
        and evidence_sha == evidence_sha.lower()
        and _sha256_file(candidate) == evidence_sha
    )


def validate_v2_failure_row(
    root: Path,
    row: dict[str, Any],
    *,
    row_label: str = "row",
    require_evidence: bool = False,
    strict_new: bool = False,
) -> list[str]:
    errors: list[str] = []
    required = NEW_V2_REQUIRED_FIELDS if strict_new else HISTORICAL_V2_REQUIRED_FIELDS
    missing = sorted(required - set(row))
    if missing:
        errors.append(f"v2 ledger {row_label} missing fields: {', '.join(missing)}")
        return errors
    for key in ("failure_id", "ts", "slice", "failure_class", "description", "action_taken", "root_cause_id", "failure_origin"):
        if (strict_new or key in row) and (not isinstance(row.get(key), str) or not str(row.get(key)).strip()):
            errors.append(f"v2 ledger {row_label} requires non-empty string {key}")
    if (strict_new or "severity" in row) and row.get("severity") not in {"low", "medium", "high", "critical"}:
        errors.append(f"v2 ledger {row_label} has invalid severity")
    if (strict_new or "open" in row) and type(row.get("open")) is not bool:
        errors.append(f"v2 ledger {row_label} requires boolean open")
    if type(row.get("false_positive")) is not bool:
        errors.append(f"v2 ledger {row_label} requires boolean false_positive")
    if not isinstance(row.get("supersedes"), list) or not all(isinstance(item, str) and item for item in row.get("supersedes", [])):
        errors.append(f"v2 ledger {row_label} requires a string-list supersedes")
    if row.get("superseded_by") is not None and not isinstance(row.get("superseded_by"), str):
        errors.append(f"v2 ledger {row_label} has invalid superseded_by")
    if not isinstance(row.get("closure_validation_command"), str):
        errors.append(f"v2 ledger {row_label} requires string closure_validation_command")
    evidence = row.get("evidence_path")
    if (strict_new or "evidence_path" in row) and (not isinstance(evidence, str) or not evidence):
        errors.append(f"v2 ledger {row_label} requires evidence_path")
    evidence_path = _safe_regular_evidence_path(root, evidence) if isinstance(evidence, str) else None
    if require_evidence and evidence_path is None:
        errors.append(f"v2 ledger {row_label} evidence_path is missing or outside repo: {evidence}")
    evidence_sha = row.get("evidence_sha256")
    if evidence_sha is not None and (
        not isinstance(evidence_sha, str)
        or len(evidence_sha) != 64
        or evidence_sha != evidence_sha.lower()
        or any(char not in "0123456789abcdef" for char in evidence_sha)
    ):
        errors.append(f"v2 ledger {row_label} has invalid evidence_sha256")
    elif require_evidence and not isinstance(evidence_sha, str):
        errors.append(f"v2 ledger {row_label} requires evidence_sha256")
    elif require_evidence and evidence_path is not None and _sha256_file(evidence_path) != evidence_sha:
        errors.append(f"v2 ledger {row_label} evidence_sha256 mismatch")
    return errors


def verify_failure_ledger(root: Path, slice_id: str | None = None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    rows = list(iter_jsonl(root / "ops/autonomy/failure_ledger.jsonl") or [])
    if slice_id:
        rows = [row for row in rows if row.get("slice") == slice_id]

    class_counts: Counter[str] = Counter()
    slices = _load_slices(root)
    scoped_external_blockers = 0
    for index, row in enumerate(rows, start=1):
        failure_class = str(row.get("failure_class") or "unclassified")
        class_counts[failure_class] += 1
        open_ = bool(row.get("open", True))
        severity = str(row.get("severity") or "")
        scoped_external = is_scoped_external_blocker(root, row, slices=slices)
        if scoped_external:
            scoped_external_blockers += 1
            warnings.append(f"scoped external authority blocker remains open: row {index} {failure_class}")
        if open_ and severity in {"high", "critical"} and not scoped_external:
            errors.append(f"open high/critical failure: row {index} {failure_class}")
        if open_ and failure_class in CRITICAL_CLASSES:
            errors.append(f"{failure_class} is open")
        if not open_:
            evidence = str(row.get("closure_evidence") or "")
            if not evidence:
                errors.append(f"closed failure lacks closure_evidence: row {index} {failure_class}")
            elif not (root / evidence).exists():
                errors.append(f"closure_evidence path missing: row {index} {evidence}")
        if row.get("schema_version") == "autokeel.failure_ledger.v2":
            errors.extend(validate_v2_failure_row(root, row, row_label=f"row {index}"))
        else:
            warnings.append(f"legacy ledger row without v2 semantics: row {index} {failure_class}")
        closure_note = str(row.get("closure_note") or "").lower()
        if "false" in closure_note and "wrapper" in closure_note and row.get("false_positive") is not True:
            warnings.append(f"legacy wrapper false-positive row lacks false_positive=true: row {index} {failure_class}")
        if class_counts[failure_class] > 2 and not row.get("root_cause_id"):
            if row.get("schema_version") == "autokeel.failure_ledger.v2":
                errors.append(f"same failure class repeats more than twice without root_cause_id: {failure_class}")
            else:
                warnings.append(f"legacy repeated failure class lacks root_cause_id: {failure_class}")

    return {
        "status": "ok" if not errors else "error",
        "errors": errors,
        "warnings": warnings,
        "rows": len(rows),
        "scoped_external_blockers": scoped_external_blockers,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify AutoKeel failure ledger.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--slice")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_failure_ledger(Path(args.root).resolve(), slice_id=args.slice)
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

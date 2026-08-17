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


def effective_failure_rows(
    root: Path,
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[str]]:
    """Resolve append-only closed successors without rewriting history.

    A later closed v2 row may retire one earlier row through ``supersedes``.
    Invalid or ambiguous links fail closed: the earlier row remains effective
    and the returned errors explain why it was not retired.
    """

    errors: list[str] = []
    by_id: dict[str, tuple[int, dict[str, Any]]] = {}
    duplicate_ids: set[str] = set()
    for index, row in enumerate(rows):
        failure_id = row.get("failure_id")
        if not isinstance(failure_id, str) or not failure_id:
            continue
        if failure_id in by_id:
            duplicate_ids.add(failure_id)
        else:
            by_id[failure_id] = (index, row)

    retired: set[str] = set()
    claimed: dict[str, str] = {}
    ambiguous_targets: set[str] = set()
    for index, successor in enumerate(rows):
        successor_id = successor.get("failure_id")
        targets = successor.get("supersedes")
        if not isinstance(targets, list) or not targets:
            continue
        if not isinstance(successor_id, str) or not successor_id:
            errors.append(f"row {index + 1} successor requires a non-empty failure_id")
            continue
        if successor_id in duplicate_ids:
            errors.append(f"row {index + 1} successor has duplicate failure_id {successor_id}")
            continue
        if len(targets) != 1:
            errors.append(f"row {index + 1} successor {successor_id} must supersede exactly one failure")
            continue
        successor_errors = validate_v2_failure_row(
            root,
            successor,
            row_label=f"row {index + 1} successor",
            require_evidence=True,
            strict_new=True,
        )
        if successor_errors:
            errors.extend(successor_errors)
            continue
        for target_id in targets:
            label = f"row {index + 1} successor {successor_id}"
            if not isinstance(target_id, str) or not target_id:
                errors.append(f"{label} has invalid supersedes entry")
                continue
            if target_id in duplicate_ids:
                errors.append(f"{label} targets duplicate failure_id {target_id}")
                continue
            target_entry = by_id.get(target_id)
            if target_entry is None:
                errors.append(f"{label} targets missing failure_id {target_id}")
                continue
            target_index, target = target_entry
            if target_index >= index:
                errors.append(f"{label} must follow the failure it supersedes")
                continue
            if target_id in ambiguous_targets:
                errors.append(f"failure_id {target_id} has multiple successors")
                continue
            if target_id in claimed:
                errors.append(f"failure_id {target_id} has multiple successors")
                ambiguous_targets.add(target_id)
                retired.discard(target_id)
                continue
            target_errors = validate_v2_failure_row(
                root,
                target,
                row_label=f"superseded failure {target_id}",
                require_evidence=True,
                strict_new=True,
            )
            if target_errors:
                errors.extend(target_errors)
                continue
            if successor.get("open") is not False:
                errors.append(f"{label} must be closed")
                continue
            for key in ("slice", "failure_class", "root_cause_id"):
                if successor.get(key) != target.get(key):
                    errors.append(f"{label} {key} does not match {target_id}")
            closure_evidence = successor.get("closure_evidence")
            closure_note = successor.get("closure_note")
            evidence_path = _safe_regular_evidence_path(root, closure_evidence)
            if evidence_path is None or not isinstance(closure_note, str) or not closure_note.strip():
                errors.append(f"{label} requires existing closure_evidence and non-empty closure_note")
            if closure_evidence != successor.get("evidence_path"):
                errors.append(f"{label} closure_evidence must equal its hash-bound evidence_path")
            if errors and any(error.startswith(label) for error in errors):
                continue
            claimed[target_id] = successor_id
            retired.add(target_id)

    effective = [
        row
        for row in rows
        if not (isinstance(row.get("failure_id"), str) and row.get("failure_id") in retired)
    ]
    return effective, errors


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
    all_rows = list(iter_jsonl(root / "ops/autonomy/failure_ledger.jsonl") or [])
    effective_rows, successor_errors = effective_failure_rows(root, all_rows)
    errors.extend(successor_errors)
    rows = all_rows
    if slice_id:
        rows = [row for row in rows if row.get("slice") == slice_id]
        effective_rows = [row for row in effective_rows if row.get("slice") == slice_id]

    class_counts: Counter[str] = Counter()
    for index, row in enumerate(rows, start=1):
        failure_class = str(row.get("failure_class") or "unclassified")
        class_counts[failure_class] += 1
        open_ = bool(row.get("open", True))
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

    for index, row in enumerate(effective_rows, start=1):
        failure_class = str(row.get("failure_class") or "unclassified")
        open_ = bool(row.get("open", True))
        severity = str(row.get("severity") or "")
        if open_ and severity in {"high", "critical"}:
            errors.append(f"open high/critical failure: effective row {index} {failure_class}")
        if open_ and failure_class in CRITICAL_CLASSES:
            errors.append(f"{failure_class} is open")

    return {
        "status": "ok" if not errors else "error",
        "errors": errors,
        "warnings": warnings,
        "rows": len(all_rows),
        "effective_rows": len(effective_rows),
        "scoped_external_blockers": 0,
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

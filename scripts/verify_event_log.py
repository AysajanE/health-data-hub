#!/usr/bin/env python3
"""Verify append-only AutoKeel event identity and legacy reconciliation.

Historical event rows are never rewritten. Any legacy duplicate event id must
instead be named by an exact raw-row SHA-256 receipt. New duplicates or
non-monotonic ids fail closed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any


DEFAULT_RECEIPT = "docs/evidence/event-log-legacy-id-reconciliation-20260816.json"
RECEIPT_SCHEMA_VERSION = "autokeel.event_log_reconciliation.v2"
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else default


def raw_row_sha256(raw_line: str) -> str:
    return hashlib.sha256(raw_line.encode("utf-8")).hexdigest()


def _is_positive_json_integer(value: Any) -> bool:
    """Return true only for a positive JSON integer, never a JSON boolean."""

    return type(value) is int and value >= 1


def load_events(path: Path) -> tuple[list[dict[str, Any]], list[str]]:
    rows: list[dict[str, Any]] = []
    errors: list[str] = []
    if not path.exists():
        return rows, ["events.jsonl is missing"]
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
        if not raw.strip():
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            errors.append(f"event row {line_number} is invalid JSON: {exc}")
            continue
        if not isinstance(payload, dict):
            errors.append(f"event row {line_number} is not an object")
            continue
        event_id = payload.get("event_id")
        if not _is_positive_json_integer(event_id):
            errors.append(f"event row {line_number} has invalid event_id: {event_id!r}")
            continue
        rows.append(
            {
                "line_number": line_number,
                "event_id": event_id,
                "event": payload.get("event"),
                "slice": payload.get("slice"),
                "ts": payload.get("ts"),
                "row_sha256": raw_row_sha256(raw),
            }
        )
    return rows, errors


def _receipt_duplicates(payload: Any) -> tuple[dict[int, list[tuple[int, str]]], list[str], int]:
    errors: list[str] = []
    expected: dict[int, list[tuple[int, str]]] = {}
    through = 0
    if not isinstance(payload, dict):
        return expected, ["event reconciliation receipt must contain an object"], through
    if payload.get("schema_version") != RECEIPT_SCHEMA_VERSION:
        errors.append("event reconciliation receipt has wrong schema_version")
    through_raw = payload.get("reconciled_through_event_id")
    if not _is_positive_json_integer(through_raw):
        errors.append("event reconciliation receipt needs positive reconciled_through_event_id")
    else:
        through = through_raw
    duplicates = payload.get("duplicate_event_ids")
    if not isinstance(duplicates, list):
        return expected, errors + ["event reconciliation receipt needs duplicate_event_ids list"], through
    for index, item in enumerate(duplicates, start=1):
        if not isinstance(item, dict):
            errors.append(f"duplicate receipt row {index} is not an object")
            continue
        event_id = item.get("event_id")
        occurrences = item.get("occurrences")
        if not _is_positive_json_integer(event_id):
            errors.append(f"duplicate receipt row {index} has invalid event_id")
            continue
        if event_id > through:
            errors.append(f"duplicate receipt id {event_id} exceeds reconciliation boundary {through}")
        if not isinstance(occurrences, list) or len(occurrences) < 2:
            errors.append(f"duplicate receipt id {event_id} needs at least two physical occurrences")
            continue
        parsed_occurrences: list[tuple[int, str]] = []
        malformed = False
        for occurrence_index, occurrence in enumerate(occurrences, start=1):
            if not isinstance(occurrence, dict):
                errors.append(
                    f"duplicate receipt id {event_id} occurrence {occurrence_index} is not an object"
                )
                malformed = True
                continue
            line_number = occurrence.get("line_number")
            row_sha256 = occurrence.get("row_sha256")
            if not _is_positive_json_integer(line_number):
                errors.append(
                    f"duplicate receipt id {event_id} occurrence {occurrence_index} "
                    "has invalid line_number"
                )
                malformed = True
                continue
            if not isinstance(row_sha256, str) or SHA256_PATTERN.fullmatch(row_sha256) is None:
                errors.append(
                    f"duplicate receipt id {event_id} occurrence {occurrence_index} "
                    "has invalid SHA-256 row hash"
                )
                malformed = True
                continue
            parsed_occurrences.append((line_number, row_sha256))
        if malformed:
            continue
        line_numbers = [line_number for line_number, _ in parsed_occurrences]
        row_hashes = [row_hash for _, row_hash in parsed_occurrences]
        if line_numbers != sorted(line_numbers) or len(set(line_numbers)) != len(line_numbers):
            errors.append(
                f"duplicate receipt id {event_id} occurrences must use unique ascending physical lines"
            )
            continue
        if len(set(row_hashes)) != len(row_hashes):
            errors.append(f"duplicate receipt id {event_id} needs unique SHA-256 row hashes")
            continue
        if event_id in expected:
            errors.append(f"duplicate receipt repeats event_id {event_id}")
            continue
        expected[event_id] = parsed_occurrences
    return expected, errors, through


def verify_event_log(root: Path, receipt_rel: str = DEFAULT_RECEIPT) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    rows, row_errors = load_events(root / "ops/autonomy/events.jsonl")
    errors.extend(row_errors)

    by_id: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_id[row["event_id"]].append(row)
    actual_duplicates = {event_id: values for event_id, values in by_id.items() if len(values) > 1}

    receipt_path = root / receipt_rel
    if Path(receipt_rel).is_absolute() or ".." in Path(receipt_rel).parts:
        errors.append(f"event reconciliation receipt path must be repo-relative: {receipt_rel}")
        receipt_payload: Any = None
    elif not receipt_path.is_file():
        if actual_duplicates:
            errors.append(f"event reconciliation receipt missing: {receipt_rel}")
        receipt_payload = None
    else:
        try:
            receipt_payload = load_json(receipt_path, None)
        except json.JSONDecodeError as exc:
            errors.append(f"event reconciliation receipt is invalid JSON: {exc}")
            receipt_payload = None

    if receipt_payload is None and not actual_duplicates:
        expected, receipt_errors, through = {}, [], 0
    else:
        expected, receipt_errors, through = _receipt_duplicates(receipt_payload)
        errors.extend(receipt_errors)

    for event_id, occurrences in sorted(actual_duplicates.items()):
        actual_occurrences = [
            (int(row["line_number"]), str(row["row_sha256"])) for row in occurrences
        ]
        if event_id not in expected:
            errors.append(f"unreconciled duplicate event_id: {event_id}")
        elif expected[event_id] != actual_occurrences:
            errors.append(
                f"duplicate event_id {event_id} does not match its exact physical-line receipt"
            )
    for event_id in sorted(set(expected) - set(actual_duplicates)):
        errors.append(f"receipt names event_id {event_id}, but it is not duplicated in events.jsonl")

    prior = 0
    for row in rows:
        event_id = int(row["event_id"])
        if event_id <= prior and not (event_id <= through and event_id in expected):
            errors.append(
                f"event ids are not strictly increasing at physical line {row['line_number']}: "
                f"{event_id} follows {prior}"
            )
        prior = max(prior, event_id)

    state = load_json(root / "ops/autonomy/autonomy_state.json", {})
    max_event_id = max((int(row["event_id"]) for row in rows), default=0)
    state_last = state.get("last_event_id") if isinstance(state, dict) else None
    if type(state_last) is not int or state_last != max_event_id:
        errors.append(f"autonomy_state last_event_id {state_last!r} != event-log max {max_event_id}")

    if actual_duplicates and not errors:
        warnings.append(
            "legacy duplicate event ids are preserved and hash-reconciled: "
            + ", ".join(str(value) for value in sorted(actual_duplicates))
        )

    return {
        "status": "ok" if not errors else "error",
        "errors": errors,
        "warnings": warnings,
        "checks": {
            "event_rows": len(rows),
            "unique_event_ids": len(by_id),
            "max_event_id": max_event_id,
            "reconciled_duplicate_ids": sorted(actual_duplicates),
            "reconciliation_receipt": receipt_rel,
            "reconciled_through_event_id": through,
        },
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify AutoKeel event-log identity and reconciliation.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--receipt", default=DEFAULT_RECEIPT)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = verify_event_log(Path(args.root), args.receipt)
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

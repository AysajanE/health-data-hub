#!/usr/bin/env python3
"""Close an AutoKeel failure ledger entry with local evidence."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


SECRET_RE = re.compile(
    r"(?i)(access_token|refresh_token|mood_token|x-mood-token|client_secret|password|authorization|api[_-]?key)"
    r"([\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]{8,})"
)


def redact_text(text: str) -> str:
    return SECRET_RE.sub(r"\1\2[REDACTED]", text)


def iter_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


def write_jsonl_atomic(path: Path, rows: list[dict[str, Any]]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")
    os.replace(tmp, path)


def next_event_id(root: Path) -> int:
    state_path = root / "ops" / "autonomy" / "autonomy_state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8") or "{}")
    else:
        state = {}
    event_id = int(state.get("last_event_id") or 0) + 1
    state["last_event_id"] = event_id
    tmp = state_path.with_suffix(state_path.suffix + ".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, state_path)
    return event_id


def requeue_closed_blocked_slice(root: Path, slice_id: str, rows: list[dict[str, Any]]) -> bool:
    if any(row.get("slice") == slice_id and row.get("open", True) for row in rows):
        return False

    slices_path = root / "ops" / "autonomy" / "slices.json"
    if not slices_path.exists():
        return False

    slices = json.loads(slices_path.read_text(encoding="utf-8") or "[]")
    changed = False
    for item in slices:
        if item.get("id") != slice_id:
            continue
        if item.get("status") not in {"blocked", "blocked_compile_inputs"}:
            return False
        item["status"] = "replan_required"
        item["retry_count"] = 0
        item.pop("reason", None)
        item["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        changed = True
        break
    if not changed:
        return False

    tmp = slices_path.with_suffix(slices_path.suffix + ".tmp")
    tmp.write_text(json.dumps(slices, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, slices_path)

    state_path = root / "ops" / "autonomy" / "autonomy_state.json"
    if state_path.exists():
        state = json.loads(state_path.read_text(encoding="utf-8") or "{}")
        state["current_slice"] = slice_id
        if not isinstance(state.get("active_run"), dict):
            state["active_run"] = None
        tmp_state = state_path.with_suffix(state_path.suffix + ".tmp")
        tmp_state.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        os.replace(tmp_state, state_path)

    return True


def close_failure(
    root: Path,
    slice_id: str,
    failure_class: str,
    evidence: str,
    note: str,
) -> dict[str, Any]:
    root = root.resolve()
    evidence_path = root / evidence
    errors: list[str] = []
    if not evidence_path.exists():
        errors.append(f"closure evidence missing: {evidence}")
    if evidence_path.is_absolute() and not evidence_path.resolve().is_relative_to(root):
        errors.append(f"closure evidence must be under repo root: {evidence}")
    if errors:
        return {"status": "error", "errors": errors, "closed": 0}

    safe_note = redact_text(note)
    ledger = root / "ops" / "autonomy" / "failure_ledger.jsonl"
    rows = iter_jsonl(ledger)
    closed = 0
    for row in rows:
        if row.get("slice") != slice_id:
            continue
        if row.get("failure_class") != failure_class:
            continue
        if not row.get("open", True):
            continue
        row["open"] = False
        row["closed_at"] = __import__("datetime").datetime.now().astimezone().isoformat(timespec="seconds")
        row["closure_evidence"] = str(evidence_path.relative_to(root))
        row["closure_note"] = safe_note
        closed += 1

    if closed:
        write_jsonl_atomic(ledger, rows)
        requeued = requeue_closed_blocked_slice(root, slice_id, rows)
        event_path = root / "ops" / "autonomy" / "events.jsonl"
        event = {
            "event_id": next_event_id(root),
            "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
            "event": "failure_closed",
            "slice": slice_id,
            "details": {
                "failure_class": failure_class,
                "closed": closed,
                "closure_evidence": str(evidence_path.relative_to(root)),
                "slice_requeued": requeued,
            },
        }
        with event_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True) + "\n")
    return {"status": "ok" if closed else "error", "errors": [] if closed else ["no matching open failure"], "closed": closed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Close open AutoKeel failure records with local evidence.")
    parser.add_argument("slice_id")
    parser.add_argument("failure_class")
    parser.add_argument("--evidence", required=True, help="Repo-relative closure evidence path.")
    parser.add_argument("--note", required=True)
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = close_failure(Path(args.root).resolve(), args.slice_id, args.failure_class, args.evidence, args.note)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

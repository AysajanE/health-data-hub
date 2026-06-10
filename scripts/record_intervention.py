#!/usr/bin/env python3
"""Sanctioned out-of-band intervention recorder for AutoKeel.

AutoKeel fails closed when control-plane state (slices.json, events.jsonl,
failure_ledger.jsonl) changes outside its own writers. Operators and assisting
agents must route every manual correction through this tool so each
intervention leaves a decision artifact, an event, and a re-synced state
digest — never a silent edit.

Subcommands:
  ratify           Record a decision artifact for an intervention that already
                   happened (or is about to), append a manual_intervention event,
                   and re-sync the state digest.
  restore-events   Append archived event rows (JSONL) back into events.jsonl with
                   id-uniqueness and monotonicity validation.
  append-ledger    Append one failure-ledger row from a JSON file, verbatim,
                   with schema sanity checks.
  abandon-swr-run  Record an SWR run abandonment decision that sanctions exactly
                   one fresh SWR launch for the slice.
  sync-digest      Re-sync the state digest sidecar after a ratified edit.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from ops.autonomy.autokeel import (  # noqa: E402
    AutoKeel,
    append_jsonl,
    iter_jsonl,
    now_iso,
    now_slug,
    update_state_digest_sidecar,
    write_json_atomic,
)


def _operator(root: Path) -> AutoKeel:
    return AutoKeel(root=root.resolve(), dry_run=False)


def cmd_ratify(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    op = _operator(root)
    details: dict[str, Any] = {}
    if args.details_json:
        details = json.loads(Path(args.details_json).read_text(encoding="utf-8"))
    artifact_rel = f"docs/evidence/{args.name}-{now_slug().lower()}.json"
    payload = {
        "schema_version": "autokeel.manual_intervention.v1",
        "recorded_at": now_iso(),
        "name": args.name,
        "slice": args.slice,
        "reason": args.reason,
        "recorded_by": args.recorded_by,
        "details": details,
    }
    write_json_atomic(root / artifact_rel, payload)
    op.log_event(
        "manual_intervention_ratified",
        {"artifact": artifact_rel, "name": args.name, "reason": args.reason},
        slice_id=args.slice,
    )
    update_state_digest_sidecar(root)
    return {"status": "ok", "artifact": artifact_rel}


def cmd_restore_events(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    op = _operator(root)
    events_path = root / "ops/autonomy/events.jsonl"
    existing_ids = set()
    max_id = 0
    for event in iter_jsonl(events_path):
        try:
            event_id = int(event.get("event_id") or 0)
        except (TypeError, ValueError):
            continue
        existing_ids.add(event_id)
        max_id = max(max_id, event_id)

    rows = [json.loads(line) for line in Path(args.source).read_text(encoding="utf-8").splitlines() if line.strip()]
    errors: list[str] = []
    incoming_ids: list[int] = []
    for row in rows:
        try:
            event_id = int(row.get("event_id") or 0)
        except (TypeError, ValueError):
            errors.append(f"row without integer event_id: {str(row)[:120]}")
            continue
        if event_id in existing_ids:
            errors.append(f"event_id {event_id} already exists in events.jsonl")
        if event_id <= max_id and not args.allow_historical:
            errors.append(f"event_id {event_id} is not after current max {max_id}; pass --allow-historical only for verified gap restoration")
        incoming_ids.append(event_id)
    if sorted(incoming_ids) != incoming_ids:
        errors.append("incoming event ids are not monotonically ordered")
    if len(set(incoming_ids)) != len(incoming_ids):
        errors.append("incoming event ids contain duplicates")
    if errors:
        return {"status": "error", "errors": errors}

    for row in rows:
        append_jsonl(events_path, row)
    state = op.load_state()
    state["last_event_id"] = max(max_id, max(incoming_ids or [0]), int(state.get("last_event_id") or 0))
    op.save_state(state)
    op.log_event(
        "events_restored_by_intervention",
        {"source": args.source, "count": len(rows), "first_id": incoming_ids[0], "last_id": incoming_ids[-1]},
    )
    update_state_digest_sidecar(root)
    return {"status": "ok", "restored": len(rows)}


def cmd_append_ledger(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    op = _operator(root)
    row = json.loads(Path(args.source).read_text(encoding="utf-8"))
    if not isinstance(row, dict):
        return {"status": "error", "errors": ["ledger row must be a JSON object"]}
    missing = sorted({"slice", "failure_class", "severity", "description"} - set(row))
    if missing:
        return {"status": "error", "errors": [f"ledger row missing required fields: {', '.join(missing)}"]}
    row.setdefault("schema_version", "autokeel.failure_ledger.v2")
    row.setdefault("ts", now_iso())
    row.setdefault("open", True)
    append_jsonl(root / "ops/autonomy/failure_ledger.jsonl", row)
    op.log_event(
        "ledger_row_restored_by_intervention",
        {"source": args.source, "failure_class": row.get("failure_class"), "open": row.get("open")},
        slice_id=str(row.get("slice") or "") or None,
    )
    update_state_digest_sidecar(root)
    return {"status": "ok", "failure_class": row.get("failure_class")}


def cmd_abandon_swr_run(args: argparse.Namespace) -> dict[str, Any]:
    root = Path(args.root).resolve()
    op = _operator(root)
    artifact_rel = f"docs/evidence/{args.slice.lower()}-swr-run-abandonment-{now_slug().lower()}.json"
    payload = {
        "schema_version": "autokeel.swr_run_abandonment.v1",
        "recorded_at": now_iso(),
        "slice": args.slice,
        "abandoned_run_id": args.run_id,
        "reason": args.reason,
        "recorded_by": args.recorded_by,
        "authorizes_fresh_launch": True,
        "consumed_at": None,
    }
    write_json_atomic(root / artifact_rel, payload)
    op.log_event(
        "swr_run_abandonment_recorded",
        {"artifact": artifact_rel, "abandoned_run_id": args.run_id, "reason": args.reason},
        slice_id=args.slice,
    )
    update_state_digest_sidecar(root)
    return {"status": "ok", "artifact": artifact_rel}


def cmd_sync_digest(args: argparse.Namespace) -> dict[str, Any]:
    update_state_digest_sidecar(Path(args.root).resolve())
    return {"status": "ok"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Record a sanctioned AutoKeel intervention.")
    parser.add_argument("--root", default=".")
    sub = parser.add_subparsers(dest="command", required=True)

    ratify = sub.add_parser("ratify", help="record a decision artifact and event for an intervention")
    ratify.add_argument("--name", required=True, help="kebab-case artifact name, e.g. s05-run-reset-ratification")
    ratify.add_argument("--slice", default=None)
    ratify.add_argument("--reason", required=True)
    ratify.add_argument("--recorded-by", default="operator")
    ratify.add_argument("--details-json", default=None, help="path to a JSON file with structured details")
    ratify.set_defaults(func=cmd_ratify)

    restore = sub.add_parser("restore-events", help="append archived events with id validation")
    restore.add_argument("--source", required=True, help="JSONL file holding the events to restore")
    restore.add_argument("--allow-historical", action="store_true")
    restore.set_defaults(func=cmd_restore_events)

    ledger = sub.add_parser("append-ledger", help="append one failure-ledger row from a JSON file")
    ledger.add_argument("--source", required=True)
    ledger.set_defaults(func=cmd_append_ledger)

    abandon = sub.add_parser("abandon-swr-run", help="record an SWR run abandonment decision")
    abandon.add_argument("--slice", required=True)
    abandon.add_argument("--run-id", required=True)
    abandon.add_argument("--reason", required=True)
    abandon.add_argument("--recorded-by", default="operator")
    abandon.set_defaults(func=cmd_abandon_swr_run)

    sync = sub.add_parser("sync-digest", help="re-sync the state digest sidecar")
    sync.set_defaults(func=cmd_sync_digest)

    args = parser.parse_args(argv)
    report = args.func(args)
    print(json.dumps(report, indent=2, sort_keys=True))
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

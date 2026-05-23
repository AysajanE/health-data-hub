#!/usr/bin/env python3
"""Normalize Keel/plan-orchestrator status output into a compact digest."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


KNOWN_STATES = {"passed", "awaiting_human_gate", "blocked_external", "escalated", "running", "live", "pending", "unknown"}


def normalize_state(raw: str) -> str | None:
    text = raw.strip().lower().replace("-", "_")
    if text in KNOWN_STATES:
        return text
    for state in KNOWN_STATES:
        if state in text:
            return state
    return None


def find_state(value: Any) -> str | None:
    if isinstance(value, dict):
        for key in ("terminal_state", "state", "status", "po_state", "supervisor_state"):
            raw = value.get(key)
            if isinstance(raw, str):
                normalized = normalize_state(raw)
                if normalized:
                    return normalized
        for item in value.values():
            found = find_state(item)
            if found:
                return found
    elif isinstance(value, list):
        terminal = None
        for item in value:
            found = find_state(item)
            if found in {"awaiting_human_gate", "blocked_external", "escalated", "passed"}:
                terminal = found
        if terminal:
            return terminal
    elif isinstance(value, str):
        return normalize_state(value)
    return None


def load_status(args: argparse.Namespace) -> dict[str, Any]:
    if args.from_file:
        return json.loads(Path(args.from_file).read_text(encoding="utf-8"))
    if not args.run_id:
        raise ValueError("--run-id or --from-file is required")
    keel_run = Path(args.keel_root) / "bin" / "keel-run"
    proc = subprocess.run([str(keel_run), "status", "--run-id", args.run_id, "--format", "json"], text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        return {"run_id": args.run_id, "terminal_state": "unknown", "error": proc.stderr}
    return json.loads(proc.stdout)


def digest_status(payload: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
    return {
        "run_id": run_id or payload.get("run_id") or payload.get("id"),
        "terminal_state": find_state(payload) or "unknown",
        "raw_state": payload.get("state") or payload.get("status") or payload.get("terminal_state"),
        "items_total": len(payload.get("items", [])) if isinstance(payload.get("items"), list) else None,
        "source": "keel_status_digest",
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize Keel status JSON.")
    parser.add_argument("--run-id")
    parser.add_argument("--from-file")
    parser.add_argument("--keel-root", default="/Users/aeziz-local/keel")
    args = parser.parse_args(argv)
    try:
        payload = load_status(args)
        print(json.dumps(digest_status(payload, run_id=args.run_id), indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

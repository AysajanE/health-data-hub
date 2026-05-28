#!/usr/bin/env python3
"""Normalize Keel/plan-orchestrator status output into a safe compact digest.

Safety rule: never infer whole-run passed from one nested item. Blocking states
dominate. Passed is returned only for explicit top-level passed status or when
all discovered item states are passed.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


BLOCKING_STATES = {"awaiting_human_gate", "escalated", "blocked_external"}
LIVE_STATES = {"running", "live", "pending", "in_progress", "started"}
TERMINAL_STATES = {"passed", "awaiting_human_gate", "blocked_external", "escalated"}
KNOWN_STATES = TERMINAL_STATES | LIVE_STATES | {"unknown"}
STATE_KEYS = (
    "terminal_state",
    "run_state",
    "state",
    "status",
    "po_state",
    "supervisor_state",
    "current_state",
)


def normalize_state(raw: str | None) -> str | None:
    if not raw:
        return None
    text = str(raw).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "st130_passed": "passed",
        "st130_awaiting_human_gate": "awaiting_human_gate",
        "st130_manual_gate": "awaiting_human_gate",
        "st130_blocked_external": "blocked_external",
        "st130_escalated": "escalated",
        "st130_failed": "escalated",
        "st040_running": "running",
        "st030_running": "running",
        "st020_pending": "pending",
        "human_gate": "awaiting_human_gate",
        "awaiting_manual_gate": "awaiting_human_gate",
        "external_block": "blocked_external",
        "blocked": "blocked_external",
        "complete": "passed",
        "completed": "passed",
        "success": "passed",
        "ok": None,
    }
    if text in aliases:
        return aliases[text]
    if text in KNOWN_STATES:
        return text
    for state in KNOWN_STATES:
        if state != "passed" and state in text:
            return state
    return None


def explicit_top_state(payload: dict[str, Any]) -> str | None:
    for key in STATE_KEYS:
        state = normalize_state(payload.get(key))
        if state:
            return state
    return None


def _find_kernel_status(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        kernel = value.get("kernel_status")
        if isinstance(kernel, dict):
            return kernel
        if isinstance(value.get("terminal_counts"), dict):
            return value
        for item in value.values():
            found = _find_kernel_status(item)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_kernel_status(item)
            if found:
                return found
    return None


def terminal_from_kernel_counts(payload: dict[str, Any]) -> str | None:
    """Derive whole-run state from supervised PO kernel terminal counts."""
    kernel = _find_kernel_status(payload)
    if not kernel:
        return None

    counts = kernel.get("terminal_counts")
    normalized: dict[str, int] = {}
    unknown_total = 0
    if isinstance(counts, dict):
        for raw_state, raw_count in counts.items():
            try:
                count = int(raw_count)
            except (TypeError, ValueError):
                continue
            if count <= 0:
                continue
            state = normalize_state(str(raw_state))
            if state:
                normalized[state] = normalized.get(state, 0) + count
            else:
                unknown_total += count

    current = explicit_top_state(kernel)
    all_counted = sum(normalized.values()) + unknown_total

    if normalized.get("awaiting_human_gate", 0):
        return "awaiting_human_gate"
    if normalized.get("escalated", 0):
        return "escalated"
    if normalized.get("blocked_external", 0):
        return "blocked_external"
    if any(normalized.get(state, 0) for state in LIVE_STATES):
        return "running"

    if current in BLOCKING_STATES:
        return current
    if current in LIVE_STATES:
        return "running"

    passed_count = normalized.get("passed", 0)
    if passed_count and passed_count == all_counted:
        return "passed"
    if passed_count and passed_count < all_counted:
        return "running"
    if current == "passed" and not all_counted:
        return "passed"
    return current


def terminal_from_supervision_status(payload: dict[str, Any]) -> str | None:
    supervision = payload.get("supervision_status")
    if not isinstance(supervision, dict):
        return None
    latest = supervision.get("latest_intervention")
    latest = latest if isinstance(latest, dict) else {}
    action = str(latest.get("action_kind") or "").lower()
    result = str(latest.get("result_status") or "").lower()
    recoverability = str(latest.get("recoverability_class") or "").lower()
    claim = str(supervision.get("claim_class") or "").lower()
    try:
        exit_code = int(supervision.get("exit_code"))
    except (TypeError, ValueError):
        exit_code = 0

    if action == "park" or result == "parked" or recoverability == "non_recoverable":
        return "escalated"
    if claim == "terminal_observed" and exit_code not in {0, 12}:
        return "escalated"
    return None


def collect_states(value: Any) -> list[str]:
    states: list[str] = []
    if isinstance(value, dict):
        for key in STATE_KEYS:
            state = normalize_state(value.get(key))
            if state:
                states.append(state)
        for item in value.values():
            states.extend(collect_states(item))
    elif isinstance(value, list):
        for item in value:
            states.extend(collect_states(item))
    return states


def extract_items(payload: Any) -> list[Any]:
    if isinstance(payload, dict):
        for key in ("items", "work_items", "steps", "tasks"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
    return []


def determine_terminal_state(payload: dict[str, Any]) -> str:
    supervision = terminal_from_supervision_status(payload)
    kernel = terminal_from_kernel_counts(payload)
    top = explicit_top_state(payload)
    all_states = collect_states(payload)

    if "awaiting_human_gate" in all_states:
        return "awaiting_human_gate"
    if "escalated" in all_states:
        return "escalated"
    if "blocked_external" in all_states:
        return "blocked_external"

    if supervision:
        return supervision

    if kernel:
        return kernel

    if top == "passed":
        return "passed"

    items = extract_items(payload)
    if items:
        item_states = [explicit_top_state(item) for item in items if isinstance(item, dict)]
        item_states = [state for state in item_states if state]
        if item_states and all(state == "passed" for state in item_states):
            return "passed"
        if any(state in LIVE_STATES for state in item_states):
            return "running"
        if any(state == "unknown" for state in item_states):
            return "unknown"

    if top in LIVE_STATES:
        return "running"
    if any(state in LIVE_STATES for state in all_states):
        return "running"

    return top or "unknown"


def run_json_command(argv: list[str], *, cwd: Path | None = None) -> tuple[int, dict[str, Any] | None, str]:
    proc = subprocess.run(argv, cwd=cwd, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        return proc.returncode, None, proc.stderr
    try:
        return proc.returncode, json.loads(proc.stdout), proc.stderr
    except json.JSONDecodeError:
        return proc.returncode, None, proc.stdout[-2000:] + proc.stderr[-2000:]


def load_status(args: argparse.Namespace) -> dict[str, Any]:
    if args.from_file:
        return json.loads(Path(args.from_file).read_text(encoding="utf-8"))

    if not args.run_id:
        raise ValueError("--run-id or --from-file is required")

    repo_root = Path(args.root).resolve()
    local_runner = repo_root / "automation" / "run_plan_orchestrator.py"
    attempts = [
        [args.python, str(local_runner), "supervise", "status", "--run-id", args.run_id, "--format", "json"],
        [args.python, str(local_runner), "status", "--run-id", args.run_id, "--format", "json"],
    ]
    if not local_runner.exists():
        keel_run = Path(args.keel_root) / "bin" / "keel-run"
        attempts = [
            [str(keel_run), "supervise", "status", "--run-id", args.run_id, "--format", "json"],
            [str(keel_run), "status", "--run-id", args.run_id, "--format", "json"],
        ]

    errors: list[str] = []
    for argv in attempts:
        code, payload, err = run_json_command(argv, cwd=repo_root)
        if payload is not None:
            if isinstance(payload, dict):
                payload.setdefault("run_id", args.run_id)
                payload.setdefault("_status_command", " ".join(argv))
                return payload
            return {"run_id": args.run_id, "payload": payload, "_status_command": " ".join(argv)}
        errors.append(f"{' '.join(argv)} -> {code}: {err}")

    return {"run_id": args.run_id, "terminal_state": "unknown", "errors": errors}


def digest_status(payload: dict[str, Any], run_id: str | None = None) -> dict[str, Any]:
    items = extract_items(payload)
    kernel = _find_kernel_status(payload)
    supervision = payload.get("supervision_status")
    latest_intervention = supervision.get("latest_intervention") if isinstance(supervision, dict) else None
    latest_intervention = latest_intervention if isinstance(latest_intervention, dict) else {}
    return {
        "run_id": run_id or payload.get("run_id") or payload.get("id"),
        "terminal_state": determine_terminal_state(payload),
        "raw_state": payload.get("state") or payload.get("status") or payload.get("terminal_state"),
        "kernel_terminal_counts": kernel.get("terminal_counts") if kernel else None,
        "supervision": {
            "claim_class": supervision.get("claim_class"),
            "exit_code": supervision.get("exit_code"),
            "latest_action": latest_intervention.get("action_kind"),
            "latest_result": latest_intervention.get("result_status"),
            "latest_reason": latest_intervention.get("reason"),
        }
        if isinstance(supervision, dict)
        else None,
        "items_total": len(items) if items else None,
        "source": "keel_status_digest",
        "status_command": payload.get("_status_command"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Normalize Keel status JSON.")
    parser.add_argument("--run-id")
    parser.add_argument("--from-file")
    parser.add_argument("--keel-root", default="/Users/aeziz-local/keel")
    parser.add_argument("--root", default=".")
    parser.add_argument("--python", default=sys.executable)
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

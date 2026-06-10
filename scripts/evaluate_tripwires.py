#!/usr/bin/env python3
"""Evaluate configured AutoKeel tripwires without fabricating evidence."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.autonomy.autokeel import load_policy


OK_STATUSES = {"ok", "fallback_accepted"}

# Collectors stamp reports with `created_at` (see scripts/evidence/_collector_common.py).
# The remaining keys are accepted as fallbacks for hand-written or legacy reports.
REPORT_TIMESTAMP_KEYS = ("created_at", "generated_at", "collected_at", "timestamp", "ts")


def _report_timestamp(path: Path, payload: dict[str, Any]) -> float:
    """Ordering key for newest-wins selection.

    Prefer the report's own timestamp; fall back to the file's mtime when the
    report does not carry a parseable timestamp.
    """
    for key in REPORT_TIMESTAMP_KEYS:
        raw = payload.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        try:
            parsed = datetime.fromisoformat(raw.strip().replace("Z", "+00:00"))
        except ValueError:
            continue
        # Naive timestamps are interpreted as local time, matching how the
        # evidence collectors stamp `created_at`.
        return parsed.timestamp()
    try:
        return path.stat().st_mtime
    except OSError:
        return float("-inf")


def latest_json_report(path: Path) -> dict[str, Any] | None:
    """Return the newest evidence report, regardless of its status.

    Selection is newest-first by the report's own timestamp (falling back to
    file mtime), with the file name as a deterministic tie-breaker. Status
    must never influence selection: an older `ok` report must not mask a
    newer failure report, and an older failure must not mask a newer `ok`.
    Neither ops/autonomy/policy.yaml nor the design-doc Tripwires section
    defines an evidence staleness window, so newest-wins is the whole rule.
    """
    if path.is_file() and path.suffix.lower() == ".json":
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if not isinstance(payload, dict):
            return None
        payload["_report_path"] = str(path)
        return payload

    if not path.exists() or not path.is_dir():
        return None

    candidates: list[tuple[float, str, dict[str, Any]]] = []
    for report in path.glob("*.json"):
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        payload["_report_path"] = str(report)
        candidates.append((_report_timestamp(report, payload), report.name, payload))

    if not candidates:
        return None
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def evidence_status(root: Path, evidence_rel: str | None) -> dict[str, Any]:
    if not evidence_rel:
        return {"status": "missing", "reason": "no evidence path configured"}

    evidence_path = root / evidence_rel
    if not evidence_path.exists():
        return {"status": "missing", "path": evidence_rel}

    report = latest_json_report(evidence_path)
    if report is None:
        return {"status": "present_without_report", "path": evidence_rel}

    status = str(report.get("status", "unknown"))
    return {
        "status": status,
        "path": evidence_rel,
        "report": report.get("_report_path"),
        "ok": status in OK_STATUSES,
    }


def evaluate_tripwires(root: Path) -> dict[str, Any]:
    policy = load_policy(root / "ops" / "autonomy" / "policy.yaml")
    errors: list[str] = []
    warnings: list[str] = []
    fired: list[dict[str, Any]] = []

    tripwires = policy.get("tripwires", {})
    if not tripwires.get("apply_design_doc_tripwires"):
        errors.append("design-doc tripwires are not enabled")

    deadlines = policy.get("tripwire_deadlines", {})
    today = date.today()

    for name, config in deadlines.items():
        if not isinstance(config, dict):
            warnings.append(f"tripwire deadline is not executable: {name}")
            continue

        deadline_raw = config.get("date")
        evidence_rel = config.get("evidence")
        action = config.get("action") or tripwires.get(name)

        if not deadline_raw:
            warnings.append(f"tripwire missing date: {name}")
            continue

        try:
            deadline = date.fromisoformat(str(deadline_raw))
        except ValueError:
            errors.append(f"tripwire has invalid date: {name}={deadline_raw}")
            continue

        status = evidence_status(root, str(evidence_rel) if evidence_rel else None)

        if today >= deadline and not status.get("ok", False):
            fired.append(
                {
                    "name": name,
                    "deadline": str(deadline),
                    "action": action,
                    "evidence": evidence_rel,
                    "evidence_status": status,
                }
            )

    return {
        "status": "ok" if not errors and not fired else "error",
        "errors": errors,
        "warnings": warnings,
        "fired": fired,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate AutoKeel tripwire policy.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = evaluate_tripwires(Path(args.root).resolve())
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        for warning in report["warnings"]:
            print(f"WARNING: {warning}", file=sys.stderr)
        for item in report["fired"]:
            print(f"FIRED: {item['name']} -> {item.get('action')}", file=sys.stderr)
        print(report["status"])

    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

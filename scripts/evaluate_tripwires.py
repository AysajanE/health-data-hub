#!/usr/bin/env python3
"""Evaluate configured AutoKeel tripwires without fabricating evidence."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.autonomy.autokeel import load_policy


OK_STATUSES = {"ok", "fallback_accepted"}


def latest_json_report(path: Path) -> dict[str, Any] | None:
    if path.is_file() and path.suffix.lower() == ".json":
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    if not path.exists() or not path.is_dir():
        return None

    reports = sorted(path.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
    newest: dict[str, Any] | None = None
    for report in reports:
        try:
            payload = json.loads(report.read_text(encoding="utf-8"))
            payload["_report_path"] = str(report)
            if newest is None:
                newest = payload
            if str(payload.get("status", "unknown")) in OK_STATUSES:
                return payload
        except Exception:
            continue
    return newest


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

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
        evidence_ok = bool(evidence_rel and (root / str(evidence_rel)).exists())
        if today >= deadline and not evidence_ok:
            fired.append({"name": name, "deadline": str(deadline), "action": action, "missing_evidence": evidence_rel})

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

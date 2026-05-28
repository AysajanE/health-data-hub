#!/usr/bin/env python3
"""Verify AutoKeel failure-ledger safety semantics."""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Any


CRITICAL_CLASSES = {"state_divergence", "provider_auth_failure", "review_artifact_invalid"}


def iter_jsonl(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def verify_failure_ledger(root: Path, slice_id: str | None = None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    rows = list(iter_jsonl(root / "ops/autonomy/failure_ledger.jsonl") or [])
    if slice_id:
        rows = [row for row in rows if row.get("slice") == slice_id]

    class_counts: Counter[str] = Counter()
    for index, row in enumerate(rows, start=1):
        failure_class = str(row.get("failure_class") or "unclassified")
        class_counts[failure_class] += 1
        open_ = bool(row.get("open", True))
        severity = str(row.get("severity") or "")
        if open_ and severity in {"high", "critical"}:
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
            for key in (
                "root_cause_id",
                "failure_origin",
                "supersedes",
                "superseded_by",
                "false_positive",
                "closure_validation_command",
            ):
                if key not in row:
                    errors.append(f"v2 ledger row missing {key}: row {index}")
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

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "rows": len(rows)}


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

#!/usr/bin/env python3
"""Validate required autonomous review artifacts for a slice.

A review artifact must exist and contain a real autonomous review, not just
an empty placeholder. This is part of autonomous gate substitution: tests plus
review artifacts replace human signoff, but must never be represented as human
approval.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


PASS_RE = re.compile(r"(?im)^\s*(verdict|result)\s*:\s*pass\s*$")
FAIL_RE = re.compile(r"(?im)^\s*(verdict|result)\s*:\s*fail\s*$")
EVIDENCE_RE = re.compile(r"(?i)evidence files? checked|evidence checked|files checked")
COMMANDS_RE = re.compile(r"(?i)exact commands run|commands run|verification commands")
BLOCKING_RE = re.compile(r"(?i)blocking findings|blocking issues|blockers")
BLOCKING_NONE_RE = re.compile(r"(?im)^\s*(blocking findings|blocking issues|blockers)\s*:\s*(none|no blocking findings|no blockers)\s*$")
PROVENANCE_RE = re.compile(r"(?i)autonomous slice review|independent reviewer|autonomous review")


def load_slices(root: Path) -> list[dict[str, Any]]:
    path = root / "ops" / "autonomy" / "slices.json"
    return json.loads(path.read_text(encoding="utf-8"))


def validate_review_file(root: Path, rel_path: str) -> list[str]:
    errors: list[str] = []
    path = root / rel_path
    if not path.exists():
        return [f"missing autonomous review artifact: {rel_path}"]
    if not path.is_file():
        return [f"review artifact is not a file: {rel_path}"]

    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if len(text) < 120:
        errors.append(f"review artifact too short to be meaningful: {rel_path}")
    if not PROVENANCE_RE.search(text):
        errors.append(f"review artifact must state autonomous reviewer provenance: {rel_path}")
    if FAIL_RE.search(text):
        errors.append(f"review artifact contains failing verdict: {rel_path}")
    if not PASS_RE.search(text):
        errors.append(f"review artifact missing required 'Verdict: pass': {rel_path}")
    if not EVIDENCE_RE.search(text):
        errors.append(f"review artifact must list evidence files checked: {rel_path}")
    if not COMMANDS_RE.search(text):
        errors.append(f"review artifact must list exact commands run: {rel_path}")
    if not BLOCKING_RE.search(text):
        errors.append(f"review artifact must mention blocking findings/blockers: {rel_path}")
    elif PASS_RE.search(text) and not BLOCKING_NONE_RE.search(text):
        errors.append(f"passing review artifact must explicitly state no blocking findings: {rel_path}")
    return errors


def check_review(root: Path, slice_id: str) -> dict[str, Any]:
    errors: list[str] = []
    target = next((item for item in load_slices(root) if item.get("id") == slice_id), None)
    if not target:
        return {"status": "error", "errors": [f"unknown slice: {slice_id}"]}

    artifacts = target.get("review_artifacts", [])
    for artifact in artifacts:
        errors.extend(validate_review_file(root, artifact))

    return {
        "status": "ok" if not errors else "error",
        "errors": errors,
        "review_artifacts": artifacts,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate required autonomous review artifacts.")
    parser.add_argument("slice_id")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = check_review(Path(args.root).resolve(), args.slice_id)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

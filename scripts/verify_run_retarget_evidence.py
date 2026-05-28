#!/usr/bin/env python3
"""Validate evidence for a high-risk PO run-branch retarget operation."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any


REQUIRED_FIELDS = {
    "slice",
    "run_id",
    "old_run_branch_head",
    "new_target_commit",
    "merge_base",
    "item_checkpoint_ancestry_proof",
    "terminal_counts_before",
    "terminal_counts_after",
    "reason",
    "closure_evidence",
}


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def git_ok(root: Path, *argv: str) -> bool:
    proc = subprocess.run(["git", *argv], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return proc.returncode == 0


def verify_run_retarget_evidence(path: Path, root: Path | None = None) -> dict[str, Any]:
    root = (root or Path.cwd()).resolve()
    errors: list[str] = []
    evidence = load_json(path, {})
    if not isinstance(evidence, dict):
        return {"status": "error", "errors": [f"retarget evidence is not a JSON object: {path}"], "warnings": []}
    for field in sorted(REQUIRED_FIELDS):
        if not evidence.get(field):
            errors.append(f"missing required field: {field}")
    old_head = str(evidence.get("old_run_branch_head") or "")
    new_commit = str(evidence.get("new_target_commit") or "")
    merge_base = str(evidence.get("merge_base") or "")
    if old_head and not git_ok(root, "cat-file", "-e", old_head):
        errors.append("old_run_branch_head is not reachable")
    if new_commit and not git_ok(root, "cat-file", "-e", new_commit):
        errors.append("new_target_commit is not reachable")
    if old_head and new_commit and merge_base:
        proc = subprocess.run(
            ["git", "merge-base", old_head, new_commit],
            cwd=root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if proc.returncode != 0 or proc.stdout.strip() != merge_base:
            errors.append("merge_base does not match old/new commit ancestry")
    closure = str(evidence.get("closure_evidence") or "")
    if closure and not (root / closure).exists():
        errors.append(f"closure_evidence path missing: {closure}")
    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": []}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify PO run retarget evidence.")
    parser.add_argument("evidence")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = verify_run_retarget_evidence(Path(args.evidence), root=Path(args.root).resolve())
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

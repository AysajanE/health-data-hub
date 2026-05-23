#!/usr/bin/env python3
"""Ensure required autonomous review artifacts exist for a slice."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def load_slices(root: Path) -> list[dict[str, Any]]:
    return json.loads((root / "ops" / "autonomy" / "slices.json").read_text(encoding="utf-8"))


def check_review(root: Path, slice_id: str) -> dict[str, Any]:
    errors: list[str] = []
    target = next((item for item in load_slices(root) if item.get("id") == slice_id), None)
    if not target:
        return {"status": "error", "errors": [f"unknown slice: {slice_id}"]}
    for artifact in target.get("review_artifacts", []):
        if not (root / artifact).exists():
            errors.append(f"missing autonomous review artifact: {artifact}")
    return {"status": "ok" if not errors else "error", "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check required autonomous review artifact paths.")
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

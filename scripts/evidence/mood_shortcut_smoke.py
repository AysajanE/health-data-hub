#!/usr/bin/env python3
"""Collect local mood shortcut evidence for S03."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.evidence._collector_common import write_report


def collect(root: Path) -> dict[str, object]:
    rel = os.environ.get("MOOD_SHORTCUT_EVIDENCE_FILE")
    if not rel:
        path = write_report(root, "mood_shortcut_smoke", {"status": "blocked_external", "missing_env": ["MOOD_SHORTCUT_EVIDENCE_FILE"]})
        return {"status": "blocked_external", "evidence": str(path.relative_to(root)), "errors": ["missing MOOD_SHORTCUT_EVIDENCE_FILE"]}
    evidence = Path(rel).expanduser()
    if not evidence.is_absolute():
        evidence = root / evidence
    if not evidence.exists():
        path = write_report(root, "mood_shortcut_smoke", {"status": "blocked_external", "missing_file": str(evidence)})
        return {"status": "blocked_external", "evidence": str(path.relative_to(root)), "errors": [f"missing evidence file: {evidence}"]}
    path = write_report(root, "mood_shortcut_smoke", {"status": "ok", "checked_file": str(evidence)})
    return {"status": "ok", "evidence": str(path.relative_to(root)), "errors": []}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect local mood shortcut evidence.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = collect(Path(args.root).resolve())
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

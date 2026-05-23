#!/usr/bin/env python3
"""Collect real local pyEight availability evidence for S03."""

from __future__ import annotations

import argparse
import importlib.util
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.evidence._collector_common import write_report


def collect(root: Path) -> dict[str, object]:
    spec = importlib.util.find_spec("pyeight")
    if spec is None:
        path = write_report(root, "pyeight_smoke", {"status": "blocked_external", "missing_python_module": "pyeight"})
        return {"status": "blocked_external", "evidence": str(path.relative_to(root)), "errors": ["missing pyeight python module"]}
    path = write_report(root, "pyeight_smoke", {"status": "ok", "module_origin": spec.origin})
    return {"status": "ok", "evidence": str(path.relative_to(root)), "errors": []}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Collect pyEight smoke evidence.")
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

#!/usr/bin/env python3
"""CLI for committed AutoKeel slice-integration lineage verification."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.slice_integration import verify_slice_integration


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Classify whether a completed slice is committed on a continuation ref."
    )
    parser.add_argument("slice_id")
    parser.add_argument("--root", default=".")
    parser.add_argument("--continuation-ref", default="main")
    parser.add_argument("--receipt")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_slice_integration(
        Path(args.root),
        args.slice_id,
        continuation_ref=args.continuation_ref,
        receipt_path=args.receipt,
    )
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["classification"] or report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

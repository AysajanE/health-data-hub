#!/usr/bin/env python3
"""Collect real local pyEight availability evidence for S03."""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.evidence._collector_common import write_report


def fallback_decision_exists(root: Path) -> Path | None:
    decisions = sorted((root / "ops/autonomy/decisions").glob("*pyeight*json"))
    for decision in reversed(decisions):
        try:
            payload = json.loads(decision.read_text(encoding="utf-8"))
        except Exception:
            continue
        if payload.get("status") == "fallback_accepted" and payload.get("action") == "oura_only_v1":
            return decision
    return None


def collect(root: Path) -> dict[str, object]:
    decision = fallback_decision_exists(root)
    if decision:
        path = write_report(
            root,
            "pyeight_smoke",
            {
                "status": "fallback_accepted",
                "decision": str(decision.relative_to(root)),
                "fallback": "oura_only_v1",
            },
        )
        return {"status": "fallback_accepted", "evidence": str(path.relative_to(root)), "errors": []}

    spec = importlib.util.find_spec("pyeight")
    if spec is None:
        path = write_report(root, "pyeight_smoke", {"status": "blocked_external", "missing_python_module": "pyeight"})
        return {"status": "blocked_external", "evidence": str(path.relative_to(root)), "errors": ["missing pyeight python module"]}

    missing_env = [key for key in ("PYEIGHT_EMAIL", "PYEIGHT_PASSWORD") if not os.environ.get(key)]
    if missing_env:
        path = write_report(root, "pyeight_smoke", {"status": "blocked_external", "module_origin": spec.origin, "missing_env": missing_env})
        return {"status": "blocked_external", "evidence": str(path.relative_to(root)), "errors": [f"missing {', '.join(missing_env)}"]}

    path = write_report(
        root,
        "pyeight_smoke",
        {
            "status": "blocked_external",
            "module_origin": spec.origin,
            "reason": "module and credentials present; authenticated last-night fetch is not implemented in this local collector",
        },
    )
    return {
        "status": "blocked_external",
        "evidence": str(path.relative_to(root)),
        "errors": ["authenticated pyEight last-night fetch not implemented"],
    }


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
    return 0 if report["status"] in {"ok", "fallback_accepted"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

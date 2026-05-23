#!/usr/bin/env python3
"""Global v1 verification gate for AutoKeel."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_no_tracked_data import check_no_tracked_data


CRITICAL_FAILURES = {"manual_gate_leak", "secret_leak_risk", "unsafe_write_root", "forbidden_ui_language", "state_divergence", "ship_failure"}
UI_BANNED_RE = re.compile(r"\b(biggest drivers|drivers|what made you tired|caused|you should|you would have felt|tomorrow prediction)\b", re.I)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def iter_jsonl(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def scan_ui_language(root: Path) -> list[str]:
    errors: list[str] = []
    for base in ("app", "src"):
        path = root / base
        if not path.exists():
            continue
        for file in path.rglob("*"):
            if file.suffix.lower() not in {".py", ".md", ".html", ".txt", ".js", ".ts", ".tsx"}:
                continue
            try:
                text = file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if UI_BANNED_RE.search(text):
                errors.append(f"forbidden v1 UI language in {file.relative_to(root)}")
    return errors


def verify_v1(root: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    slices = load_json(root / "ops" / "autonomy" / "slices.json", [])
    required = [item for item in slices if item.get("required")]
    incomplete = [item["id"] for item in required if item.get("status") != "complete"]
    if incomplete:
        errors.append(f"required slices incomplete: {', '.join(incomplete)}")

    failures = list(iter_jsonl(root / "ops" / "autonomy" / "failure_ledger.jsonl") or [])
    open_critical = [
        item
        for item in failures
        if item.get("open", True) and (item.get("severity") in {"high", "critical"} or item.get("failure_class") in CRITICAL_FAILURES)
    ]
    if open_critical:
        errors.append(f"open critical/high failures: {len(open_critical)}")

    tracked = check_no_tracked_data(root)
    errors.extend(tracked["errors"])
    warnings.extend(tracked.get("warnings", []))
    errors.extend(scan_ui_language(root))

    state = load_json(root / "ops" / "autonomy" / "autonomy_state.json", {})
    if state.get("active_run"):
        warnings.append("active_run still present in autonomy_state.json")

    return {
        "status": "ok" if not errors else "error",
        "errors": errors,
        "warnings": warnings,
        "required_slices": [item["id"] for item in required],
        "incomplete_slices": incomplete,
        "open_critical_failures": len(open_critical),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Health Data Hub v1 autonomous completion.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = verify_v1(Path(args.root).resolve())
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

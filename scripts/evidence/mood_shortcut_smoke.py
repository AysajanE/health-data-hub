#!/usr/bin/env python3
"""Collect local mood shortcut evidence for S03."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import urllib.request
from pathlib import Path

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from scripts.evidence._collector_common import write_report


def collect(root: Path) -> dict[str, object]:
    url = os.environ.get("MOOD_SHORTCUT_TEST_URL")
    token = os.environ.get("MOOD_SHORTCUT_TOKEN")
    if url and token:
        payload = json.dumps({"feeling": 3, "energy": 3, "notes": "autokeel smoke", "context_chips": ["autokeel"]}).encode("utf-8")
        request = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json", "X-Mood-Token": token}, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                status_code = response.status
                body = response.read(1000).decode("utf-8", errors="replace")
        except Exception as exc:
            path = write_report(root, "mood_shortcut_smoke", {"status": "error", "error_type": type(exc).__name__, "error": str(exc)})
            return {"status": "error", "evidence": str(path.relative_to(root)), "errors": [str(exc)]}

        db_rel = os.environ.get("MOOD_SHORTCUT_VERIFY_SQLITE")
        db_checked = False
        if db_rel:
            db_path = Path(db_rel)
            if not db_path.is_absolute():
                db_path = root / db_path
            if db_path.exists():
                with sqlite3.connect(db_path) as connection:
                    count = connection.execute("select count(*) from mood_entries").fetchone()[0]
                db_checked = count > 0
        path = write_report(root, "mood_shortcut_smoke", {"status": "ok", "http_status": status_code, "response_chars": len(body), "db_checked": db_checked})
        return {"status": "ok", "evidence": str(path.relative_to(root)), "errors": []}

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

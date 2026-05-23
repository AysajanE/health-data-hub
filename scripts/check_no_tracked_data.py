#!/usr/bin/env python3
"""Verify no health data, secrets, or token-like values are tracked by git."""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


FORBIDDEN_PATH_RE = re.compile(
    r"(^|/)(data|private)(/|$)|(^|/)\.env($|\.)|"
    r"\.(duckdb|duckdb\.wal|sqlite|sqlite3|parquet)$|"
    r"(^|/)(raw|secrets|quarantine|snapshots)(/|$)",
    re.I,
)
SECRET_CONTENT_RE = re.compile(
    r"(?i)(access_token|refresh_token|mood_token|x-mood-token|client_secret|password|authorization)"
    r"\s*[:=]\s*['\"]?[A-Za-z0-9_\-./+=]{16,}"
)


def git_ls_files(root: Path) -> list[str]:
    proc = subprocess.run(["git", "ls-files", "-z"], cwd=str(root), text=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        return []
    return [part.decode("utf-8") for part in proc.stdout.split(b"\0") if part]


def check_no_tracked_data(root: Path) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    for rel in git_ls_files(root):
        if FORBIDDEN_PATH_RE.search(rel):
            errors.append(f"tracked sensitive path: {rel}")
            continue
        path = root / rel
        if not path.is_file() or path.stat().st_size > 1_000_000:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        if SECRET_CONTENT_RE.search(text):
            errors.append(f"tracked file appears to contain a secret/token value: {rel}")
    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check git-tracked files for health data and secret leaks.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = check_no_tracked_data(Path(args.root).resolve())
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

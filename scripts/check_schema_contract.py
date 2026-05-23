#!/usr/bin/env python3
"""S01 schema contract checker for Health Data Hub v1."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


REQUIRED_TABLES = {"sleep_nights", "mood_entries", "mood_current", "daily_features", "sleep_merge_diagnostics"}
DAILY_REQUIRED = {"total_sleep_min", "hrv_z", "deep_sleep_pct", "prior_day_feeling", "hrv_avg_ms"}
DAILY_FORBIDDEN = {"training_load_7d", "days_since_zone4", "subjective_energy_lag1", "body_temp_dev_c_lag1", "rhr_avg_bpm", "body_temp_dev_c", "nutrition", "diet"}


def table_columns(sql: str, table: str) -> set[str]:
    pattern = re.compile(rf"create\s+table\s+{re.escape(table)}\s*\((.*?)\);", re.I | re.S)
    match = pattern.search(sql)
    if not match:
        return set()
    cols: set[str] = set()
    for raw in match.group(1).splitlines():
        line = raw.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        name = line.split()[0].strip('"')
        if name.lower() in {"primary", "foreign", "unique", "constraint"}:
            continue
        cols.add(name)
    return cols


def check_schema(path: Path) -> dict:
    errors: list[str] = []
    if not path.exists():
        return {"status": "error", "errors": [f"schema missing: {path}"]}
    sql = path.read_text(encoding="utf-8")
    tables = set(re.findall(r"create\s+table\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.I))
    missing = REQUIRED_TABLES - tables
    extra_core = tables - REQUIRED_TABLES
    if missing:
        errors.append(f"missing required tables: {', '.join(sorted(missing))}")
    if extra_core:
        errors.append(f"unexpected v1 core tables: {', '.join(sorted(extra_core))}")
    daily = table_columns(sql, "daily_features")
    missing_daily = DAILY_REQUIRED - daily
    forbidden_daily = daily & DAILY_FORBIDDEN
    if missing_daily:
        errors.append(f"daily_features missing columns: {', '.join(sorted(missing_daily))}")
    if forbidden_daily:
        errors.append(f"daily_features contains v2/forbidden columns: {', '.join(sorted(forbidden_daily))}")
    if "is_imputed" in daily:
        errors.append("daily_features must not use coarse is_imputed flag")
    if "is_imputed" in table_columns(sql, "sleep_nights"):
        errors.append("sleep_nights must not contain is_imputed; sleep is never forward-filled")
    return {"status": "ok" if not errors else "error", "errors": errors}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check S01 schema contract.")
    parser.add_argument("--schema", default="src/db/schema.sql")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = check_schema(Path(args.schema))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

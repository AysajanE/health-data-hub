#!/usr/bin/env python3
"""S01 schema contract checker for Health Data Hub v1."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path


REQUIRED_TABLES = {
    "sleep_nights",
    "mood_entries",
    "mood_current",
    "daily_features",
    "sleep_merge_diagnostics",
}

REQUIRED_COLUMNS = {
    "sleep_nights": {
        "source",
        "sleep_date",
        "bedtime_utc",
        "waketime_utc",
        "total_sleep_min",
        "rem_min",
        "deep_min",
        "light_min",
        "awake_min",
        "hrv_avg_ms",
        "rhr_avg_bpm",
        "body_temp_dev_c",
        "sleep_score",
        "ingested_at_utc",
    },
    "mood_entries": {
        "log_id",
        "logged_at_utc",
        "mood_date",
        "feeling",
        "energy",
        "notes",
        "context_chips",
        "source",
        "supersedes_log_id",
    },
    "mood_current": {
        "mood_date",
        "log_id",
    },
    "daily_features": {
        "feature_date",
        "total_sleep_min",
        "hrv_z",
        "deep_sleep_pct",
        "prior_day_feeling",
        "hrv_avg_ms",
        "hrv_z_method",
        "feature_version",
        "prior_day_feeling_imputed",
        "sleep_source_count",
        "sleep_merge_warning",
        "computed_at_utc",
    },
    "sleep_merge_diagnostics": {
        "sleep_date",
        "oura_present",
        "eight_present",
        "total_sleep_delta_min",
        "hrv_merge_method",
        "stage_source",
        "warning",
        "computed_at_utc",
    },
}

DAILY_FORBIDDEN = {
    "training_load_7d",
    "days_since_zone4",
    "subjective_energy_lag1",
    "body_temp_dev_c_lag1",
    "rhr_avg_bpm",
    "body_temp_dev_c",
    "nutrition",
    "diet",
    "is_imputed",
}

SLEEP_FORBIDDEN = {"is_imputed"}
CREATE_TABLE_RE = r"create\s+table\s+(?:if\s+not\s+exists\s+)?"


def normalize_sql(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def table_names(sql: str) -> set[str]:
    return set(re.findall(CREATE_TABLE_RE + r"([A-Za-z_][A-Za-z0-9_]*)", sql, re.I))


def table_columns(sql: str, table: str) -> set[str]:
    pattern = re.compile(rf"{CREATE_TABLE_RE}{re.escape(table)}\s*\((.*?)\);", re.I | re.S)
    match = pattern.search(sql)
    if not match:
        return set()

    cols: set[str] = set()
    for raw in match.group(1).splitlines():
        line = raw.strip().rstrip(",")
        if not line or line.startswith("--"):
            continue
        name = line.split()[0].strip('"')
        if name.lower() in {"primary", "foreign", "unique", "constraint", "check"}:
            continue
        cols.add(name)
    return cols


def has_primary_key(sql: str, table: str, expected_fragment: str) -> bool:
    pattern = re.compile(rf"{CREATE_TABLE_RE}{re.escape(table)}\s*\((.*?)\);", re.I | re.S)
    match = pattern.search(sql)
    if not match:
        return False
    body = normalize_sql(match.group(1)).lower()
    return expected_fragment.lower() in body


def check_schema(path: Path) -> dict:
    errors: list[str] = []
    warnings: list[str] = []

    if not path.exists():
        return {"status": "error", "errors": [f"schema missing: {path}"], "warnings": []}

    sql = path.read_text(encoding="utf-8")
    tables = table_names(sql)

    missing = REQUIRED_TABLES - tables
    extra = tables - REQUIRED_TABLES

    if missing:
        errors.append(f"missing required tables: {', '.join(sorted(missing))}")
    if extra:
        errors.append(f"unexpected v1 core tables: {', '.join(sorted(extra))}")

    for table, required_cols in REQUIRED_COLUMNS.items():
        cols = table_columns(sql, table)
        missing_cols = required_cols - cols
        if missing_cols:
            errors.append(f"{table} missing columns: {', '.join(sorted(missing_cols))}")

    daily = table_columns(sql, "daily_features")
    forbidden_daily = daily & DAILY_FORBIDDEN
    if forbidden_daily:
        errors.append(f"daily_features contains v2/forbidden columns: {', '.join(sorted(forbidden_daily))}")

    sleep_cols = table_columns(sql, "sleep_nights")
    forbidden_sleep = sleep_cols & SLEEP_FORBIDDEN
    if forbidden_sleep:
        errors.append(f"sleep_nights contains forbidden columns: {', '.join(sorted(forbidden_sleep))}")

    if re.search(r"create\s+unique\s+index\b.*\bwhere\b", sql, re.I | re.S):
        errors.append("schema must not rely on DuckDB partial unique indexes for mood_current/corrections")

    if not has_primary_key(sql, "mood_current", "primary key"):
        errors.append("mood_current must define mood_date as primary key")

    if not has_primary_key(sql, "daily_features", "primary key"):
        errors.append("daily_features must define feature_date as primary key")

    sleep_body = normalize_sql(sql).lower()
    if "primary key (source, sleep_date)" not in sleep_body and "primary key(source,sleep_date)" not in sleep_body.replace(" ", ""):
        warnings.append("sleep_nights should define PRIMARY KEY (source, sleep_date)")

    if "hrv_z" not in daily or "hrv_avg_ms" not in daily:
        errors.append("daily_features must persist hrv_z and include hrv_avg_ms display metadata")

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings}


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
        for warning in report["warnings"]:
            print(f"WARNING: {warning}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

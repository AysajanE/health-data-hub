#!/usr/bin/env python3
"""Verify S04 can start from the resolved S03 provider decision."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.validate_provider_decisions import validate_provider_decisions
from scripts.verify_s03_readiness import verify_s03_readiness


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


def git_stdout(root: Path, *argv: str) -> tuple[bool, str]:
    proc = subprocess.run(
        ["git", *argv],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode == 0, proc.stdout.strip()


def active_pyeight_state(root: Path) -> tuple[str, str | None]:
    decisions = sorted((root / "ops/autonomy/decisions").glob("*.json"), key=lambda item: item.name)
    active_include: str | None = None
    active_fallback: str | None = None
    for path in decisions:
        payload = load_json(path, {})
        if not isinstance(payload, dict):
            continue
        if payload.get("slice") != "S03" or str(payload.get("provider") or "").lower() not in {"pyeight", "8sleep", "eight_sleep"}:
            continue
        if payload.get("superseded_by"):
            continue
        rel = str(path.relative_to(root))
        if payload.get("status") == "fallback_accepted" and payload.get("fallback_active") is True:
            active_fallback = rel
        elif payload.get("status") == "ok" and payload.get("fallback_active") is False:
            active_include = rel
    if active_fallback:
        return "fallback_active", active_fallback
    if active_include:
        return "include_active", active_include
    return "unknown", None


def verify_s04_readiness(root: Path) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    slices = load_json(root / "ops/autonomy/slices.json", [])
    if not isinstance(slices, list):
        return {"status": "error", "errors": ["slices.json must contain a list"], "warnings": [], "checks": checks}
    by_id = {item.get("id"): item for item in slices if isinstance(item, dict)}
    for slice_id in ("S01", "S02", "S03"):
        status = by_id.get(slice_id, {}).get("status")
        checks[f"{slice_id.lower()}_status"] = status
        if status != "complete":
            errors.append(f"{slice_id} must be complete before S04 readiness: {status}")

    s03 = by_id.get("S03", {})
    ship_branch = str(s03.get("ship_branch") or "")
    ship_commit = str(s03.get("ship_commit") or "")
    checks["s03_ship_branch"] = ship_branch
    checks["s03_ship_commit"] = ship_commit
    if not ship_branch or not ship_commit:
        errors.append("S03 completion is missing ship_branch or ship_commit")
    elif ship_branch and ship_commit:
        ok, branch_head = git_stdout(root, "rev-parse", "--verify", ship_branch)
        if not ok:
            errors.append(f"S03 ship_branch does not resolve: {ship_branch}")
        elif branch_head != ship_commit:
            errors.append(f"S03 ship_branch points to {branch_head} but slices.json records {ship_commit}")

    s03_readiness = verify_s03_readiness(root)
    checks["s03_readiness_status"] = s03_readiness["status"]
    if s03_readiness["status"] != "ok":
        errors.extend(f"S03 readiness failed: {error}" for error in s03_readiness["errors"])

    provider_decisions = validate_provider_decisions(root, "S03")
    checks["s03_provider_decisions_status"] = provider_decisions["status"]
    checks["s03_provider_decision_count"] = provider_decisions["decision_count"]
    if provider_decisions["status"] != "ok":
        errors.extend(f"S03 provider decision invalid: {error}" for error in provider_decisions["errors"])

    evidence_summary = root / "docs/evidence/ingestion/s03-ingestion-evidence.md"
    summary_text = evidence_summary.read_text(encoding="utf-8") if evidence_summary.exists() else ""
    checks["s03_evidence_summary"] = str(evidence_summary.relative_to(root)) if evidence_summary.exists() else None
    if "direct_oura_api_v2_periodic_pull" not in summary_text and "direct Oura API v2 periodic pull" not in summary_text:
        errors.append("S03 provider decision summary does not resolve Oura as active")
    if "oura_only_v1" not in summary_text:
        errors.append("S03 provider decision summary does not record the Oura-only v1 fallback")

    pyeight_state, pyeight_path = active_pyeight_state(root)
    checks["pyeight_state"] = pyeight_state
    checks["pyeight_decision"] = pyeight_path
    if pyeight_state not in {"fallback_active", "include_active"}:
        errors.append("pyEight fallback/include state is not explicit")

    open_high = [
        row
        for row in (iter_jsonl(root / "ops/autonomy/failure_ledger.jsonl") or [])
        if row.get("open", True) and row.get("severity") in {"high", "critical"}
    ]
    checks["open_high_or_critical_failures"] = len(open_high)
    if open_high:
        errors.append(
            "open high/critical failures block S04 readiness: "
            + ", ".join(str(row.get("failure_class") or "unknown") for row in open_high)
        )

    brief = root / "docs/briefs/s04-feature-engineering.autonomous-brief.md"
    checks["s04_brief"] = str(brief.relative_to(root)) if brief.exists() else None
    if not brief.exists():
        errors.append("S04 autonomous brief is missing")
    else:
        brief_text = brief.read_text(encoding="utf-8").lower()
        required_phrases = {
            "active s03 provider decision": "S04 brief must require reading the active S03 provider decision",
            "oura-only v1": "S04 brief must treat Oura-only v1 as first-class",
            "must not require pyeight evidence": "S04 brief must not require pyEight evidence",
            "8 sleep must remain absent/fallback": "S04 brief must keep 8 Sleep absent/fallback",
        }
        for phrase, error in required_phrases.items():
            if phrase not in brief_text:
                errors.append(error)

    autoplan = root / "docs/gstack/s04-feature-engineering-autoplan.md"
    checks["s04_autoplan"] = str(autoplan.relative_to(root)) if autoplan.exists() else None
    if not autoplan.exists():
        errors.append("S04 autoplan is missing")
    else:
        autoplan_text = autoplan.read_text(encoding="utf-8").lower()
        for phrase, error in {
            "active s03 provider decision": "S04 autoplan must require reading the active S03 provider decision",
            "oura-only v1": "S04 autoplan must treat Oura-only v1 as first-class",
            "must not require pyeight evidence": "S04 autoplan must not require pyEight evidence",
            "8 sleep must remain absent/fallback": "S04 autoplan must keep 8 Sleep absent/fallback",
        }.items():
            if phrase not in autoplan_text:
                errors.append(error)

    warnings.extend(s03_readiness.get("warnings", []))
    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify S04 launch readiness.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_s04_readiness(Path(args.root).resolve())
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

#!/usr/bin/env python3
"""Verify controlled-autonomous readiness for S03 provider ingestion."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_no_tracked_data import check_no_tracked_data


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


def git_check_ignore(root: Path, rel: str) -> bool:
    proc = subprocess.run(["git", "check-ignore", "-q", rel], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return proc.returncode == 0


def evidence_report_exists(root: Path, rel: str) -> tuple[bool, str | None]:
    path = root / rel
    candidates = []
    if path.is_file():
        candidates.append(path)
    elif path.is_dir():
        candidates.extend(path.rglob("*.json"))
    for candidate in sorted(candidates, key=lambda item: (item.stat().st_mtime_ns, item.name), reverse=True):
        payload = load_json(candidate, {})
        if isinstance(payload, dict):
            return payload.get("status") in {"ok", "blocked_external", "error", "fallback_accepted"}, str(candidate.relative_to(root))
    return False, None


def pyeight_decision_state(root: Path) -> tuple[bool, bool, str | None]:
    decisions_dir = root / "ops/autonomy/decisions"
    if not decisions_dir.exists():
        return False, False, None
    for path in sorted(decisions_dir.glob("*.json"), key=lambda item: item.name, reverse=True):
        if "pyeight" not in path.name.lower():
            continue
        payload = load_json(path, {})
        if not isinstance(payload, dict):
            continue
        provider = str(payload.get("provider") or payload.get("service") or "").lower()
        status = str(payload.get("status") or "").lower()
        action = str(payload.get("action") or payload.get("decision") or "").lower()
        fallback_active = bool(payload.get("fallback_active")) or status == "fallback_accepted" or action == "oura_only_v1"
        evidence_ok = (
            provider in {"pyeight", "8sleep", "eight_sleep"}
            and status in {"ok", "evidence_ok", "accepted"}
            and str(payload.get("evidence_status") or "ok").lower() == "ok"
        )
        if fallback_active or evidence_ok:
            return True, fallback_active, str(path.relative_to(root))
    return False, False, None


def verify_s03_readiness(root: Path) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}
    slices = load_json(root / "ops/autonomy/slices.json", [])
    if not isinstance(slices, list):
        return {"status": "error", "errors": ["slices.json must contain a list"], "warnings": [], "checks": checks}
    by_id = {item.get("id"): item for item in slices if isinstance(item, dict)}
    for slice_id in ("S01", "S02"):
        status = by_id.get(slice_id, {}).get("status")
        checks[f"{slice_id.lower()}_status"] = status
        if status != "complete":
            errors.append(f"{slice_id} must be complete before S03 readiness: {status}")

    oura_ok, oura_path = evidence_report_exists(root, "private/evidence/S03/oura_smoke")
    checks["oura_evidence"] = oura_path
    missing_env = [name for name in ("OURA_ACCESS_TOKEN",) if not (os.environ.get(name) or "").strip()]
    checks["required_token_env_present"] = not missing_env
    checks["missing_env"] = missing_env
    checks["secret_values_logged"] = False
    if not oura_ok and missing_env:
        failures = list(iter_jsonl(root / "ops/autonomy/failure_ledger.jsonl") or [])
        blocked = any(
            row.get("slice") == "S03"
            and row.get("failure_class") == "blocked_external_missing_evidence"
            and row.get("open", True)
            for row in failures
        )
        if not blocked:
            errors.append("Oura evidence preflight is missing and OURA_ACCESS_TOKEN is absent")

    pyeight_ok, pyeight_path = evidence_report_exists(root, "private/evidence/S03/pyeight_smoke")
    decision_ok, fallback, decision_path = pyeight_decision_state(root)
    checks["pyeight_evidence"] = pyeight_path
    checks["pyeight_decision"] = decision_path
    checks["pyeight_fallback_explicit"] = fallback
    checks["pyeight_provider_state_explicit"] = pyeight_ok or decision_ok
    if not pyeight_ok and not decision_ok:
        errors.append("pyEight evidence/fallback/tripwire state is not explicit")

    tracked = check_no_tracked_data(root)
    if tracked["status"] != "ok":
        errors.extend(tracked["errors"])
    warnings.extend(tracked.get("warnings", []))

    checks["private_evidence_ignored"] = git_check_ignore(root, "private/evidence/S03/test-ignore-probe")
    if not checks["private_evidence_ignored"]:
        errors.append("private/evidence/S03 is not ignored by git")

    s03 = by_id.get("S03", {})
    planned_reviews = s03.get("review_artifacts") if isinstance(s03, dict) else []
    checks["review_artifacts"] = planned_reviews
    if not planned_reviews:
        errors.append("S03 review artifact path is not planned in slices.json")

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify S03 AutoKeel readiness.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_s03_readiness(Path(args.root).resolve())
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

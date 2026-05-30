#!/usr/bin/env python3
"""Verify controlled-autonomous readiness for S03 provider ingestion."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_no_tracked_data import check_no_tracked_data
from scripts.evidence._collector_common import env_present
from scripts.validate_provider_decisions import validate_provider_decisions


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


DEFAULT_EVIDENCE_STATUSES = {"ok", "blocked_external", "error", "fallback_accepted"}


def plan_orchestrator_primary_root(root: Path) -> Path | None:
    parts = root.resolve().parts
    marker = (".local", "automation", "plan_orchestrator", "worktrees")
    for index in range(0, len(parts) - len(marker) + 1):
        if tuple(parts[index : index + len(marker)]) == marker and index > 0:
            candidate = Path(*parts[:index])
            if (candidate / "ops/autonomy").exists():
                return candidate
    return None


def evidence_search_roots(root: Path, rel: str) -> list[Path]:
    roots = [root]
    if rel.startswith("private/evidence/"):
        primary = plan_orchestrator_primary_root(root)
        if primary is not None and primary != root:
            roots.append(primary)
    return roots


def evidence_report_state(
    root: Path,
    rel: str,
    *,
    accepted_statuses: set[str] | None = None,
) -> tuple[bool, str | None, str | None]:
    statuses = accepted_statuses or DEFAULT_EVIDENCE_STATUSES
    candidates = []
    for base in evidence_search_roots(root, rel):
        path = base / rel
        if path.is_file():
            candidates.append((base, path))
        elif path.is_dir():
            candidates.extend((base, item) for item in path.rglob("*.json"))
    newest_status: tuple[str, str] | None = None
    for base, candidate in sorted(candidates, key=lambda item: (item[1].stat().st_mtime_ns, item[1].name), reverse=True):
        payload = load_json(candidate, {})
        if isinstance(payload, dict):
            status = str(payload.get("status") or "")
            if newest_status is None:
                newest_status = (str(candidate.relative_to(base)), status)
            if status in statuses:
                return True, str(candidate.relative_to(base)), status
    if newest_status is not None:
        path, status = newest_status
        return False, path, status
    return False, None, None


def evidence_report_exists(
    root: Path,
    rel: str,
    *,
    accepted_statuses: set[str] | None = None,
) -> tuple[bool, str | None]:
    ok, path, _status = evidence_report_state(root, rel, accepted_statuses=accepted_statuses)
    return ok, path


def has_open_blocked_external_missing_evidence(root: Path, slice_id: str) -> bool:
    failures = list(iter_jsonl(root / "ops/autonomy/failure_ledger.jsonl") or [])
    return any(
        row.get("slice") == slice_id
        and row.get("failure_class") == "blocked_external_missing_evidence"
        and row.get("open", True)
        for row in failures
    )


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

    oura_ok, oura_path, oura_status = evidence_report_state(root, "private/evidence/S03/oura_smoke", accepted_statuses={"ok"})
    blocked_oura = has_open_blocked_external_missing_evidence(root, "S03")
    checks["oura_evidence"] = oura_path
    checks["oura_evidence_status"] = oura_status
    checks["oura_blocked_external_open"] = blocked_oura
    missing_env = [name for name in ("OURA_ACCESS_TOKEN",) if not env_present(name)]
    checks["required_token_env_present"] = not missing_env
    checks["missing_env"] = missing_env
    checks["secret_values_logged"] = False
    if not oura_ok and not blocked_oura:
        errors.append("Oura evidence preflight is missing or not ok and no open blocked_external_missing_evidence failure is recorded")

    pyeight_ok, pyeight_path, pyeight_status = evidence_report_state(
        root,
        "private/evidence/S03/pyeight_smoke",
        accepted_statuses={"ok", "fallback_accepted"},
    )
    decision_ok, fallback, decision_path = pyeight_decision_state(root)
    provider_decisions = validate_provider_decisions(root, "S03")
    checks["provider_decision_validation"] = provider_decisions["status"]
    checks["provider_decision_count"] = provider_decisions["decision_count"]
    if provider_decisions["status"] != "ok":
        errors.extend(f"provider decision validation failed: {error}" for error in provider_decisions["errors"])
    checks["pyeight_evidence"] = pyeight_path
    checks["pyeight_evidence_status"] = pyeight_status
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

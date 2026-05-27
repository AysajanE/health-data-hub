#!/usr/bin/env python3
"""Verify that AutoKeel may start the S02 SWR playbook path.

This is a pre-launch readiness check, not a completion gate. It verifies the
reviewed lane decision and static safety prerequisites before S02 PO execution.
The S02 autonomous review artifacts are still required before completion.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_autonomous_review_exists import check_review
from scripts.check_no_tracked_data import check_no_tracked_data
from scripts.swr_lane_policy import validate_swr_lane_requirements


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def swr_run_is_active(payload: dict[str, Any]) -> bool:
    if str(payload.get("status", "")) in {"running", "waiting_for_review"}:
        return True
    stages = payload.get("stages")
    if not isinstance(stages, list):
        return False
    return any(
        isinstance(stage, dict) and str(stage.get("status", "")) in {"submitted", "in_progress"}
        for stage in stages
    )


def latest_swr_manifest_for_slice(root: Path, slice_id: str) -> Path | None:
    output_root = root / ".local/autokeel/swr/runs"
    if not output_root.exists():
        return None
    prefix = f"autokeel-{slice_id.lower()}-"
    candidates: list[tuple[float, Path]] = []
    for manifest in output_root.glob("*/run_manifest.json"):
        payload = load_json(manifest, {})
        if not isinstance(payload, dict):
            continue
        if not str(payload.get("run_name", "")).startswith(prefix):
            continue
        if not swr_run_is_active(payload):
            continue
        candidates.append((manifest.stat().st_mtime, manifest))
    if not candidates:
        return None
    return max(candidates, key=lambda item: item[0])[1]


def swr_evidence_matches_playbook(root: Path, s02: dict[str, Any], playbook: Path) -> tuple[bool, Path]:
    evidence_rel = s02.get("swr_evidence") or f"docs/evidence/{s02.get('slug', str(s02.get('id', 's02')).lower())}-swr-playbook-evidence.json"
    evidence = root / str(evidence_rel)
    if not playbook.exists() or not evidence.exists():
        return False, evidence
    payload = load_json(evidence, {})
    return (
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("tool") == "keel-swr"
        and payload.get("playbook") == str(playbook.relative_to(root))
        and payload.get("playbook_sha256") == file_sha256(playbook),
        evidence,
    )


def verify_s02_readiness(root: Path) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    slices = load_json(root / "ops" / "autonomy" / "slices.json", [])
    if not isinstance(slices, list):
        return {
            "status": "error",
            "errors": ["ops/autonomy/slices.json must contain a list"],
            "warnings": [],
            "checks": checks,
        }

    s01 = next((item for item in slices if item.get("id") == "S01"), None)
    s02 = next((item for item in slices if item.get("id") == "S02"), None)
    checks["s01_status"] = s01.get("status") if isinstance(s01, dict) else None
    checks["s02_status"] = s02.get("status") if isinstance(s02, dict) else None

    if not isinstance(s01, dict):
        errors.append("S01 missing from slices.json")
    elif s01.get("status") != "complete":
        errors.append(f"S01 must be complete before S02 readiness: {s01.get('status')}")

    if not isinstance(s02, dict):
        errors.append("S02 missing from slices.json")
        s02 = {}

    if isinstance(s02, dict):
        errors.extend(validate_swr_lane_requirements(root, s02))
        checks["lane_decision"] = s02.get("lane_decision")
        checks["review_artifacts"] = s02.get("review_artifacts", [])
        repair_plan = s02.get("swr_validation_repair")
        checks["swr_validation_repair"] = repair_plan
        if isinstance(repair_plan, dict):
            errors.append(
                "S02 has a pending SWR validation repair plan; do not launch a fresh SWR run. "
                "Resume only the recorded run_dir and repair_stage_id after explicit repair authorization."
            )

        review_report = check_review(root, "S02") if s02 else {"status": "error", "errors": ["S02 missing"]}
        if review_report["status"] != "ok":
            errors.extend(review_report["errors"])

        state = load_json(root / "ops" / "autonomy" / "autonomy_state.json", {})
        active_run = state.get("active_run") if isinstance(state, dict) else None
        active_swr_run = state.get("active_swr_run") if isinstance(state, dict) else None
        checks["active_run"] = active_run
        checks["active_swr_run"] = active_swr_run
        if active_run:
            errors.append(f"active_run must be null before S02 readiness: {active_run}")
        active_swr_manifest = None
        if isinstance(active_swr_run, dict) and active_swr_run.get("slice") == "S02":
            manifest_rel = active_swr_run.get("run_manifest")
            if isinstance(manifest_rel, str) and manifest_rel:
                candidate = root / manifest_rel
                payload = load_json(candidate, {})
                if isinstance(payload, dict) and swr_run_is_active(payload):
                    active_swr_manifest = candidate
                else:
                    warnings.append(
                        "S02 active_swr_run state points at a stale or non-active SWR manifest; "
                        "ignoring it for relaunch readiness."
                    )
            elif str(active_swr_run.get("status", "")) in {"running", "waiting_for_review"}:
                errors.append(f"S02 active_swr_run is active but has no run_manifest: {active_swr_run}")
            else:
                warnings.append(
                    "S02 active_swr_run state has no active manifest; ignoring it for relaunch readiness."
                )
        active_swr_manifest = active_swr_manifest or latest_swr_manifest_for_slice(root, "S02")
        checks["active_swr_manifest"] = str(active_swr_manifest.relative_to(root)) if active_swr_manifest else None
        if active_swr_manifest:
            errors.append(
                "S02 already has an active local SWR manifest; do not launch a new run: "
                f"{active_swr_manifest.relative_to(root)}"
            )

        playbook_rel = s02.get("playbook")
        if isinstance(playbook_rel, str) and playbook_rel:
            playbook = root / playbook_rel
            checks["canonical_playbook_exists"] = playbook.exists()
            if playbook.exists():
                evidence_ok, swr_evidence = swr_evidence_matches_playbook(root, s02, playbook)
                checks["swr_evidence"] = str(swr_evidence.relative_to(root))
                checks["swr_evidence_exists"] = swr_evidence.exists()
                if s02.get("lane") == "swr_preferred" and not evidence_ok:
                    errors.append(
                        f"S02 canonical playbook exists without matching SWR evidence: {s02.get('playbook')}"
                    )

    tracked = check_no_tracked_data(root)
    if tracked["status"] != "ok":
        errors.extend(tracked["errors"])
    warnings.extend(tracked.get("warnings", []))

    return {
        "status": "ok" if not errors else "error",
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify S02 AutoKeel readiness.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_s02_readiness(Path(args.root).resolve())
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

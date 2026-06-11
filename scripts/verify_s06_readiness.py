#!/usr/bin/env python3
"""Verify S06 launch readiness BEFORE any usage-billed SWR call.

SWR stage generations are the only usage-billed resource in this project;
this gate exists to stop a launch at zero marginal cost when any
deterministic precondition is missing — most critically when S05's
deliverables (which S06 builds on) are not tracked at HEAD, a condition the
paid pipeline would otherwise discover only after a full five-stage spend.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.check_no_tracked_data import check_no_tracked_data  # noqa: E402

# Surfaces S06 consumes that must be tracked at HEAD before any paid
# generation grounds itself in the repo.
REQUIRED_DEPENDENCY_SURFACES = (
    "src/model/ridge.py",
    "src/model/baseline_gate.py",
    "src/model/eval_log.py",
    "scripts/retrain_model.py",
    "docs/reviews/s05-autonomous-model-gate-review.md",
    "docs/reviews/s05-autonomous-statistical-validity-review.md",
)

REQUIRED_INPUT_DOCS = (
    "docs/gstack/s06-counterfactual-generator-autoplan.md",
    "docs/briefs/s06-counterfactual-generator.autonomous-brief.md",
)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else default


def iter_jsonl(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def tracked_at_head(root: Path, rel: str) -> bool:
    proc = subprocess.run(
        ["git", "cat-file", "-e", f"HEAD:{rel}"],
        cwd=root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode == 0


def verify_s06_readiness(root: Path) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    slices = load_json(root / "ops/autonomy/slices.json", [])
    by_id = {item.get("id"): item for item in slices if isinstance(item, dict)}
    s06 = by_id.get("S06")
    if not isinstance(s06, dict):
        return {"status": "error", "errors": ["S06 not found in slices.json"], "warnings": [], "checks": checks}

    for dep in s06.get("depends_on", []) or []:
        status = by_id.get(dep, {}).get("status")
        checks[f"{str(dep).lower()}_status"] = status
        if status != "complete":
            errors.append(f"{dep} must be complete before S06 launch: {status}")

    checks["s06_status"] = s06.get("status")
    if s06.get("status") not in {"pending", "replan_required", "waiting_for_playbook", "evidence_ready"}:
        errors.append(f"S06 is not in an actionable pre-launch status: {s06.get('status')}")

    # Dependency surfaces tracked at HEAD: the paid SWR pipeline and the PO
    # worktrees ground in HEAD, not in unmerged ship branches.
    surface_state = {}
    for rel in REQUIRED_DEPENDENCY_SURFACES:
        present = tracked_at_head(root, rel)
        surface_state[rel] = "tracked" if present else "MISSING_AT_HEAD"
        if not present:
            errors.append(
                f"S05 dependency surface is not tracked at HEAD: {rel} "
                "(land ship/s05 product paths on main before spending any SWR generation)"
            )
    checks["dependency_surfaces"] = surface_state

    for rel in REQUIRED_INPUT_DOCS:
        if not tracked_at_head(root, rel):
            errors.append(f"S06 input doc is not tracked at HEAD: {rel}")
    checks["lane_decision"] = s06.get("lane_decision")
    if not s06.get("lane_decision"):
        errors.append(
            "S06 lane_decision artifact missing: run scripts/materialize_swr_lane_decision.py S06 "
            "then scripts/record_intervention.py sync-digest BEFORE the next tick"
        )

    # Provider env + reviewer CLIs (capacity-limited but launch-critical).
    from ops.autonomy.autokeel import load_local_env  # noqa: E402

    load_local_env(root)
    checks["swr_required_env"] = {"OPENAI_API_KEY": "[SET]" if os.environ.get("OPENAI_API_KEY", "").strip() else "[UNSET]"}
    if not os.environ.get("OPENAI_API_KEY", "").strip():
        errors.append("OPENAI_API_KEY is required for the S06 keel-swr launch; secret_values_logged=false")
    reviewer_clis = {}
    for cli in ("codex", "claude"):
        located = shutil.which(cli)
        reviewer_clis[cli] = "[FOUND]" if located else "[MISSING]"
        if not located:
            errors.append(f"reviewer CLI '{cli}' not found on PATH; the SWR review lane cannot run")
    checks["reviewer_clis"] = reviewer_clis

    tracked = check_no_tracked_data(root)
    if tracked["status"] != "ok":
        errors.extend(tracked["errors"])

    failures = list(iter_jsonl(root / "ops/autonomy/failure_ledger.jsonl") or [])
    open_high = [
        row for row in failures
        if row.get("open", True)
        and row.get("severity") in {"high", "critical"}
        and row.get("slice") in {"S06", "GLOBAL"}
    ]
    checks["open_high_or_critical_failures_for_s06_or_global"] = len(open_high)
    if open_high:
        errors.append(f"open high/critical S06 or GLOBAL failures block launch: {len(open_high)}")

    state = load_json(root / "ops/autonomy/autonomy_state.json", {})
    checks["active_run"] = state.get("active_run")
    checks["active_swr_run"] = state.get("active_swr_run")
    if state.get("active_run"):
        errors.append("active_run must be null before S06 launch")
    if state.get("active_swr_run"):
        errors.append("active_swr_run must be null before S06 launch")

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify S06 launch readiness (zero-spend pre-SWR gate).")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_s06_readiness(Path(args.root).resolve())
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Verify AutoKeel control-plane invariants before or after a tick."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from ops.autonomy.autokeel import load_policy
from scripts.swr_lane_policy import validate_swr_lane_requirements


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


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git(root: Path, *argv: str) -> tuple[int, str, str]:
    proc = subprocess.run(["git", *argv], cwd=root, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def git_is_repo(root: Path) -> bool:
    code, stdout, _ = git(root, "rev-parse", "--is-inside-work-tree")
    return code == 0 and stdout == "true"


def swr_required(policy: dict[str, Any], slice_: dict[str, Any]) -> bool:
    return slice_.get("lane") == "swr_preferred" and policy.get("lanes", {}).get("swr_preferred") == "use_swr"


def swr_evidence_matches(root: Path, slice_: dict[str, Any]) -> bool:
    playbook = root / str(slice_.get("playbook") or "")
    evidence = root / str(slice_.get("swr_evidence") or f"docs/evidence/{slice_.get('slug', str(slice_.get('id', '')).lower())}-swr-playbook-evidence.json")
    if not playbook.exists() or not evidence.exists():
        return False
    payload = load_json(evidence, {})
    return (
        isinstance(payload, dict)
        and payload.get("status") == "ok"
        and payload.get("tool") == "keel-swr"
        and payload.get("playbook") == str(playbook.relative_to(root))
        and payload.get("playbook_sha256") == file_sha256(playbook)
    )


def completed_slice_ids(slices: list[Any], state: dict[str, Any]) -> set[str]:
    completed = {str(item.get("id")) for item in slices if isinstance(item, dict) and item.get("status") == "complete"}
    state_completed = state.get("completed_slices") if isinstance(state, dict) else []
    if isinstance(state_completed, list):
        completed.update(str(item) for item in state_completed)
    return completed


def next_actionable_slice_id(slices: list[Any], state: dict[str, Any]) -> str | None:
    completed = completed_slice_ids(slices, state)
    for slice_ in slices:
        if not isinstance(slice_, dict) or not slice_.get("required"):
            continue
        if slice_.get("status") in {"blocked", "blocked_external"}:
            continue
        deps = set(str(item) for item in slice_.get("depends_on", []))
        if deps and not deps.issubset(completed):
            continue
        if str(slice_.get("status", "pending")) in {
            "pending",
            "replan_required",
            "ready",
            "evidence_ready",
            "blocked_compile_inputs",
            "waiting_for_playbook",
        }:
            return str(slice_.get("id") or "")
    return None


def lane_decision_required_now(
    root: Path,
    slice_: dict[str, Any],
    active_run: Any,
    active_swr: Any,
    next_actionable_id: str | None,
) -> bool:
    slice_id = str(slice_.get("id") or "")
    if slice_.get("status") == "complete":
        return True
    if isinstance(active_run, dict) and active_run.get("slice") == slice_id:
        return True
    if isinstance(active_swr, dict) and active_swr.get("slice") == slice_id:
        return True
    if next_actionable_id == slice_id:
        return True
    playbook = root / str(slice_.get("playbook") or "")
    return playbook.exists()


def dirty_paths(root: Path) -> list[str]:
    code, stdout, _ = git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if code != 0:
        return []
    paths: list[str] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        if len(line) > 3 and line[2] == " ":
            path = line[3:]
        elif len(line) > 2 and line[1] == " ":
            path = line[2:]
        else:
            path = line[3:] if len(line) > 3 else line
        if " -> " in path:
            paths.extend(part.strip('"') for part in path.split(" -> ", 1))
        else:
            paths.append(path.strip('"'))
    return paths


def approved_dirty_path(path: str) -> bool:
    return (
        path.startswith("ops/autonomy/")
        or path.startswith("docs/briefs/")
        or path.startswith("docs/evidence/")
        or path.startswith("docs/gstack/")
        or path.startswith("docs/playbooks/")
        or path.startswith(".local/autokeel/")
    )


def verify_autokeel_invariants(root: Path) -> dict[str, Any]:
    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}
    policy = load_policy(root / "ops/autonomy/policy.yaml")
    slices = load_json(root / "ops/autonomy/slices.json", [])
    state = load_json(root / "ops/autonomy/autonomy_state.json", {})
    if not isinstance(slices, list):
        return {"status": "error", "errors": ["slices.json must contain a list"], "warnings": [], "checks": checks}

    active_run = state.get("active_run") if isinstance(state, dict) else None
    active_swr = state.get("active_swr_run") if isinstance(state, dict) else None
    checks["active_run"] = active_run
    checks["active_swr_run"] = active_swr
    if isinstance(active_run, dict) and isinstance(active_swr, dict) and active_run.get("slice") == active_swr.get("slice"):
        errors.append("active_run and active_swr_run are both set for the same slice")

    repo = git_is_repo(root)
    checks["git_repo"] = repo
    next_actionable_id = next_actionable_slice_id(slices, state if isinstance(state, dict) else {})
    checks["next_actionable_slice"] = next_actionable_id
    for slice_ in slices:
        if not isinstance(slice_, dict):
            continue
        slice_id = str(slice_.get("id") or "")
        if slice_.get("status") == "complete":
            for key in ("run_id", "ship_branch", "ship_commit"):
                if not slice_.get(key):
                    errors.append(f"{slice_id}: completed slice missing {key}")
            if repo and slice_.get("ship_branch") and slice_.get("ship_commit"):
                code, branch_head, stderr = git(root, "rev-parse", f"{slice_['ship_branch']}^{{commit}}")
                if code != 0:
                    errors.append(f"{slice_id}: ship_branch is not reachable: {stderr}")
                elif branch_head != str(slice_["ship_commit"]):
                    errors.append(f"{slice_id}: ship_branch HEAD differs from ship_commit")
        if (
            str(slice_.get("risk", "")).lower() == "high"
            and slice_.get("lane") == "swr_preferred"
            and lane_decision_required_now(root, slice_, active_run, active_swr, next_actionable_id)
        ):
            errors.extend(validate_swr_lane_requirements(root, slice_))
        if swr_required(policy, slice_):
            playbook = root / str(slice_.get("playbook") or "")
            evidence_ok = swr_evidence_matches(root, slice_)
            if slice_.get("status") == "complete" and not evidence_ok:
                errors.append(f"{slice_id}: complete SWR-required slice lacks matching SWR evidence")
            if slice_.get("status") != "complete" and playbook.exists() and not evidence_ok:
                errors.append(f"{slice_id}: canonical playbook exists without matching SWR evidence")

    failures = list(iter_jsonl(root / "ops/autonomy/failure_ledger.jsonl") or [])
    open_high = [row for row in failures if row.get("open", True) and row.get("severity") in {"high", "critical"}]
    checks["open_high_failures"] = len(open_high)
    if open_high:
        errors.append(f"open high/critical failures exist: {len(open_high)}")

    if repo:
        code, tracked_lock, _ = git(root, "ls-files", "ops/autonomy/.autokeel.lock")
        if code == 0 and tracked_lock:
            errors.append("runtime lock file is tracked: ops/autonomy/.autokeel.lock")
        disallowed_dirty = [path for path in dirty_paths(root) if not approved_dirty_path(path)]
        checks["disallowed_dirty_paths"] = disallowed_dirty[:20]
        if disallowed_dirty:
            errors.append("operator checkout has non-approved dirty paths: " + ", ".join(disallowed_dirty[:20]))
    else:
        warnings.append("not a git repository; git-backed invariants skipped")

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify AutoKeel invariants.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_autokeel_invariants(Path(args.root).resolve())
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

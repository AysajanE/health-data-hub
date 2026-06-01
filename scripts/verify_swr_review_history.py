#!/usr/bin/env python3
"""Verify SWR review history before AutoKeel resumes or consumes a SWR run."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.validate_swr_review_bundle import validate_swr_review_bundle


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def iter_stage_dicts(value: Any):
    if isinstance(value, dict):
        if any(key in value for key in ("stage_id", "id", "review_bundle_path")):
            yield value
        for child in value.values():
            yield from iter_stage_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from iter_stage_dicts(child)


def repo_path(root: Path, rel_or_abs: str) -> Path | None:
    if not rel_or_abs:
        return None
    path = Path(rel_or_abs)
    if not path.is_absolute():
        path = root / path
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return path


def verify_swr_review_history(root: Path, manifest_path: Path) -> dict[str, Any]:
    root = root.resolve()
    manifest_path = manifest_path if manifest_path.is_absolute() else root / manifest_path
    errors: list[str] = []
    warnings: list[str] = []
    checks: list[dict[str, Any]] = []

    manifest = load_json(manifest_path, {})
    if not isinstance(manifest, dict):
        return {"status": "error", "errors": [f"manifest is not a JSON object: {manifest_path}"], "warnings": [], "checks": []}

    manifest_run_id = str(manifest.get("run_id") or manifest.get("id") or "")
    for stage in iter_stage_dicts(manifest):
        stage_id = str(stage.get("stage_id") or stage.get("id") or "")
        bundle_rel = str(stage.get("review_bundle_path") or stage.get("review_bundle") or "")
        if not stage_id or not bundle_rel:
            continue

        bundle_path = repo_path(root, bundle_rel)
        if bundle_path is None or not bundle_path.exists():
            errors.append(f"{stage_id}: review bundle missing or outside repo: {bundle_rel}")
            continue

        bundle = load_json(bundle_path, {})
        bundle_run = str(bundle.get("run_id") or bundle.get("source_run_id") or "")
        bundle_stage = str(bundle.get("stage_id") or bundle.get("source_stage_id") or "")

        if manifest_run_id and bundle_run and bundle_run != manifest_run_id:
            errors.append(f"{stage_id}: review bundle run_id mismatch: {bundle_run} != {manifest_run_id}")
            continue

        if bundle_stage and bundle_stage != stage_id:
            warnings.append(
                f"{stage_id}: review_bundle_path points to consumed handoff bundle for {bundle_stage}; "
                "skipped as current-stage review history"
            )
            continue

        report = validate_swr_review_bundle(bundle_path, root=root)
        checks.append({"stage_id": stage_id, "bundle": str(bundle_path.relative_to(root)), "status": report["status"]})
        if report["status"] != "ok":
            errors.extend(f"{stage_id}: {error}" for error in report["errors"])

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify SWR review history for one run manifest.")
    parser.add_argument("manifest")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_swr_review_history(Path(args.root), Path(args.manifest))
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

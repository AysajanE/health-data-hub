#!/usr/bin/env python3
"""Validate an AutoKeel SWR review bundle before next-stage continuation."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any


REQUIRED_REVIEWERS = {"operator_codex", "codex_review_agent", "claude_review_agent"}


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


def repo_path(root: Path, value: str) -> Path | None:
    if not value:
        return None
    path = Path(value)
    if not path.is_absolute():
        path = root / path
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    return path


def validate_swr_review_bundle(path: Path, root: Path | None = None) -> dict[str, Any]:
    root = (root or Path.cwd()).resolve()
    errors: list[str] = []
    bundle = load_json(path, {})
    if not isinstance(bundle, dict):
        return {"status": "error", "errors": [f"bundle is not a JSON object: {path}"], "warnings": []}

    required = [
        "slice",
        "run_id",
        "stage_id",
        "response_id",
        "input_sha256",
        "output_sha256",
        "reviewer_results",
        "consolidated_verdict",
        "accepted_at",
    ]
    for key in required:
        if not bundle.get(key):
            errors.append(f"missing required field: {key}")

    reviewers = bundle.get("reviewer_results")
    seen_reviewers: set[str] = set()
    if not isinstance(reviewers, list):
        errors.append("reviewer_results must be a list")
    else:
        for index, item in enumerate(reviewers, start=1):
            if not isinstance(item, dict):
                errors.append(f"reviewer_results[{index}] must be an object")
                continue
            reviewer = str(item.get("reviewer") or "")
            verdict = str(item.get("verdict") or "")
            seen_reviewers.add(reviewer)
            if reviewer not in REQUIRED_REVIEWERS:
                errors.append(f"unknown reviewer: {reviewer}")
            if verdict != "pass":
                errors.append(f"reviewer {reviewer or index} verdict must be pass")
    missing_reviewers = sorted(REQUIRED_REVIEWERS - seen_reviewers)
    if missing_reviewers:
        errors.append("missing reviewer_results for: " + ", ".join(missing_reviewers))

    if bundle.get("consolidated_verdict") != "pass":
        errors.append("consolidated_verdict must be pass")

    input_rel = str(bundle.get("input_artifact") or bundle.get("input_manifest_json") or "")
    output_rel = str(bundle.get("response_artifact_json") or bundle.get("output_artifact") or "")
    hash_checks = [
        ("input_sha256", input_rel),
        ("output_sha256", output_rel),
    ]
    for key, rel in hash_checks:
        if not rel:
            errors.append(f"missing artifact path for {key}")
            continue
        artifact = repo_path(root, rel)
        if artifact is None or not artifact.exists():
            errors.append(f"artifact path for {key} does not exist under repo: {rel}")
            continue
        expected = str(bundle.get(key) or "")
        if expected and expected != file_sha256(artifact):
            errors.append(f"{key} does not match artifact: {rel}")

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": []}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an SWR review bundle.")
    parser.add_argument("bundle")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = validate_swr_review_bundle(Path(args.bundle), root=Path(args.root).resolve())
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

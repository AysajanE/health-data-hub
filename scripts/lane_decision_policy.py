#!/usr/bin/env python3
"""Validate AutoKeel lane decision artifacts."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


DECISIONS = {"use_swr", "compile_with_keel_compile", "block"}
RISKS = {"low", "medium", "high"}
VERDICTS = {"pass", "fail"}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_lane_decision_payload(payload: Any, slice_: dict[str, Any], rel_path: str) -> list[str]:
    slice_id = str(slice_.get("id", "<unknown>"))
    errors: list[str] = []
    if not isinstance(payload, dict):
        return [f"{slice_id}: lane_decision artifact must contain an object: {rel_path}"]

    required = ["created_at", "status", "slice", "lane", "decision", "risk", "review_artifacts", "commands", "verdict"]
    for key in required:
        if key not in payload:
            errors.append(f"{slice_id}: lane_decision missing required field {key}: {rel_path}")

    if payload.get("slice") != slice_id:
        errors.append(f"{slice_id}: lane_decision slice mismatch: {payload.get('slice')!r}")
    if payload.get("lane") != slice_.get("lane"):
        errors.append(f"{slice_id}: lane_decision lane mismatch: {payload.get('lane')!r}")
    if payload.get("risk") != slice_.get("risk"):
        errors.append(f"{slice_id}: lane_decision risk mismatch: {payload.get('risk')!r}")

    decision = payload.get("decision")
    verdict = payload.get("verdict")
    if payload.get("status") != "accepted":
        errors.append(f"{slice_id}: lane_decision status must be accepted: {rel_path}")
    if decision not in DECISIONS:
        errors.append(f"{slice_id}: lane_decision has unsupported decision: {decision!r}")
    elif slice_.get("risk") == "high" and slice_.get("lane") == "swr_preferred" and decision != "use_swr":
        errors.append(f"{slice_id}: high-risk swr_preferred lane_decision must be use_swr; got {decision!r}")
    if payload.get("risk") not in RISKS:
        errors.append(f"{slice_id}: lane_decision has unsupported risk: {payload.get('risk')!r}")
    if verdict not in VERDICTS:
        errors.append(f"{slice_id}: lane_decision has unsupported verdict: {verdict!r}")
    if decision == "block":
        errors.append(f"{slice_id}: lane_decision verdict blocks execution: {rel_path}")
    if verdict != "pass":
        errors.append(f"{slice_id}: lane_decision verdict is not pass: {rel_path}")

    reviews = payload.get("review_artifacts")
    if not isinstance(reviews, list) or not all(isinstance(item, str) and item for item in reviews):
        errors.append(f"{slice_id}: lane_decision review_artifacts must be a non-empty string list: {rel_path}")
    else:
        if slice_.get("risk") == "high" and len(reviews) < 2:
            errors.append(f"{slice_id}: high-risk lane_decision requires at least two review artifact paths: {rel_path}")
        slice_reviews = set(slice_.get("review_artifacts", []))
        decision_reviews = set(reviews)
        if slice_reviews and decision_reviews != slice_reviews:
            errors.append(f"{slice_id}: lane_decision review_artifacts must match slice review_artifacts")

    commands = payload.get("commands")
    if not isinstance(commands, list) or not commands:
        errors.append(f"{slice_id}: lane_decision commands must be a non-empty list: {rel_path}")
    elif isinstance(commands, list):
        for index, row in enumerate(commands, start=1):
            if not isinstance(row, dict):
                errors.append(f"{slice_id}: lane_decision command row {index} is not an object: {rel_path}")
                continue
            if not isinstance(row.get("command"), str) or not row.get("command"):
                errors.append(f"{slice_id}: lane_decision command row {index} missing command: {rel_path}")
            if not isinstance(row.get("exit_code"), int):
                errors.append(f"{slice_id}: lane_decision command row {index} missing integer exit_code: {rel_path}")
            elif row.get("exit_code") != 0 and verdict == "pass":
                errors.append(f"{slice_id}: lane_decision command row {index} nonzero exit_code: {rel_path}")
            for field in ("stdout_tail", "stderr_tail"):
                value = row.get(field)
                if value is not None and not isinstance(value, str):
                    errors.append(f"{slice_id}: lane_decision command row {index} {field} must be a string: {rel_path}")

    return errors


def validate_lane_decision(root: Path, slice_: dict[str, Any]) -> list[str]:
    if slice_.get("lane") != "swr_preferred" or slice_.get("risk") != "high":
        return []

    slice_id = str(slice_.get("id", "<unknown>"))
    decision_rel = slice_.get("lane_decision")
    if not decision_rel:
        return [f"{slice_id}: high-risk swr_preferred slice missing lane_decision artifact"]

    rel = str(decision_rel)
    path = root / rel
    if Path(rel).is_absolute():
        return [f"{slice_id}: lane_decision path must be repo-relative: {rel}"]
    if not path.exists():
        return [f"{slice_id}: lane_decision artifact missing: {rel}"]
    if not path.is_file():
        return [f"{slice_id}: lane_decision artifact is not a file: {rel}"]

    try:
        payload = load_json(path)
    except json.JSONDecodeError as exc:
        return [f"{slice_id}: lane_decision is not valid JSON: {rel}: {exc}"]

    return validate_lane_decision_payload(payload, slice_, rel)

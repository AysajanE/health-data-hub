#!/usr/bin/env python3
"""Additional safeguards for high-risk SWR-preferred slices."""

from __future__ import annotations

from pathlib import Path
from typing import Any


def validate_swr_lane_requirements(root: Path, slice_: dict[str, Any]) -> list[str]:
    if slice_.get("lane") != "swr_preferred" or slice_.get("risk") != "high":
        return []

    slice_id = slice_.get("id", "<unknown>")
    errors: list[str] = []

    decision_rel = slice_.get("lane_decision")
    if not decision_rel:
        errors.append(f"{slice_id}: high-risk swr_preferred slice missing lane_decision artifact")
    elif not (root / str(decision_rel)).exists():
        errors.append(f"{slice_id}: lane_decision artifact missing: {decision_rel}")

    review_artifacts = slice_.get("review_artifacts", [])
    if not isinstance(review_artifacts, list) or len(review_artifacts) < 2:
        errors.append(f"{slice_id}: high-risk swr_preferred slice requires at least two review artifacts")

    return errors

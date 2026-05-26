#!/usr/bin/env python3
"""Additional safeguards for high-risk SWR-preferred slices."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from scripts.lane_decision_policy import validate_lane_decision


def validate_swr_lane_requirements(root: Path, slice_: dict[str, Any]) -> list[str]:
    if slice_.get("lane") != "swr_preferred" or slice_.get("risk") != "high":
        return []

    slice_id = slice_.get("id", "<unknown>")
    errors = validate_lane_decision(root, slice_)

    review_artifacts = slice_.get("review_artifacts", [])
    if not isinstance(review_artifacts, list) or len(review_artifacts) < 2:
        errors.append(f"{slice_id}: high-risk swr_preferred slice requires at least two review artifacts")

    return errors

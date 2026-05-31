from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, TypeVar


REPO_ROOT = Path(__file__).resolve().parents[2]

_RowT = TypeVar("_RowT")


@dataclass(frozen=True)
class SleepProviderPolicy:
    active_sleep_source: str
    eight_sleep_state: str
    eight_sleep_allowed_for_features: bool = False


def _load_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _provider_name(payload: Mapping[str, Any]) -> str:
    return str(payload.get("provider") or "").lower().replace("-", "_")


def load_sleep_provider_policy(root: Path = REPO_ROOT) -> SleepProviderPolicy:
    """Load the active v1 sleep-provider decision.

    Safety invariant: under the current S03 decision, Oura is the only active
    v1 feature source and 8 Sleep/pyEight may only be represented as fallback.
    """

    root = root.resolve()
    decisions_dir = root / "ops/autonomy/decisions"
    active_fallback_paths: list[Path] = []
    active_include_paths: list[Path] = []

    for path in sorted(decisions_dir.glob("*.json"), key=lambda item: item.name):
        payload = _load_json(path)
        if payload is None:
            continue
        if payload.get("slice") != "S03" or _provider_name(payload) not in {"pyeight", "8sleep", "eight_sleep"}:
            continue
        if payload.get("superseded_by"):
            continue

        status = payload.get("status")
        action = payload.get("action")
        if status == "fallback_accepted" and payload.get("fallback_active") is True and action == "oura_only_v1":
            active_fallback_paths.append(path)
        elif status == "ok" and payload.get("fallback_active") is False:
            active_include_paths.append(path)

    if active_fallback_paths and active_include_paths:
        fallback_names = ", ".join(str(path.relative_to(root)) for path in active_fallback_paths)
        include_names = ", ".join(str(path.relative_to(root)) for path in active_include_paths)
        raise ValueError(
            "conflicting active S03 sleep-provider decisions: "
            f"fallback={fallback_names}; include={include_names}"
        )
    if not active_fallback_paths:
        if active_include_paths:
            include_names = ", ".join(str(path.relative_to(root)) for path in active_include_paths)
            raise ValueError(f"8 Sleep include decision is not valid for v1 fallback-only policy: {include_names}")
        raise ValueError("missing active S03 8 Sleep fallback decision")

    return SleepProviderPolicy(
        active_sleep_source="oura",
        eight_sleep_state="fallback_active",
        eight_sleep_allowed_for_features=False,
    )


def _row_source(row: Any) -> str | None:
    if isinstance(row, Mapping):
        value = row.get("source")
    else:
        value = getattr(row, "source", None)
    return value if isinstance(value, str) else None


def eligible_sleep_rows_for_v1(rows: Iterable[_RowT], policy: SleepProviderPolicy) -> list[_RowT]:
    """Return sleep rows eligible for v1 model features.

    Safety invariant: 8 Sleep rows are not an Oura fallback, not a merge source,
    and not counted as active v1 sleep evidence under the S03 fallback decision.
    """

    if policy.active_sleep_source != "oura":
        raise ValueError(f"unsupported active v1 sleep source: {policy.active_sleep_source}")
    if policy.eight_sleep_state != "fallback_active":
        raise ValueError(f"8 Sleep must be fallback_active for v1: {policy.eight_sleep_state}")
    if policy.eight_sleep_allowed_for_features:
        raise ValueError("8 Sleep feature use is disabled for v1")
    return [row for row in rows if _row_source(row) == policy.active_sleep_source]


__all__ = [
    "SleepProviderPolicy",
    "eligible_sleep_rows_for_v1",
    "load_sleep_provider_policy",
]

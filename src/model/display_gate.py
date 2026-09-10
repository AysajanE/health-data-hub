from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import Enum
from math import isclose, isfinite
from types import MappingProxyType
from typing import Mapping

from src.model.counterfactual import FEATURE_POLICY, MUTABLE_FEATURE, CounterfactualConfig
from src.model.eval_log import confidence_label_for_interval


class DisplayState(str, Enum):
    COLLECTING = "collecting"
    GATE_FAILED = "gate_failed"
    INSUFFICIENT_STABLE_SIGNAL = "insufficient_stable_signal"
    SHOW = "show"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class ContributorView:
    feature_name: str
    display_name: str
    value_text: str
    contribution: float
    direction_text: str | None
    low_confidence: bool


COUNTERFACTUAL_FRAMING = "model-estimated change in your past data"
COUNTERFACTUAL_CAVEAT = "correlation, not proven causation"
# The presentation layer re-checks the generator's own floors so a malformed
# payload can never render a comparison below 7 hours or below materiality.
COUNTERFACTUAL_SAFE_FLOOR = float(FEATURE_POLICY[MUTABLE_FEATURE].safe_floor or 0.0)
COUNTERFACTUAL_MIN_MEDIAN_DELTA = CounterfactualConfig().min_median_delta
COUNTERFACTUAL_UNCERTAIN_REASONS = frozenset(
    {
        "empty_candidate_envelope",
        "no_plausible_candidate",
        "delta_interval_not_positive",
        "delta_below_materiality_floor",
    }
)


@dataclass(frozen=True)
class CounterfactualView:
    """Presentation of the retrospective sleep comparison for the insight date."""

    status: str  # "available" | "suppressed" | "unavailable"
    reason: str | None
    actual_text: str | None
    comparison_text: str | None
    delta_low: float | None
    delta_median: float | None
    delta_high: float | None
    message: str


@dataclass(frozen=True)
class InsightView:
    state: DisplayState
    message: str
    insight_date: date | None
    logged_feeling: int | None
    model_ready_days: int
    min_model_rows: int
    n_model: int
    gate_reason: str | None
    contributors: tuple[ContributorView, ...]
    interval_low: float | None
    interval_high: float | None
    confidence_label: str | None
    model_version: str | None
    trained_through_date: date | None
    counterfactual: CounterfactualView | None = None


FEATURE_DISPLAY: Mapping[str, str] = MappingProxyType(
    {
        "total_sleep_min": "Total sleep",
        "hrv_z": "HRV (z-score vs baseline)",
        "deep_sleep_pct": "Deep sleep %",
        "prior_day_feeling": "Yesterday's feeling",
    }
)


def format_feature_value(
    feature_name: str, value: float, *, hrv_avg_ms: float | None = None
) -> str:
    if feature_name == "total_sleep_min":
        hours, minutes = divmod(round(value), 60)
        return f"{hours}h {minutes:02d}m"
    if feature_name == "hrv_z":
        value_text = f"{value:.1f}".replace("-", "−")
        if hrv_avg_ms is not None:
            value_text += f" ({hrv_avg_ms:.0f} ms)"
        return value_text
    if feature_name == "deep_sleep_pct":
        return f"{value * 100:.0f}%"
    if feature_name == "prior_day_feeling":
        return f"{round(value)} / 10"
    raise ValueError("Unknown model feature")


def direction_text(
    feature_name: str, value: float, usual: float | None
) -> str | None:
    if feature_name == "hrv_z":
        if value < -0.25:
            return "▼ below your prior baseline"
        if value > 0.25:
            return "▲ above your prior baseline"
        return "≈ your prior baseline"
    if feature_name not in {"total_sleep_min", "deep_sleep_pct"} or usual is None:
        return None
    usual_text = format_feature_value(feature_name, usual)
    distance, band = abs(value - usual), abs(usual) * 0.05
    if distance <= band or isclose(distance, band, rel_tol=1e-12, abs_tol=1e-12):
        return f"≈ your usual {usual_text}"
    if value < usual:
        return f"▼ below your usual {usual_text}"
    return f"▲ above your usual {usual_text}"


def _number(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("Expected a number")
    result = float(value)
    if not isfinite(result):
        raise ValueError("Expected a finite number")
    return result


def _optional_string(value: object) -> str | None:
    if value is not None and not isinstance(value, str):
        raise ValueError("Expected text")
    return value


def _unavailable_counterfactual() -> CounterfactualView:
    return CounterfactualView(
        status="unavailable",
        reason=None,
        actual_text=None,
        comparison_text=None,
        delta_low=None,
        delta_median=None,
        delta_high=None,
        message="Insufficient stable signal for a sleep comparison.",
    )


def build_counterfactual_view(payload: object) -> CounterfactualView:
    """Turn the eval record's ``latest_counterfactual`` payload into display text.

    The generator returns structured values; this is the only place that turns
    them into words, and every word stays explanation-framed. Anything
    malformed collapses to the safe "insufficient stable signal" message.
    """

    if not isinstance(payload, Mapping):
        return _unavailable_counterfactual()
    status = payload.get("status")
    if status == "suppressed":
        reason = payload.get("suppression_reason")
        reason = reason if isinstance(reason, str) else None
        if reason == "actual_at_or_above_recent_median":
            message = (
                "No useful sleep-increase comparison: that night's sleep was already at your "
                "normal upper range."
            )
        elif reason in COUNTERFACTUAL_UNCERTAIN_REASONS:
            message = "The estimated change is too uncertain to call useful."
        else:
            message = "Insufficient stable signal for a sleep comparison."
        return CounterfactualView(
            status="suppressed",
            reason=reason,
            actual_text=None,
            comparison_text=None,
            delta_low=None,
            delta_median=None,
            delta_high=None,
            message=message,
        )
    if status != "available":
        return _unavailable_counterfactual()
    try:
        detail = payload["counterfactual"]
        if not isinstance(detail, Mapping) or detail.get("feature_name") != MUTABLE_FEATURE:
            raise ValueError("Unsupported counterfactual feature")
        if detail.get("framing_label") != COUNTERFACTUAL_FRAMING or detail.get("caveat") != COUNTERFACTUAL_CAVEAT:
            raise ValueError("Counterfactual language mismatch")
        if detail.get("direction") not in (None, "increase_only"):
            raise ValueError("Unsupported counterfactual direction")
        actual = _number(detail["actual_value"])
        comparison = _number(detail["comparison_value"])
        low = _number(detail["model_delta_low"])
        median_delta = _number(detail["median_delta"])
        high = _number(detail["model_delta_high"])
        if comparison <= actual or comparison < COUNTERFACTUAL_SAFE_FLOOR:
            raise ValueError("Counterfactual violates the increase-only or safe-floor contract")
        if low <= 0 or median_delta < COUNTERFACTUAL_MIN_MEDIAN_DELTA or high < median_delta or median_delta < low:
            raise ValueError("Counterfactual interval violates the materiality contract")
    except (KeyError, TypeError, ValueError, OverflowError):
        return _unavailable_counterfactual()
    return CounterfactualView(
        status="available",
        reason=None,
        actual_text=format_feature_value("total_sleep_min", actual),
        comparison_text=format_feature_value("total_sleep_min", comparison),
        delta_low=low,
        delta_median=median_delta,
        delta_high=high,
        message="",
    )


def build_insight_view(
    *,
    latest_record: Mapping | None,
    model_ready_days: int,
    usual_values: Mapping[str, float],
    min_model_rows: int = 37,
) -> InsightView:
    view = InsightView(
        state=DisplayState.COLLECTING,
        message=(
            f"Collecting model-ready days: {model_ready_days}/{min_model_rows}. "
            f"Insights begin once we have {min_model_rows} model-ready days."
        ),
        insight_date=None,
        logged_feeling=None,
        model_ready_days=model_ready_days,
        min_model_rows=min_model_rows,
        n_model=0,
        gate_reason=None,
        contributors=(),
        interval_low=None,
        interval_high=None,
        confidence_label=None,
        model_version=None,
        trained_through_date=None,
    )
    if latest_record is None:
        return view
    try:
        if not isinstance(latest_record, Mapping):
            raise ValueError("Expected an eval record")
        status = latest_record["status"]
        n_model = latest_record["n_model"]
        if not isinstance(status, str):
            raise ValueError("Expected a status")
        if isinstance(n_model, bool) or not isinstance(n_model, int) or n_model < 0:
            raise ValueError("Expected a model row count")
        feeling = latest_record["latest_logged_feeling"]
        if feeling is not None:
            feeling = _number(feeling)
        trained_date = latest_record.get("trained_through_date")
        if trained_date is not None:
            if not isinstance(trained_date, str):
                raise ValueError("Expected a training date")
            trained_date = date.fromisoformat(trained_date)
        view = replace(
            view,
            n_model=n_model,
            model_version=_optional_string(latest_record.get("model_version")),
            trained_through_date=trained_date,
        )
        if status != "trained" or n_model < min_model_rows or feeling is None:
            return view

        gate_passed = latest_record["baseline_gate_passed"]
        if not isinstance(gate_passed, bool):
            raise ValueError("Expected a baseline gate result")
        view = replace(
            view, gate_reason=_optional_string(latest_record.get("baseline_gate_reason"))
        )
        if not gate_passed:
            return replace(
                view,
                state=DisplayState.GATE_FAILED,
                message="Model is not yet better than a simple baseline. Collecting more data.",
            )

        contributions = latest_record["latest_contributions"]
        if not isinstance(contributions, list):
            raise ValueError("Expected contributions")
        metadata = latest_record.get("latest_display_metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("Expected display metadata")
        hrv_avg_ms = metadata.get("hrv_avg_ms")
        if hrv_avg_ms is not None:
            hrv_avg_ms = _number(hrv_avg_ms)
        contributors = []
        feature_order = tuple(FEATURE_DISPLAY)
        for item in contributions:
            if not isinstance(item, Mapping):
                raise ValueError("Expected a contribution")
            feature_name = item["feature_name"]
            if not isinstance(feature_name, str) or feature_name not in FEATURE_DISPLAY:
                raise ValueError("Unknown model feature")
            stability = item["stability_label"]
            if stability not in ("stable", "low_confidence_signal", "suppressed"):
                raise ValueError("Unknown stability tier")
            if stability == "suppressed":
                continue
            value = _number(item["feature_value"])
            contribution = _number(item["contribution"])
            usual = usual_values.get(feature_name)
            if usual is not None:
                usual = _number(usual)
            contributors.append(
                ContributorView(
                    feature_name=feature_name,
                    display_name=FEATURE_DISPLAY[feature_name],
                    value_text=format_feature_value(feature_name, value, hrv_avg_ms=hrv_avg_ms),
                    contribution=contribution,
                    direction_text=direction_text(feature_name, value, usual),
                    low_confidence=stability == "low_confidence_signal",
                )
            )
        contributors.sort(
            key=lambda item: (-abs(item.contribution), feature_order.index(item.feature_name))
        )
        if not contributors:
            return replace(
                view,
                state=DisplayState.INSUFFICIENT_STABLE_SIGNAL,
                message="Insufficient stable signal. Collecting more data.",
            )
        if trained_date is None:
            raise ValueError("Missing training date")
        interval = latest_record["latest_prediction_interval"]
        if not isinstance(interval, Mapping):
            raise ValueError("Expected an interval")
        low = _number(interval["low"])
        high = _number(interval["high"])
        width = _number(interval["full_width"])
        if low > high or width < 0:
            raise ValueError("Invalid interval")
        confidence = latest_record.get("confidence_label")
        if confidence is None:
            confidence = confidence_label_for_interval(width)
        if confidence not in ("high", "medium", "low"):
            raise ValueError("Unknown confidence bucket")
        return replace(
            view,
            state=DisplayState.SHOW,
            message="",
            insight_date=trained_date,
            logged_feeling=int(round(feeling)),
            contributors=tuple(contributors),
            interval_low=low,
            interval_high=high,
            confidence_label=confidence,
            counterfactual=build_counterfactual_view(latest_record.get("latest_counterfactual")),
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return replace(
            view,
            state=DisplayState.UNAVAILABLE,
            message="Model output is unavailable right now.",
        )


__all__ = [
    "COUNTERFACTUAL_CAVEAT",
    "COUNTERFACTUAL_FRAMING",
    "ContributorView",
    "CounterfactualView",
    "DisplayState",
    "FEATURE_DISPLAY",
    "InsightView",
    "build_counterfactual_view",
    "build_insight_view",
    "direction_text",
    "format_feature_value",
]

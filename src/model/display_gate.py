from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import Enum
from math import isclose, isfinite
from types import MappingProxyType
from typing import Mapping

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
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return replace(
            view,
            state=DisplayState.UNAVAILABLE,
            message="Model output is unavailable right now.",
        )


__all__ = [
    "ContributorView",
    "DisplayState",
    "FEATURE_DISPLAY",
    "InsightView",
    "build_insight_view",
    "direction_text",
    "format_feature_value",
]

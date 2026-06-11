from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from src.model.ridge import MODEL_FEATURES, RidgePredictor, _stability_label


def _build_rows(count: int, *, residual_scale: float = 0.0) -> tuple[list[dict[str, float | str]], list[float]]:
    rows: list[dict[str, float | str]] = []
    targets: list[float] = []
    residual_pattern = (0.0, 0.05, -0.04, 0.03, -0.02)

    for offset in range(count):
        total_sleep_min = 390.0 + float((offset * 17) % 120)
        hrv_z = -1.5 + float((offset * 5) % 14) / 4.0
        deep_sleep_pct = 0.14 + float((offset * 7) % 10) / 100.0
        prior_day_feeling = 3.0 + float((offset * 3) % 7)
        residual = residual_pattern[offset % len(residual_pattern)] * residual_scale
        target = (
            1.25
            + (0.010 * total_sleep_min)
            + (0.90 * hrv_z)
            + (7.50 * deep_sleep_pct)
            + (0.55 * prior_day_feeling)
            + residual
        )
        rows.append(
            {
                "feature_date": (date(2026, 1, 1) + timedelta(days=offset)).isoformat(),
                "total_sleep_min": total_sleep_min,
                "hrv_z": hrv_z,
                "deep_sleep_pct": deep_sleep_pct,
                "prior_day_feeling": prior_day_feeling,
                "hrv_avg_ms": 1000.0 + float(offset * 50),
            }
        )
        targets.append(target)

    return rows, targets


def test_ridge_predictor_rejects_rows_missing_required_v1_features() -> None:
    rows, targets = _build_rows(8)
    rows[0].pop("deep_sleep_pct")

    with pytest.raises(ValueError, match="exactly the v1 features"):
        RidgePredictor().fit(rows, targets)


def test_ridge_predictor_ignores_hrv_avg_ms_and_returns_linear_contributions() -> None:
    rows, targets = _build_rows(48, residual_scale=0.2)
    predictor = RidgePredictor().fit(rows, targets)

    baseline_row = dict(rows[-1])
    mutated_row = dict(baseline_row)
    mutated_row["hrv_avg_ms"] = 999999.0

    baseline_prediction = predictor.predict([baseline_row])[0]
    mutated_prediction = predictor.predict([mutated_row])[0]
    explanation = predictor.explain([baseline_row])[0]
    contributions = {item.feature_name: item.contribution for item in explanation.contributions}

    assert baseline_prediction == pytest.approx(mutated_prediction)
    assert tuple(contributions) == MODEL_FEATURES
    assert "hrv_avg_ms" not in contributions
    assert explanation.intercept + sum(contributions.values()) == pytest.approx(explanation.prediction)
    assert explanation.prediction == pytest.approx(baseline_prediction)


def test_ridge_predictor_computes_bootstrap_sign_stability_tiers() -> None:
    rows, targets = _build_rows(48, residual_scale=0.1)
    predictor = RidgePredictor().fit(rows, targets)

    stability = {item.feature_name: item for item in predictor.feature_sign_stability()}

    assert set(stability) == set(MODEL_FEATURES)
    assert stability["total_sleep_min"].sign_stability_pct >= 0.90
    assert stability["total_sleep_min"].stability_label == "stable"
    assert stability["hrv_z"].sign_stability_pct >= 0.90
    assert stability["hrv_z"].stability_label == "stable"


def test_stability_label_covers_low_confidence_and_suppressed_thresholds() -> None:
    assert _stability_label(0.90) == "stable"
    assert _stability_label(0.89) == "low_confidence_signal"
    assert _stability_label(0.80) == "low_confidence_signal"
    assert _stability_label(0.79) == "suppressed"


def test_predict_interval_applies_two_point_floor_until_sixty_rows() -> None:
    small_rows, small_targets = _build_rows(30)
    small_predictor = RidgePredictor().fit(small_rows, small_targets)
    small_interval = small_predictor.predict_interval([small_rows[-1]])[0]

    large_rows, large_targets = _build_rows(60)
    large_predictor = RidgePredictor().fit(large_rows, large_targets)
    large_interval = large_predictor.predict_interval([large_rows[-1]])[0]

    assert small_interval.full_width == pytest.approx(2.0)
    assert small_interval.high - small_interval.low == pytest.approx(2.0)
    assert large_interval.full_width < 2.0
    assert np.isfinite(large_interval.low)
    assert np.isfinite(large_interval.high)


def test_predict_interval_is_batch_order_invariant_for_same_row() -> None:
    rows, targets = _build_rows(60, residual_scale=0.2)
    predictor = RidgePredictor().fit(rows, targets)

    reference_row = dict(rows[-1])
    other_row = dict(rows[0])

    solo_interval = predictor.predict_interval([reference_row])[0]
    first_in_batch = predictor.predict_interval([reference_row, other_row])[0]
    second_in_batch = predictor.predict_interval([other_row, reference_row])[1]

    assert first_in_batch.low == pytest.approx(solo_interval.low)
    assert first_in_batch.high == pytest.approx(solo_interval.high)
    assert second_in_batch.low == pytest.approx(solo_interval.low)
    assert second_in_batch.high == pytest.approx(solo_interval.high)

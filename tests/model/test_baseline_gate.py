from __future__ import annotations

from datetime import date, timedelta

import pytest

from src.model.baseline_gate import evaluate_baseline_gate


def _build_rows(count: int) -> tuple[list[dict[str, float | str | int]], list[float]]:
    rows: list[dict[str, float | str | int]] = []
    targets: list[float] = []
    residual_pattern = (0.25, -0.15, 0.20, -0.10, 0.05, -0.20, 0.10)

    for offset in range(count):
        total_sleep_min = 380.0 + float((offset * 13) % 140)
        hrv_z = -1.25 + float((offset * 3) % 10) / 4.0
        deep_sleep_pct = 0.12 + float((offset * 5) % 12) / 100.0
        prior_day_feeling = 4.0 + float((offset * 2) % 5)
        target = (
            1.4
            + (0.012 * total_sleep_min)
            + (0.85 * hrv_z)
            + (6.75 * deep_sleep_pct)
            + (0.60 * prior_day_feeling)
            + residual_pattern[offset % len(residual_pattern)]
        )
        rows.append(
            {
                "row_index": offset,
                "feature_date": (date(2026, 1, 1) + timedelta(days=offset)).isoformat(),
                "total_sleep_min": total_sleep_min,
                "hrv_z": hrv_z,
                "deep_sleep_pct": deep_sleep_pct,
                "prior_day_feeling": prior_day_feeling,
            }
        )
        targets.append(target)

    return rows, targets


class _StubPredictor:
    def __init__(self, predictions_by_index: dict[int, float]) -> None:
        self._predictions_by_index = predictions_by_index

    def fit(
        self,
        rows: list[dict[str, float | str | int]],
        targets: list[float],
    ) -> "_StubPredictor":
        return self

    def predict(self, rows: list[dict[str, float | str | int]]) -> list[float]:
        return [self._predictions_by_index[int(row["row_index"])] for row in rows]


def _predictor_factory(predictions_by_index: dict[int, float]):
    def factory() -> _StubPredictor:
        return _StubPredictor(predictions_by_index)

    return factory


def test_baseline_gate_is_ineligible_below_thirty_seven_model_ready_days() -> None:
    rows, targets = _build_rows(36)

    result = evaluate_baseline_gate(rows, targets)

    assert result.n_model == 36
    assert result.n_eval_days == 0
    assert result.baseline_gate_eligible is False
    assert result.baseline_gate_passed is False
    assert result.gate_reason == "collecting_model_ready_days"


@pytest.mark.parametrize("n_model", [37, 43])
def test_baseline_gate_uses_last_seven_eval_days_between_thirty_seven_and_forty_three(
    n_model: int,
) -> None:
    rows, targets = _build_rows(n_model)
    predictions_by_index = {index: target for index, target in enumerate(targets)}

    result = evaluate_baseline_gate(
        rows,
        targets,
        predictor_factory=_predictor_factory(predictions_by_index),
    )

    assert result.n_eval_days == 7
    assert [fold.feature_date for fold in result.folds] == [
        row["feature_date"] for row in rows[-7:]
    ]
    assert [fold.training_row_count for fold in result.folds] == list(
        range(n_model - 7, n_model)
    )
    assert min(fold.training_row_count for fold in result.folds) >= 30


def test_baseline_gate_uses_last_fourteen_days_at_forty_four_and_above() -> None:
    rows, targets = _build_rows(44)
    predictions_by_index = {index: target for index, target in enumerate(targets)}

    result = evaluate_baseline_gate(
        rows,
        targets,
        predictor_factory=_predictor_factory(predictions_by_index),
    )

    assert result.n_eval_days == 14
    assert [fold.feature_date for fold in result.folds] == [
        row["feature_date"] for row in rows[-14:]
    ]
    assert [fold.training_row_count for fold in result.folds] == list(range(30, 44))
    assert min(fold.training_row_count for fold in result.folds) == 30


def test_baseline_gate_fails_when_rmse_ratio_does_not_clear_ninety_five_percent() -> None:
    rows, _ = _build_rows(44)
    targets = [float(index) for index in range(len(rows))]
    predictions_by_index: dict[int, float] = {}
    eval_start = len(rows) - 14

    for eval_index in range(eval_start, len(rows)):
        prior_targets = targets[:eval_index]
        actual = targets[eval_index]
        rolling_prediction = sum(prior_targets[-7:]) / 7.0
        prior_day_prediction = prior_targets[-1]
        rolling_error = abs(actual - rolling_prediction)
        prior_day_error = abs(actual - prior_day_prediction)

        if rolling_error <= prior_day_error:
            baseline_prediction = rolling_prediction
        else:
            baseline_prediction = prior_day_prediction

        predictions_by_index[eval_index] = actual - ((actual - baseline_prediction) * 0.98)

    result = evaluate_baseline_gate(
        rows,
        targets,
        predictor_factory=_predictor_factory(predictions_by_index),
    )

    assert result.rmse_gate_passed is False
    assert result.better_day_gate_passed is True
    assert result.ridge_better_day_count == 14
    assert result.ridge_to_best_baseline_rmse_ratio == pytest.approx(0.98, rel=1e-3)
    assert result.baseline_gate_passed is False


def test_baseline_gate_fails_when_better_day_count_misses_the_ceil_rule() -> None:
    rows, _ = _build_rows(44)
    targets = [float(index) for index in range(len(rows))]
    predictions_by_index: dict[int, float] = {}
    eval_start = len(rows) - 14

    for eval_offset, eval_index in enumerate(range(eval_start, len(rows))):
        prior_targets = targets[:eval_index]
        actual = targets[eval_index]
        rolling_prediction = sum(prior_targets[-7:]) / 7.0
        prior_day_prediction = prior_targets[-1]
        rolling_error = abs(actual - rolling_prediction)
        prior_day_error = abs(actual - prior_day_prediction)

        if rolling_error <= prior_day_error:
            baseline_prediction = rolling_prediction
        else:
            baseline_prediction = prior_day_prediction

        error_scale = 0.0 if eval_offset < 9 else 1.01
        predictions_by_index[eval_index] = actual - ((actual - baseline_prediction) * error_scale)

    result = evaluate_baseline_gate(
        rows,
        targets,
        predictor_factory=_predictor_factory(predictions_by_index),
    )

    assert result.rmse_gate_passed is True
    assert result.better_day_gate_passed is False
    assert result.ridge_better_day_count == 9
    assert result.better_day_threshold == 10
    assert result.baseline_gate_passed is False

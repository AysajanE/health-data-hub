from __future__ import annotations

from dataclasses import dataclass
from math import ceil, sqrt
from typing import Any, Callable, Mapping, Sequence

from src.model.ridge import RidgePredictor


ROLLING_MEAN_WINDOW_DAYS = 7
MIN_TRAINING_ROWS_PER_FOLD = 30
MIN_MODEL_ROWS_FOR_GATE = 37
MAX_MODEL_ROWS_FOR_SEVEN_DAY_WINDOW = 43
SHORT_EVAL_WINDOW_DAYS = 7
LONG_EVAL_WINDOW_DAYS = 14
RMSE_RATIO_THRESHOLD = 0.95
BETTER_DAY_RATE_THRESHOLD = 0.65


@dataclass(frozen=True)
class BaselineGateFold:
    eval_index: int
    feature_date: str
    training_row_count: int
    actual: float
    ridge_prediction: float
    rolling_mean_prediction: float
    prior_day_prediction: float
    ridge_abs_error: float
    rolling_mean_abs_error: float
    prior_day_abs_error: float
    best_baseline_name: str
    best_baseline_prediction: float
    best_baseline_abs_error: float
    ridge_better_than_best_baseline: bool


@dataclass(frozen=True)
class BaselineGateResult:
    n_model: int
    n_eval_days: int
    eval_window_days: int
    baseline_gate_eligible: bool
    baseline_gate_passed: bool
    gate_reason: str
    minimum_training_row_count: int | None
    ridge_walk_forward_rmse: float | None
    baseline_rolling_mean_rmse: float | None
    baseline_prior_day_rmse: float | None
    best_baseline_rmse: float | None
    ridge_to_best_baseline_rmse_ratio: float | None
    rmse_gate_passed: bool
    ridge_better_day_count: int
    better_day_threshold: int
    better_day_gate_passed: bool
    folds: tuple[BaselineGateFold, ...]


def evaluation_window_days_for_n_model(n_model: int) -> int:
    if n_model < MIN_MODEL_ROWS_FOR_GATE:
        return 0
    if n_model <= MAX_MODEL_ROWS_FOR_SEVEN_DAY_WINDOW:
        return SHORT_EVAL_WINDOW_DAYS
    return LONG_EVAL_WINDOW_DAYS


def rolling_seven_day_mean_prediction(prior_targets: Sequence[float]) -> float:
    if not prior_targets:
        raise ValueError("rolling baseline requires at least one prior target")
    window = prior_targets[-ROLLING_MEAN_WINDOW_DAYS:]
    return float(sum(window) / len(window))


def prior_day_baseline_prediction(prior_targets: Sequence[float]) -> float:
    if not prior_targets:
        raise ValueError("prior-day baseline requires at least one prior target")
    return float(prior_targets[-1])


def evaluate_baseline_gate(
    rows: Sequence[Any],
    targets: Sequence[float],
    *,
    predictor_factory: Callable[[], Any] = RidgePredictor,
) -> BaselineGateResult:
    row_list = list(rows)
    target_list = [float(value) for value in targets]
    if len(row_list) != len(target_list):
        raise ValueError("rows and targets must have the same length")

    n_model = len(row_list)
    eval_window_days = evaluation_window_days_for_n_model(n_model)
    if eval_window_days == 0:
        return BaselineGateResult(
            n_model=n_model,
            n_eval_days=0,
            eval_window_days=0,
            baseline_gate_eligible=False,
            baseline_gate_passed=False,
            gate_reason="collecting_model_ready_days",
            minimum_training_row_count=None,
            ridge_walk_forward_rmse=None,
            baseline_rolling_mean_rmse=None,
            baseline_prior_day_rmse=None,
            best_baseline_rmse=None,
            ridge_to_best_baseline_rmse_ratio=None,
            rmse_gate_passed=False,
            ridge_better_day_count=0,
            better_day_threshold=0,
            better_day_gate_passed=False,
            folds=(),
        )

    eval_start = n_model - eval_window_days
    folds: list[BaselineGateFold] = []

    for eval_index in range(eval_start, n_model):
        training_rows = row_list[:eval_index]
        training_targets = target_list[:eval_index]
        if len(training_rows) < MIN_TRAINING_ROWS_PER_FOLD:
            raise ValueError("walk-forward folds require at least 30 prior model-ready days")

        predictor = predictor_factory()
        predictor.fit(training_rows, training_targets)
        ridge_prediction = float(predictor.predict([row_list[eval_index]])[0])

        actual = target_list[eval_index]
        rolling_mean_prediction = rolling_seven_day_mean_prediction(training_targets)
        prior_day_prediction = prior_day_baseline_prediction(training_targets)

        ridge_abs_error = abs(actual - ridge_prediction)
        rolling_mean_abs_error = abs(actual - rolling_mean_prediction)
        prior_day_abs_error = abs(actual - prior_day_prediction)

        if rolling_mean_abs_error <= prior_day_abs_error:
            best_baseline_name = "rolling_mean"
            best_baseline_prediction = rolling_mean_prediction
            best_baseline_abs_error = rolling_mean_abs_error
        else:
            best_baseline_name = "prior_day"
            best_baseline_prediction = prior_day_prediction
            best_baseline_abs_error = prior_day_abs_error

        folds.append(
            BaselineGateFold(
                eval_index=eval_index,
                feature_date=_feature_date(row_list[eval_index], fallback=str(eval_index)),
                training_row_count=len(training_rows),
                actual=actual,
                ridge_prediction=ridge_prediction,
                rolling_mean_prediction=rolling_mean_prediction,
                prior_day_prediction=prior_day_prediction,
                ridge_abs_error=ridge_abs_error,
                rolling_mean_abs_error=rolling_mean_abs_error,
                prior_day_abs_error=prior_day_abs_error,
                best_baseline_name=best_baseline_name,
                best_baseline_prediction=best_baseline_prediction,
                best_baseline_abs_error=best_baseline_abs_error,
                ridge_better_than_best_baseline=ridge_abs_error < best_baseline_abs_error,
            )
        )

    ridge_walk_forward_rmse = _rmse(fold.ridge_abs_error for fold in folds)
    baseline_rolling_mean_rmse = _rmse(fold.rolling_mean_abs_error for fold in folds)
    baseline_prior_day_rmse = _rmse(fold.prior_day_abs_error for fold in folds)
    best_baseline_rmse = min(baseline_rolling_mean_rmse, baseline_prior_day_rmse)
    ridge_to_best_baseline_rmse_ratio = _rmse_ratio(
        ridge_walk_forward_rmse,
        best_baseline_rmse,
    )
    rmse_gate_passed = ridge_walk_forward_rmse <= (RMSE_RATIO_THRESHOLD * best_baseline_rmse)

    ridge_better_day_count = sum(
        1 for fold in folds if fold.ridge_better_than_best_baseline
    )
    better_day_threshold = ceil(BETTER_DAY_RATE_THRESHOLD * len(folds))
    better_day_gate_passed = ridge_better_day_count >= better_day_threshold
    baseline_gate_passed = rmse_gate_passed and better_day_gate_passed

    return BaselineGateResult(
        n_model=n_model,
        n_eval_days=len(folds),
        eval_window_days=eval_window_days,
        baseline_gate_eligible=True,
        baseline_gate_passed=baseline_gate_passed,
        gate_reason=_gate_reason(
            baseline_gate_passed=baseline_gate_passed,
            rmse_gate_passed=rmse_gate_passed,
            better_day_gate_passed=better_day_gate_passed,
        ),
        minimum_training_row_count=min(fold.training_row_count for fold in folds),
        ridge_walk_forward_rmse=ridge_walk_forward_rmse,
        baseline_rolling_mean_rmse=baseline_rolling_mean_rmse,
        baseline_prior_day_rmse=baseline_prior_day_rmse,
        best_baseline_rmse=best_baseline_rmse,
        ridge_to_best_baseline_rmse_ratio=ridge_to_best_baseline_rmse_ratio,
        rmse_gate_passed=rmse_gate_passed,
        ridge_better_day_count=ridge_better_day_count,
        better_day_threshold=better_day_threshold,
        better_day_gate_passed=better_day_gate_passed,
        folds=tuple(folds),
    )


def _feature_date(row: Any, *, fallback: str) -> str:
    value = _row_value(row, "feature_date")
    if value in {None, ""}:
        return fallback
    return str(value)


def _row_value(row: Any, field: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(field)
    return getattr(row, field, None)


def _rmse(abs_errors: Sequence[float] | Any) -> float:
    values = [float(value) for value in abs_errors]
    if not values:
        return 0.0
    return float(sqrt(sum((value * value) for value in values) / len(values)))


def _rmse_ratio(ridge_rmse: float, baseline_rmse: float) -> float:
    if baseline_rmse == 0.0:
        return 0.0 if ridge_rmse == 0.0 else float("inf")
    return float(ridge_rmse / baseline_rmse)


def _gate_reason(
    *,
    baseline_gate_passed: bool,
    rmse_gate_passed: bool,
    better_day_gate_passed: bool,
) -> str:
    if baseline_gate_passed:
        return "passed"
    if not rmse_gate_passed and not better_day_gate_passed:
        return "failed_rmse_ratio_and_better_day_count"
    if not rmse_gate_passed:
        return "failed_rmse_ratio"
    return "failed_better_day_count"


__all__ = [
    "BETTER_DAY_RATE_THRESHOLD",
    "BaselineGateFold",
    "BaselineGateResult",
    "LONG_EVAL_WINDOW_DAYS",
    "MAX_MODEL_ROWS_FOR_SEVEN_DAY_WINDOW",
    "MIN_MODEL_ROWS_FOR_GATE",
    "MIN_TRAINING_ROWS_PER_FOLD",
    "RMSE_RATIO_THRESHOLD",
    "ROLLING_MEAN_WINDOW_DAYS",
    "SHORT_EVAL_WINDOW_DAYS",
    "evaluate_baseline_gate",
    "evaluation_window_days_for_n_model",
    "prior_day_baseline_prediction",
    "rolling_seven_day_mean_prediction",
]

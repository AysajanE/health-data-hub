from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable, Mapping, Sequence

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler


MODEL_FEATURES = (
    "total_sleep_min",
    "hrv_z",
    "deep_sleep_pct",
    "prior_day_feeling",
)
BOOTSTRAP_RESAMPLES = 200
STABLE_SIGN_THRESHOLD = 0.90
LOW_CONFIDENCE_SIGN_THRESHOLD = 0.80
PREDICTION_INTERVAL_FULL_WIDTH_FLOOR = 2.0
PREDICTION_INTERVAL_FLOOR_MIN_N = 60


@dataclass(frozen=True)
class FeatureSignStability:
    feature_name: str
    coefficient: float
    sign_stability_pct: float
    stability_label: str


@dataclass(frozen=True)
class FeatureContribution:
    feature_name: str
    feature_value: float
    scaled_value: float
    coefficient: float
    contribution: float
    sign_stability_pct: float
    stability_label: str


@dataclass(frozen=True)
class RidgeExplanation:
    prediction: float
    intercept: float
    contributions: tuple[FeatureContribution, ...]


@dataclass(frozen=True)
class PredictionInterval:
    prediction: float
    low: float
    high: float
    full_width: float


def _row_value(row: Any, field: str) -> Any:
    if isinstance(row, Mapping):
        return row.get(field)
    return getattr(row, field, None)


def _as_float(value: Any, *, field: str) -> float:
    if value is None:
        raise ValueError(field)
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(field) from error


def _stability_label(sign_stability_pct: float) -> str:
    if sign_stability_pct >= STABLE_SIGN_THRESHOLD:
        return "stable"
    if sign_stability_pct >= LOW_CONFIDENCE_SIGN_THRESHOLD:
        return "low_confidence_signal"
    return "suppressed"


class RidgePredictor:
    """Fixed-feature v1 ridge model with deterministic bootstrap diagnostics."""

    def __init__(
        self,
        *,
        alpha: float = 1.0,
        bootstrap_resamples: int = BOOTSTRAP_RESAMPLES,
        random_seed: int = 0,
    ) -> None:
        self.alpha = float(alpha)
        self.bootstrap_resamples = int(bootstrap_resamples)
        self.random_seed = int(random_seed)

    def fit(self, rows: Sequence[Any], targets: Sequence[float]) -> "RidgePredictor":
        row_list = list(rows)
        if not row_list:
            raise ValueError("at least one training row is required")

        target_array = np.asarray(list(targets), dtype=float)
        if len(row_list) != len(target_array):
            raise ValueError("rows and targets must have the same length")

        matrix, feature_dates = self._extract_feature_matrix(row_list, require_feature_date=True)
        self.scaler_ = StandardScaler()
        scaled_matrix = self.scaler_.fit_transform(matrix)
        self.model_ = Ridge(alpha=self.alpha)
        self.model_.fit(scaled_matrix, target_array)

        self.training_residuals_ = target_array - self.model_.predict(scaled_matrix)
        self.n_training_rows_ = len(row_list)
        self.feature_sign_stability_ = self._bootstrap_feature_sign_stability(
            matrix=matrix,
            targets=target_array,
            feature_dates=feature_dates,
        )
        return self

    def predict(self, rows: Sequence[Any]) -> list[float]:
        self._require_fitted()
        matrix, _ = self._extract_feature_matrix(rows, require_feature_date=False)
        scaled_matrix = self.scaler_.transform(matrix)
        return [float(value) for value in self.model_.predict(scaled_matrix)]

    def predict_interval(self, rows: Sequence[Any]) -> list[PredictionInterval]:
        self._require_fitted()
        predictions = np.asarray(self.predict(rows), dtype=float)
        residual_draws = self._bootstrap_residual_draws()
        interval_draws = predictions[:, None] + residual_draws
        low = np.quantile(interval_draws, 0.05, axis=1)
        high = np.quantile(interval_draws, 0.95, axis=1)

        intervals: list[PredictionInterval] = []
        for index, prediction in enumerate(predictions):
            bounded_low = float(low[index])
            bounded_high = float(high[index])
            if self.n_training_rows_ < PREDICTION_INTERVAL_FLOOR_MIN_N:
                full_width = bounded_high - bounded_low
                if full_width < PREDICTION_INTERVAL_FULL_WIDTH_FLOOR:
                    bounded_low = float(prediction - (PREDICTION_INTERVAL_FULL_WIDTH_FLOOR / 2.0))
                    bounded_high = float(prediction + (PREDICTION_INTERVAL_FULL_WIDTH_FLOOR / 2.0))

            intervals.append(
                PredictionInterval(
                    prediction=float(prediction),
                    low=bounded_low,
                    high=bounded_high,
                    full_width=float(bounded_high - bounded_low),
                )
            )
        return intervals

    def explain(self, rows: Sequence[Any]) -> list[RidgeExplanation]:
        self._require_fitted()
        matrix, _ = self._extract_feature_matrix(rows, require_feature_date=False)
        scaled_matrix = self.scaler_.transform(matrix)
        predictions = self.model_.predict(scaled_matrix)
        coefficients = np.asarray(self.model_.coef_, dtype=float)
        sign_stability_by_name = {
            item.feature_name: item for item in self.feature_sign_stability()
        }

        explanations: list[RidgeExplanation] = []
        for row_index, prediction in enumerate(predictions):
            contributions: list[FeatureContribution] = []
            scaled_row = scaled_matrix[row_index]
            raw_row = matrix[row_index]
            for feature_index, feature_name in enumerate(MODEL_FEATURES):
                stability = sign_stability_by_name[feature_name]
                contribution = float(coefficients[feature_index] * scaled_row[feature_index])
                contributions.append(
                    FeatureContribution(
                        feature_name=feature_name,
                        feature_value=float(raw_row[feature_index]),
                        scaled_value=float(scaled_row[feature_index]),
                        coefficient=stability.coefficient,
                        contribution=contribution,
                        sign_stability_pct=stability.sign_stability_pct,
                        stability_label=stability.stability_label,
                    )
                )

            explanations.append(
                RidgeExplanation(
                    prediction=float(prediction),
                    intercept=float(self.model_.intercept_),
                    contributions=tuple(contributions),
                )
            )
        return explanations

    def feature_sign_stability(self) -> tuple[FeatureSignStability, ...]:
        self._require_fitted()
        return tuple(self.feature_sign_stability_)

    def _extract_feature_matrix(
        self,
        rows: Sequence[Any],
        *,
        require_feature_date: bool,
    ) -> tuple[np.ndarray, np.ndarray | None]:
        row_list = list(rows)
        if not row_list:
            return np.empty((0, len(MODEL_FEATURES)), dtype=float), None

        matrix: list[list[float]] = []
        feature_dates: list[str] = []
        for row in row_list:
            values: list[float] = []
            for feature_name in MODEL_FEATURES:
                try:
                    values.append(_as_float(_row_value(row, feature_name), field=feature_name))
                except ValueError as error:
                    raise ValueError(
                        "rows must include feature_date and exactly the v1 features: "
                        + ", ".join(MODEL_FEATURES)
                    ) from error
            matrix.append(values)

            if require_feature_date:
                feature_date = _row_value(row, "feature_date")
                if feature_date in {None, ""}:
                    raise ValueError(
                        "rows must include feature_date and exactly the v1 features: "
                        + ", ".join(MODEL_FEATURES)
                    )
                feature_dates.append(str(feature_date))

        date_array = np.asarray(feature_dates, dtype=object) if require_feature_date else None
        return np.asarray(matrix, dtype=float), date_array

    def _bootstrap_feature_sign_stability(
        self,
        *,
        matrix: np.ndarray,
        targets: np.ndarray,
        feature_dates: np.ndarray,
    ) -> list[FeatureSignStability]:
        point_signs = np.sign(np.asarray(self.model_.coef_, dtype=float))
        sign_matches = np.zeros(len(MODEL_FEATURES), dtype=float)
        grouped_indices = self._group_indices_by_feature_date(feature_dates)
        unique_dates = np.asarray(tuple(grouped_indices), dtype=object)
        rng = np.random.default_rng(self.random_seed)

        for _ in range(self.bootstrap_resamples):
            sampled_dates = rng.choice(unique_dates, size=len(unique_dates), replace=True)
            sampled_indices: list[int] = []
            for sampled_date in sampled_dates:
                sampled_indices.extend(grouped_indices[str(sampled_date)])

            sample_matrix = matrix[sampled_indices]
            sample_targets = targets[sampled_indices]
            sample_scaler = StandardScaler()
            sample_scaled = sample_scaler.fit_transform(sample_matrix)
            sample_model = Ridge(alpha=self.alpha)
            sample_model.fit(sample_scaled, sample_targets)
            sample_signs = np.sign(np.asarray(sample_model.coef_, dtype=float))
            sign_matches += (sample_signs == point_signs) & (point_signs != 0.0)

        sign_stability = np.where(
            point_signs == 0.0,
            0.0,
            sign_matches / float(self.bootstrap_resamples),
        )
        return [
            FeatureSignStability(
                feature_name=feature_name,
                coefficient=float(self.model_.coef_[feature_index]),
                sign_stability_pct=float(sign_stability[feature_index]),
                stability_label=_stability_label(float(sign_stability[feature_index])),
            )
            for feature_index, feature_name in enumerate(MODEL_FEATURES)
        ]

    def _bootstrap_residual_draws(self) -> np.ndarray:
        rng = np.random.default_rng(self.random_seed + 1)
        # Broadcast one deterministic residual sample set across the batch so
        # interval bounds depend only on the model state and each row value.
        return rng.choice(
            self.training_residuals_,
            size=self.bootstrap_resamples,
            replace=True,
        )

    def _group_indices_by_feature_date(self, feature_dates: np.ndarray) -> dict[str, list[int]]:
        grouped: dict[str, list[int]] = {}
        for index, feature_date in enumerate(feature_dates.tolist()):
            grouped.setdefault(str(feature_date), []).append(index)
        return grouped

    def _require_fitted(self) -> None:
        if not hasattr(self, "model_") or not hasattr(self, "scaler_"):
            raise ValueError("RidgePredictor must be fitted before use")


__all__ = [
    "BOOTSTRAP_RESAMPLES",
    "FeatureContribution",
    "FeatureSignStability",
    "MODEL_FEATURES",
    "PredictionInterval",
    "RidgeExplanation",
    "RidgePredictor",
]

"""Pure, as-of v1 retrospective comparisons over caller-supplied Oura rows."""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import date, timedelta
from types import MappingProxyType
from typing import Any, Literal, Mapping, Sequence

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

from src.model.baseline_gate import MIN_MODEL_ROWS_FOR_GATE, evaluate_baseline_gate
from src.model.ridge import MODEL_FEATURES, RidgePredictor


@dataclass(frozen=True)
class FeaturePolicyEntry:
    role: Literal["behavior", "physiological_proxy", "noisy_stage_metric", "outcome_lag"]
    mutable: bool
    recommendation_policy: Literal["allowed_with_caveat", "disallowed"]
    show_as_contributor: bool
    display_name: str
    display_companion: str | None
    unit: Literal["minutes", "percent", "rating"] | None
    safe_floor: float | None


FEATURE_POLICY: Mapping[str, FeaturePolicyEntry] = MappingProxyType({
    "total_sleep_min": FeaturePolicyEntry(
        "behavior", True, "allowed_with_caveat", True, "Total sleep", None, "minutes", 420.0,
    ),
    "hrv_z": FeaturePolicyEntry(
        "physiological_proxy", False, "disallowed", True,
        "HRV (z-score vs baseline)", "hrv_avg_ms", None, None,
    ),
    "deep_sleep_pct": FeaturePolicyEntry(
        "noisy_stage_metric", False, "disallowed", True, "Deep sleep %", None, "percent", None,
    ),
    "prior_day_feeling": FeaturePolicyEntry(
        "outcome_lag", False, "disallowed", True, "Yesterday's feeling", None, "rating", None,
    ),
})
(MUTABLE_FEATURE,) = tuple(
    name for name, entry in FEATURE_POLICY.items()
    if entry.mutable and entry.recommendation_policy != "disallowed"
)
_SLEEP_INDEX = MODEL_FEATURES.index(MUTABLE_FEATURE)
_CONTEXT_INDICES = tuple(i for i, name in enumerate(MODEL_FEATURES) if name != MUTABLE_FEATURE)
FRAMING_LABEL = "model-estimated change in your past data"
CAVEAT = "correlation, not proven causation"
SUPPRESSION_REASONS = (
    "target_mood_missing",
    "target_features_unavailable",
    "collecting_model_ready_days",
    "as_of_model_unavailable",
    "baseline_gate_not_passed",
    "mutable_feature_not_stable",
    "actual_at_or_above_recent_median",
    "empty_candidate_envelope",
    "no_plausible_candidate",
    "delta_interval_not_positive",
    "delta_below_materiality_floor",
)


@dataclass(frozen=True)
class CounterfactualConfig:
    ridge_alpha: float = 1.0
    delta_bootstrap_seed: int = 20260611
    delta_bootstrap_resamples: int = 200
    sign_stability_seed: int = 0
    sign_stability_resamples: int = 200
    min_model_rows: int = MIN_MODEL_ROWS_FOR_GATE
    safe_floor: float = FEATURE_POLICY[MUTABLE_FEATURE].safe_floor
    recent_window_rows: int = 28
    candidate_count: int = 10
    max_neighbor_distance: float = 2.0
    witness_sleep_tolerance_min: float = 30.0
    witness_feature_tolerance: float = 1.0
    min_sign_stability: float = 0.90
    min_median_delta: float = 0.5
    jump_penalty: float = 0.1
    supported_model_versions: tuple[str, ...] = ("ridge-v1.0",)


@dataclass(frozen=True, kw_only=True)
class RetroCF:
    feature_name: str = MUTABLE_FEATURE
    feature_display_name: str = FEATURE_POLICY[MUTABLE_FEATURE].display_name
    actual_value: float
    comparison_value: float
    model_delta_low: float
    model_delta_high: float
    median_delta: float
    direction: Literal["increase_only"] = "increase_only"
    framing_label: str = FRAMING_LABEL
    caveat: str = CAVEAT


@dataclass(frozen=True, kw_only=True)
class CounterfactualProvenance:
    # Without a target row there is no supplied D; do not invent a date.
    target_date: date | None
    history_cutoff_date: date | None
    n_model: int
    model_version: str | None
    feature_version: str | None
    model_alpha: float | None
    training_start_date: date | None
    training_end_date: date | None
    provider_policy: Literal["oura_only_v1"] = "oura_only_v1"
    sleep_provider: Literal["oura"] = "oura"
    baseline_gate_source: Literal["s05_gate_recomputed_from_history_cohort"] | None = None
    baseline_gate_eligible: bool | None = None
    baseline_gate_passed: bool | None = None
    point_model_fit_source: Literal["s06_refit_from_history_cohort"] | None = None
    total_sleep_sign_stability: float | None = None
    sign_stability_seed: int
    sign_stability_resamples_configured: int
    sign_stability_resamples_executed: int = 0
    delta_bootstrap_seed: int
    delta_bootstrap_resamples_configured: int
    delta_bootstrap_resamples_executed: int = 0
    envelope_p5: float | None = None
    envelope_p95: float | None = None
    recent_median: float | None = None
    candidates_generated: int = 0
    candidates_plausible: int = 0


@dataclass(frozen=True)
class CounterfactualResult:
    status: Literal["available", "suppressed"]
    suppression_reason: str | None
    counterfactual: RetroCF | None
    provenance: CounterfactualProvenance

    def to_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result["provenance"] = {
            key: value.isoformat() if isinstance(value, date) else value
            for key, value in result["provenance"].items()
        }
        return result


@dataclass(frozen=True)
class FittedRidge:
    scaler: StandardScaler
    model: Ridge

    def predict(self, matrix: np.ndarray) -> np.ndarray:
        return self.model.predict(self.scaler.transform(matrix))


def fit_scaled_ridge_once(
    matrix: np.ndarray, targets: np.ndarray, *, alpha: float,
) -> FittedRidge:
    """Fit one scaler and one ridge model, without bootstrap diagnostics."""
    scaler = StandardScaler()
    scaled = scaler.fit_transform(matrix)
    model = Ridge(alpha=alpha).fit(scaled, targets)
    return FittedRidge(scaler=scaler, model=model)


def _matrix(rows: Sequence[Mapping]) -> np.ndarray:
    return np.asarray([[row[name] for name in MODEL_FEATURES] for row in rows], dtype=float)


class _OnceFitPredictor:
    """Baseline-fold adapter; these refits are separate from delta refits."""

    def __init__(self, *, alpha: float) -> None:
        self.alpha = alpha

    def fit(self, rows: Sequence[Mapping], targets: Sequence[float]) -> _OnceFitPredictor:
        self.fitted = fit_scaled_ridge_once(_matrix(rows), np.asarray(targets), alpha=self.alpha)
        return self

    def predict(self, rows: Sequence[Mapping]) -> list[float]:
        return self.fitted.predict(_matrix(rows)).tolist()


def _finite_float(value: Any) -> float | None:
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return numeric if np.isfinite(numeric) else None


def _feature_date(value: Any) -> date:
    if type(value) is date:
        return value
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError:
            pass
    raise ValueError("feature_date must be a date or an ISO date string")


def _plausible_candidates(
    candidates: np.ndarray, *, matrix: np.ndarray, target: np.ndarray,
    scaler: StandardScaler, config: CounterfactualConfig,
) -> np.ndarray:
    standardized_history = scaler.transform(matrix)
    standardized_target = scaler.transform(target[None, :])[0]
    context_matches = np.all(
        np.abs(standardized_history[:, _CONTEXT_INDICES]
               - standardized_target[list(_CONTEXT_INDICES)]) <= config.witness_feature_tolerance,
        axis=1,
    )
    plausible = []
    for candidate in candidates:
        vector = target.copy()
        vector[_SLEEP_INDEX] = candidate
        standardized = scaler.transform(vector[None, :])[0]
        nearest = np.min(np.linalg.norm(standardized_history - standardized, axis=1))
        witnesses = context_matches & (
            np.abs(matrix[:, _SLEEP_INDEX] - candidate) <= config.witness_sleep_tolerance_min
        )
        if nearest <= config.max_neighbor_distance and np.any(witnesses):
            plausible.append(candidate)
    return np.asarray(plausible, dtype=float)


def _bootstrap_deltas(
    matrix: np.ndarray, targets: np.ndarray, target: np.ndarray,
    candidates: np.ndarray, *, config: CounterfactualConfig,
) -> np.ndarray:
    vectors = np.tile(target, (len(candidates) + 1, 1))
    vectors[1:, _SLEEP_INDEX] = candidates
    rng = np.random.default_rng(config.delta_bootstrap_seed)
    deltas = np.empty((config.delta_bootstrap_resamples, len(candidates)), dtype=float)
    for index in range(config.delta_bootstrap_resamples):
        sample = rng.choice(len(matrix), size=len(matrix), replace=True)
        fitted = fit_scaled_ridge_once(matrix[sample], targets[sample], alpha=config.ridge_alpha)
        predictions = fitted.predict(vectors)
        deltas[index] = predictions[1:] - predictions[0]
    return deltas


def _select_candidate(
    candidates: np.ndarray, quantiles: np.ndarray, *, actual: float,
    sleep_scale: float, config: CounterfactualConfig,
) -> int | str:
    low, median, _ = quantiles
    positive = low > 0.0
    if not np.any(positive):
        return "delta_interval_not_positive"
    eligible = np.flatnonzero(positive & (median >= config.min_median_delta))
    if not len(eligible):
        return "delta_below_materiality_floor"
    return int(max(eligible, key=lambda i: (
        median[i] - config.jump_penalty * abs(candidates[i] - actual) / sleep_scale,
        -candidates[i],
    )))


def generate_retro_counterfactual(
    *, history_rows: Sequence[Mapping], target_row: Mapping | None,
    model_version: str | None = "ridge-v1.0", feature_version: str | None = "v1.0",
    config: CounterfactualConfig = CounterfactualConfig(),
) -> CounterfactualResult:
    """Return one retrospective sleep comparison or the first closed suppression.

    History must contain unique, ascending, finite model-ready rows before D.
    Invalid history is a caller error even when target mood is absent. The
    caller supplies the Oura-only cohort; this pure module cannot attest its
    origin. With no target/date supplied, missing-mood provenance dates are null.
    """
    dates = [_feature_date(row.get("feature_date")) for row in history_rows]
    if any(earlier >= later for earlier, later in zip(dates, dates[1:])):
        raise ValueError("history feature_date values must be unique and sorted ascending")
    history_targets = []
    for row in history_rows:
        values = [_finite_float(row.get(name)) for name in (*MODEL_FEATURES, "feeling")]
        if any(value is None for value in values):
            raise ValueError("history rows require finite feeling and all four finite features")
        history_targets.append(values[-1])

    target_mood = _finite_float(target_row.get("feeling")) if target_row is not None else None
    target_date = None
    if target_row is not None and (target_mood is not None or "feature_date" in target_row):
        target_date = _feature_date(target_row.get("feature_date"))
    if target_date is not None and any(day >= target_date for day in dates):
        raise ValueError("every history feature_date must be strictly earlier than target feature_date")
    alpha = _finite_float(config.ridge_alpha)
    provenance = CounterfactualProvenance(
        target_date=target_date,
        history_cutoff_date=target_date - timedelta(days=1) if target_date is not None else None,
        n_model=len(history_rows), model_version=model_version, feature_version=feature_version,
        model_alpha=alpha if alpha is not None and alpha > 0 else None,
        training_start_date=dates[0] if dates else None,
        training_end_date=dates[-1] if dates else None,
        sign_stability_seed=config.sign_stability_seed,
        sign_stability_resamples_configured=config.sign_stability_resamples,
        delta_bootstrap_seed=config.delta_bootstrap_seed,
        delta_bootstrap_resamples_configured=config.delta_bootstrap_resamples,
    )

    def suppressed(reason: str) -> CounterfactualResult:
        return CounterfactualResult("suppressed", reason, None, provenance)

    if target_mood is None:
        return suppressed("target_mood_missing")
    target_values = [_finite_float(target_row.get(name)) for name in MODEL_FEATURES]
    if any(value is None for value in target_values):
        return suppressed("target_features_unavailable")
    if len(history_rows) < config.min_model_rows:
        return suppressed("collecting_model_ready_days")
    if model_version not in config.supported_model_versions or alpha is None or alpha <= 0:
        return suppressed("as_of_model_unavailable")

    baseline = evaluate_baseline_gate(
        history_rows, history_targets, predictor_factory=lambda: _OnceFitPredictor(alpha=alpha),
    )
    provenance = replace(
        provenance, baseline_gate_source="s05_gate_recomputed_from_history_cohort",
        baseline_gate_eligible=baseline.baseline_gate_eligible,
        baseline_gate_passed=baseline.baseline_gate_passed,
    )
    if not (baseline.baseline_gate_eligible and baseline.baseline_gate_passed):
        return suppressed("baseline_gate_not_passed")

    point_model = RidgePredictor(
        alpha=alpha, bootstrap_resamples=config.sign_stability_resamples,
        random_seed=config.sign_stability_seed,
    ).fit(history_rows, history_targets)
    stability = next(item for item in point_model.feature_sign_stability()
                     if item.feature_name == MUTABLE_FEATURE)
    provenance = replace(
        provenance, point_model_fit_source="s06_refit_from_history_cohort",
        total_sleep_sign_stability=float(stability.sign_stability_pct),
        sign_stability_resamples_executed=config.sign_stability_resamples,
    )
    # The frozen algorithm gates on sign stability only. A stable negative
    # coefficient is not short-circuited here: increase-only candidates then
    # produce non-positive deltas and the bootstrap reports
    # delta_interval_not_positive, exactly as specified.
    if stability.sign_stability_pct < config.min_sign_stability:
        return suppressed("mutable_feature_not_stable")

    matrix = _matrix(history_rows)
    target = np.asarray(target_values, dtype=float)
    actual = float(target[_SLEEP_INDEX])
    sleep = matrix[:, _SLEEP_INDEX]
    recent_median = float(np.median(sleep[-config.recent_window_rows:]))
    provenance = replace(provenance, recent_median=recent_median)
    if actual >= recent_median:
        return suppressed("actual_at_or_above_recent_median")
    p5, p95 = (float(value) for value in np.quantile(sleep, [0.05, 0.95], method="linear"))
    provenance = replace(provenance, envelope_p5=p5, envelope_p95=p95)
    lower, upper = max(actual, config.safe_floor, p5), p95
    if upper <= lower:
        return suppressed("empty_candidate_envelope")

    candidates = np.linspace(lower, upper, num=config.candidate_count + 1)[1:]
    scaler = StandardScaler().fit(matrix)
    plausible = _plausible_candidates(candidates, matrix=matrix, target=target, scaler=scaler, config=config)
    provenance = replace(
        provenance, candidates_generated=len(candidates), candidates_plausible=len(plausible),
    )
    if not len(plausible):
        return suppressed("no_plausible_candidate")

    deltas = _bootstrap_deltas(matrix, np.asarray(history_targets), target, plausible, config=config)
    provenance = replace(provenance, delta_bootstrap_resamples_executed=config.delta_bootstrap_resamples)
    quantiles = np.quantile(deltas, [0.05, 0.5, 0.95], axis=0, method="linear")
    selected = _select_candidate(
        plausible, quantiles, actual=actual, sleep_scale=float(scaler.scale_[_SLEEP_INDEX]), config=config,
    )
    if isinstance(selected, str):
        return suppressed(selected)
    low, median, high = quantiles[:, selected]
    comparison = RetroCF(
        actual_value=actual, comparison_value=float(plausible[selected]),
        model_delta_low=float(low), model_delta_high=float(high), median_delta=float(median),
    )
    return CounterfactualResult("available", None, comparison, provenance)


__all__ = [
    "FeaturePolicyEntry", "FEATURE_POLICY", "MUTABLE_FEATURE", "FRAMING_LABEL", "CAVEAT",
    "SUPPRESSION_REASONS", "CounterfactualConfig", "RetroCF", "CounterfactualProvenance",
    "CounterfactualResult", "FittedRidge", "fit_scaled_ridge_once", "generate_retro_counterfactual",
]

from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import date, timedelta
import json
from types import MappingProxyType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import numpy as np
from sklearn.linear_model import Ridge
from sklearn.preprocessing import StandardScaler

import src.model.counterfactual as cf
from src.model.ridge import MODEL_FEATURES, FeatureSignStability, RidgePredictor


def _cohort(count=80, *, slope=0.01, noise=0.012):
    rng = np.random.default_rng(214)
    sleep = rng.permutation(np.linspace(350.0, 570.0, count))
    hrv = np.clip(rng.normal(0.0, 0.5, count), -1.0, 1.0)
    deep = rng.uniform(0.12, 0.24, count)
    prior = rng.uniform(3.0, 7.0, count)
    residual = rng.normal(0.0, noise, count)
    return [
        {
            "feature_date": (date(2026, 1, 1) + timedelta(days=i)).isoformat(),
            "total_sleep_min": float(sleep[i]),
            "hrv_z": float(hrv[i]),
            "deep_sleep_pct": float(deep[i]),
            "prior_day_feeling": float(prior[i]),
            "feeling": float(3.0 + slope * sleep[i] + residual[i]),
            "hrv_avg_ms": 50.0 + i,
            "feature_version": "v1.0",
        }
        for i in range(count)
    ]


def _target(rows, *, sleep=380.0, **changes):
    target = {
        "feature_date": (
            date.fromisoformat(str(rows[-1]["feature_date"])) + timedelta(days=1)
        ).isoformat(),
        "total_sleep_min": sleep,
        "hrv_z": 0.0,
        "deep_sleep_pct": 0.18,
        "prior_day_feeling": 5.0,
        "feeling": 8.7654321,
    }
    target.update(changes)
    return target


def _matrix(rows):
    return np.asarray([[row[name] for name in MODEL_FEATURES] for row in rows])


def _sign_predictor(*, stability=1.0, coefficient=1.0):
    predictor = Mock()
    predictor.fit.return_value = predictor
    predictor.feature_sign_stability.return_value = (
        FeatureSignStability("total_sleep_min", coefficient, stability, "stable"),
    )
    return predictor


def _passing_gate():
    return SimpleNamespace(baseline_gate_eligible=True, baseline_gate_passed=True)


def _numeric_leaves(value):
    if isinstance(value, dict):
        return [leaf for child in value.values() for leaf in _numeric_leaves(child)]
    if isinstance(value, (list, tuple)):
        return [leaf for child in value for leaf in _numeric_leaves(child)]
    return [value] if type(value) in (float, int) else []


class CounterfactualTestCase(unittest.TestCase):
    def _counted_generate(self, rows, target, **kwargs):
        # Baseline folds still perform real once-fits, counted separately from
        # the delta bootstrap's public helper calls.
        original_fit = cf.fit_scaled_ridge_once
        original_fold_fit = cf._OnceFitPredictor.fit

        def fit_fold(instance, fold_rows, targets):
            with patch.object(cf, "fit_scaled_ridge_once", original_fit):
                return original_fold_fit(instance, fold_rows, targets)

        with (
            patch.object(cf, "fit_scaled_ridge_once", wraps=original_fit) as delta_fits,
            patch.object(cf._OnceFitPredictor, "fit", autospec=True, side_effect=fit_fold) as folds,
        ):
            result = cf.generate_retro_counterfactual(
                history_rows=rows, target_row=target, **kwargs,
            )
        return result, delta_fits.call_count, folds.call_count

    def _assert_suppressed(self, reason, rows, target, **kwargs):
        result, delta_fits, _ = self._counted_generate(rows, target, **kwargs)
        self.assertEqual(result.status, "suppressed")
        self.assertEqual(result.suppression_reason, reason)
        self.assertIsNone(result.counterfactual)
        self.assertIn(result.suppression_reason, cf.SUPPRESSION_REASONS)
        self.assertEqual(result.provenance.n_model, len(rows))
        index = cf.SUPPRESSION_REASONS.index(reason)
        self.assertEqual(delta_fits, 200 if index >= 9 else 0)
        self.assertEqual(result.provenance.delta_bootstrap_resamples_executed, delta_fits)
        self.assertEqual(result.provenance.sign_stability_resamples_executed, 200 if index >= 5 else 0)
        if index < 4:
            self.assertIsNone(result.provenance.baseline_gate_source)
            self.assertIsNone(result.provenance.baseline_gate_eligible)
            self.assertIsNone(result.provenance.baseline_gate_passed)
        if index < 5:
            self.assertIsNone(result.provenance.point_model_fit_source)
            self.assertIsNone(result.provenance.total_sleep_sign_stability)
        else:
            self.assertEqual(result.provenance.point_model_fit_source, "s06_refit_from_history_cohort")
        return result

    def test_feature_policy_exact_values_order_and_deep_immutability(self):
        self.assertIsInstance(cf.FEATURE_POLICY, MappingProxyType)
        self.assertEqual(tuple(cf.FEATURE_POLICY), MODEL_FEATURES)
        expected = (
            ("behavior", True, "allowed_with_caveat", True, "Total sleep", None, "minutes", 420.0),
            ("physiological_proxy", False, "disallowed", True, "HRV (z-score vs baseline)", "hrv_avg_ms", None, None),
            ("noisy_stage_metric", False, "disallowed", True, "Deep sleep %", None, "percent", None),
            ("outcome_lag", False, "disallowed", True, "Yesterday's feeling", None, "rating", None),
        )
        self.assertEqual(tuple(tuple(asdict(entry).values()) for entry in cf.FEATURE_POLICY.values()), expected)
        self.assertEqual(
            [name for name, entry in cf.FEATURE_POLICY.items() if entry.mutable and entry.recommendation_policy != "disallowed"],
            [cf.MUTABLE_FEATURE],
        )
        self.assertEqual(cf.MUTABLE_FEATURE, "total_sleep_min")
        with self.assertRaises(TypeError):
            cf.FEATURE_POLICY["hrv_z"] = cf.FEATURE_POLICY["total_sleep_min"]
        with self.assertRaises(TypeError):
            del cf.FEATURE_POLICY["total_sleep_min"]
        for entry in cf.FEATURE_POLICY.values():
            with self.assertRaises(FrozenInstanceError):
                entry.mutable = not entry.mutable

    def test_config_defaults_and_closed_suppression_order(self):
        self.assertEqual(asdict(cf.CounterfactualConfig()), {
            "ridge_alpha": 1.0, "delta_bootstrap_seed": 20260611,
            "delta_bootstrap_resamples": 200, "sign_stability_seed": 0,
            "sign_stability_resamples": 200, "min_model_rows": 37,
            "safe_floor": 420.0, "recent_window_rows": 28, "candidate_count": 10,
            "max_neighbor_distance": 2.0, "witness_sleep_tolerance_min": 30.0,
            "witness_feature_tolerance": 1.0, "min_sign_stability": 0.90,
            "min_median_delta": 0.5, "jump_penalty": 0.1,
            "supported_model_versions": ("ridge-v1.0",),
        })
        self.assertEqual(cf.SUPPRESSION_REASONS, (
            "target_mood_missing", "target_features_unavailable",
            "collecting_model_ready_days", "as_of_model_unavailable",
            "baseline_gate_not_passed", "mutable_feature_not_stable",
            "actual_at_or_above_recent_median", "empty_candidate_envelope",
            "no_plausible_candidate", "delta_interval_not_positive",
            "delta_below_materiality_floor",
        ))
        with self.assertRaises(FrozenInstanceError):
            cf.CounterfactualConfig().safe_floor = 0.0

    def test_target_mood_missing_precedes_features_history_size_and_version(self):
        rows = _cohort(36)
        targets = [None, {}, {"feature_date": _target(rows)["feature_date"]}]
        targets.extend({**_target(rows), "feeling": invalid} for invalid in (None, np.nan, np.inf, -np.inf, "bad"))
        for target in targets:
            with self.subTest(target=target):
                result = self._assert_suppressed("target_mood_missing", rows, target, model_version="unknown")
                if target is None or "feature_date" not in target:
                    self.assertIsNone(result.provenance.target_date)
                    self.assertIsNone(result.provenance.history_cutoff_date)

    def test_missing_or_nonfinite_target_feature_precedes_row_count_and_version(self):
        rows = _cohort(36)
        for feature in MODEL_FEATURES:
            for invalid in ("missing", None, np.nan, np.inf, -np.inf, "bad"):
                with self.subTest(feature=feature, invalid=invalid):
                    target = _target(rows)
                    if invalid == "missing":
                        target.pop(feature)
                    else:
                        target[feature] = invalid
                    self._assert_suppressed("target_features_unavailable", rows, target, model_version="unknown")

    def test_thirty_six_collects_before_model_check_and_thirty_seven_runs_gate(self):
        rows = _cohort(37)
        self._assert_suppressed("collecting_model_ready_days", rows[:36], _target(rows), model_version="unknown")
        result, delta_fits, folds = self._counted_generate(rows, _target(rows))
        self.assertEqual(result.status, "available")
        self.assertEqual(result.provenance.n_model, 37)
        self.assertEqual(folds, 7)
        self.assertEqual(delta_fits, 200)

    def test_unknown_version_and_nonpositive_alpha_fail_before_baseline(self):
        rows = _cohort()
        with patch.object(cf, "evaluate_baseline_gate", side_effect=AssertionError("gate must not run")):
            for version in (None, "ridge-v9", ""):
                with self.subTest(version=version):
                    self._assert_suppressed("as_of_model_unavailable", rows, _target(rows), model_version=version)
            for alpha in (0.0, -1.0, np.nan, np.inf):
                with self.subTest(alpha=alpha):
                    self._assert_suppressed("as_of_model_unavailable", rows, _target(rows), config=replace(cf.CounterfactualConfig(), ridge_alpha=alpha))

    def test_noise_gate_failure_precedes_sign_and_ignores_stored_gate_flags(self):
        rows = _cohort()
        for row, feeling in zip(rows, np.random.default_rng(0).normal(6.0, 1.0, len(rows))):
            row.update(feeling=float(feeling), baseline_gate_eligible=True, baseline_gate_passed=True)
        target = _target(rows, sleep=1000.0, baseline_gate_eligible=True, baseline_gate_passed=True)
        with patch.object(cf, "RidgePredictor", side_effect=AssertionError("sign fit must not run")):
            result = self._assert_suppressed("baseline_gate_not_passed", rows, target)
        self.assertEqual(result.provenance.baseline_gate_source, "s05_gate_recomputed_from_history_cohort")
        self.assertTrue(result.provenance.baseline_gate_eligible)
        self.assertFalse(result.provenance.baseline_gate_passed)

    def test_baseline_requires_both_eligible_and_passed(self):
        rows = _cohort()
        for eligible, passed in ((False, False), (False, True), (True, False)):
            with self.subTest(eligible=eligible, passed=passed), patch.object(
                cf, "evaluate_baseline_gate", return_value=SimpleNamespace(baseline_gate_eligible=eligible, baseline_gate_passed=passed),
            ):
                result = self._assert_suppressed("baseline_gate_not_passed", rows, _target(rows))
                self.assertEqual(result.provenance.baseline_gate_eligible, eligible)
                self.assertEqual(result.provenance.baseline_gate_passed, passed)

    def test_sign_threshold_precedes_recent_median_regardless_of_coefficient_sign(self):
        rows = _cohort()
        with patch.object(cf, "RidgePredictor", return_value=_sign_predictor(stability=0.899999)):
            result = self._assert_suppressed("mutable_feature_not_stable", rows, _target(rows, sleep=1000.0))
            self.assertEqual(result.provenance.total_sleep_sign_stability, 0.899999)
        # The frozen algorithm gates on stability only: a stable negative or zero
        # coefficient passes this gate and is caught later by the delta interval.
        for stability, coefficient in ((0.90, 1.0), (1.0, -1.0), (1.0, 0.0)):
            with self.subTest(stability=stability, coefficient=coefficient), patch.object(
                cf, "RidgePredictor", return_value=_sign_predictor(stability=stability, coefficient=coefficient),
            ):
                result = self._assert_suppressed("actual_at_or_above_recent_median", rows, _target(rows, sleep=1000.0))
                self.assertEqual(result.provenance.total_sleep_sign_stability, stability)

    def test_real_negative_stable_sleep_coefficient_reaches_the_delta_interval_gate(self):
        # A stable negative sleep effect passes the sign-stability gate; the
        # increase-only candidates then yield non-positive deltas after the
        # full 200-resample bootstrap, exactly as the frozen contract specifies.
        rows = _cohort(slope=-0.01)
        result = self._assert_suppressed("delta_interval_not_positive", rows, _target(rows))
        self.assertGreaterEqual(result.provenance.total_sleep_sign_stability, 0.90)
        self.assertEqual(result.provenance.delta_bootstrap_resamples_executed, 200)
        self.assertGreater(result.provenance.candidates_plausible, 0)

    def test_recent_median_uses_latest_twenty_eight_and_is_inclusive(self):
        rows = _cohort()
        median = float(np.median([row["total_sleep_min"] for row in rows[-28:]]))
        self.assertNotEqual(median, float(np.median([row["total_sleep_min"] for row in rows])))
        for actual in (median, median + 1.0):
            with self.subTest(actual=actual):
                result = self._assert_suppressed("actual_at_or_above_recent_median", rows, _target(rows, sleep=actual))
                self.assertEqual(result.provenance.recent_median, median)
                self.assertIsNone(result.provenance.envelope_p5)

    def test_empty_envelope_when_actual_reaches_p95_below_recent_median(self):
        rows = _cohort(300)
        for row in rows[-14:]:
            row["total_sleep_min"] = 800.0
        p5, p95 = np.quantile([row["total_sleep_min"] for row in rows], [0.05, 0.95], method="linear")
        with (
            patch.object(cf, "evaluate_baseline_gate", return_value=_passing_gate()),
            patch.object(cf, "RidgePredictor", return_value=_sign_predictor()),
        ):
            for actual in (float(p95), float(p95 + 1.0)):
                with self.subTest(actual=actual):
                    result = self._assert_suppressed("empty_candidate_envelope", rows, _target(rows, sleep=actual))
                    self.assertLess(actual, result.provenance.recent_median)
                    self.assertEqual(result.provenance.envelope_p5, p5)
                    self.assertEqual(result.provenance.envelope_p95, p95)
                    self.assertEqual(result.provenance.candidates_generated, 0)

    def test_safe_floor_empties_envelope_when_all_history_sleep_is_below_it(self):
        rows = _cohort()
        for row in rows:
            row["total_sleep_min"] -= 200.0
        result = self._assert_suppressed("empty_candidate_envelope", rows, _target(rows, sleep=180.0))
        self.assertLess(result.provenance.envelope_p95, 420.0)
        self.assertLess(180.0, result.provenance.recent_median)
        self.assertEqual(result.provenance.candidates_generated, 0)

    def test_outlying_target_context_has_no_plausible_candidate(self):
        rows = _cohort()
        result = self._assert_suppressed("no_plausible_candidate", rows, _target(rows, hrv_z=1000.0))
        self.assertEqual(result.provenance.candidates_generated, 10)
        self.assertEqual(result.provenance.candidates_plausible, 0)

    def test_negative_delta_interval_after_isolating_the_sign_gate(self):
        rows = _cohort(slope=-0.01)
        # A real negative effect normally stops at sign stability. Isolate that
        # earlier gate to test the later delta reason with real bootstrap fits.
        with patch.object(cf, "RidgePredictor", return_value=_sign_predictor()):
            self._assert_suppressed("delta_interval_not_positive", rows, _target(rows))

    def test_tiny_positive_effect_clears_interval_but_not_materiality(self):
        rows = _cohort(slope=0.0001, noise=0.00001)
        result = self._assert_suppressed("delta_below_materiality_floor", rows, _target(rows))
        self.assertGreaterEqual(result.provenance.total_sleep_sign_stability, 0.90)

    def test_strong_positive_effect_available_with_exact_provenance_and_counts(self):
        rows = _cohort()
        target = _target(rows)
        result, delta_fits, fold_fits = self._counted_generate(rows, target)
        self.assertEqual(result.status, "available")
        self.assertIsNone(result.suppression_reason)
        comparison = result.counterfactual
        self.assertIsNotNone(comparison)
        self.assertEqual(comparison.feature_name, "total_sleep_min")
        self.assertEqual(comparison.feature_display_name, "Total sleep")
        self.assertEqual(comparison.actual_value, target["total_sleep_min"])
        self.assertGreater(comparison.comparison_value, comparison.actual_value)
        self.assertGreaterEqual(comparison.comparison_value, 420.0)
        self.assertLessEqual(comparison.comparison_value, result.provenance.envelope_p95)
        self.assertGreater(comparison.model_delta_low, 0.0)
        self.assertGreaterEqual(comparison.median_delta, 0.5)
        self.assertLessEqual(comparison.model_delta_low, comparison.median_delta)
        self.assertLessEqual(comparison.median_delta, comparison.model_delta_high)
        self.assertEqual(comparison.direction, "increase_only")
        self.assertEqual(comparison.framing_label, "model-estimated change in your past data")
        self.assertEqual(comparison.caveat, "correlation, not proven causation")
        self.assertEqual(cf.FRAMING_LABEL, comparison.framing_label)
        self.assertEqual(cf.CAVEAT, comparison.caveat)
        self.assertEqual(delta_fits, 200)
        self.assertEqual(fold_fits, 14)
        provenance = result.provenance
        self.assertEqual(provenance.target_date, date.fromisoformat(target["feature_date"]))
        self.assertEqual(provenance.history_cutoff_date, provenance.target_date - timedelta(days=1))
        self.assertEqual(provenance.training_start_date, date(2026, 1, 1))
        self.assertEqual(provenance.training_end_date, date.fromisoformat(rows[-1]["feature_date"]))
        self.assertEqual(provenance.model_version, "ridge-v1.0")
        self.assertEqual(provenance.feature_version, "v1.0")
        self.assertEqual(provenance.model_alpha, 1.0)
        self.assertEqual(provenance.provider_policy, "oura_only_v1")
        self.assertEqual(provenance.sleep_provider, "oura")
        self.assertEqual(provenance.baseline_gate_source, "s05_gate_recomputed_from_history_cohort")
        self.assertTrue(provenance.baseline_gate_eligible)
        self.assertTrue(provenance.baseline_gate_passed)
        self.assertEqual(provenance.point_model_fit_source, "s06_refit_from_history_cohort")
        self.assertEqual(provenance.sign_stability_seed, 0)
        self.assertEqual(provenance.sign_stability_resamples_configured, 200)
        self.assertEqual(provenance.sign_stability_resamples_executed, 200)
        self.assertEqual(provenance.delta_bootstrap_seed, 20260611)
        self.assertEqual(provenance.delta_bootstrap_resamples_configured, 200)
        self.assertEqual(provenance.delta_bootstrap_resamples_executed, 200)

    def test_ten_unrounded_candidates_exclude_lower_and_include_p95(self):
        for sleep_shift, actual in ((0.0, 350.0), (0.0, 430.123456789), (100.0, 350.0)):
            with self.subTest(sleep_shift=sleep_shift, actual=actual):
                rows = _cohort()
                for row in rows:
                    row["total_sleep_min"] += sleep_shift
                p5, p95 = np.quantile([row["total_sleep_min"] for row in rows], [0.05, 0.95], method="linear")
                lower = max(actual, 420.0, p5)
                expected = np.linspace(lower, p95, 11)[1:]
                with (
                    patch.object(cf.np, "linspace", wraps=np.linspace) as linspace,
                    patch.object(cf, "_plausible_candidates", wraps=cf._plausible_candidates) as plausibility,
                ):
                    result = cf.generate_retro_counterfactual(history_rows=rows, target_row=_target(rows, sleep=actual))
                linspace.assert_called_once_with(lower, p95, num=11)
                candidates = plausibility.call_args.args[0]
                np.testing.assert_array_equal(candidates, expected)
                self.assertEqual(len(candidates), 10)
                self.assertTrue(np.all(candidates > lower))
                self.assertEqual(candidates[-1], p95)
                self.assertEqual(result.provenance.candidates_generated, 10)
                self.assertEqual(result.provenance.envelope_p5, p5)
                self.assertEqual(result.provenance.envelope_p95, p95)
                self.assertIn(result.counterfactual.comparison_value, expected)

    def test_history_contract_errors_raise_value_error(self):
        rows = _cohort(37)
        target = _target(rows)
        cases = []
        swapped = deepcopy(rows)
        swapped[0], swapped[1] = swapped[1], swapped[0]
        cases.append(("unsorted", swapped))
        for invalid_date in (target["feature_date"], "2099-01-01", rows[-2]["feature_date"], "bad", None):
            invalid = deepcopy(rows)
            invalid[-1]["feature_date"] = invalid_date
            cases.append((f"date {invalid_date}", invalid))
        for field in (*MODEL_FEATURES, "feeling"):
            for invalid_value in ("missing", np.nan, np.inf):
                invalid = deepcopy(rows)
                if invalid_value == "missing":
                    invalid[0].pop(field)
                else:
                    invalid[0][field] = invalid_value
                cases.append((f"{field} {invalid_value}", invalid))
        for name, history in cases:
            with self.subTest(case=name), self.assertRaises(ValueError):
                cf.generate_retro_counterfactual(history_rows=history, target_row=target)

    def test_date_objects_and_iso_strings_are_equivalent_and_invalid_target_date_errors(self):
        rows = _cohort()
        target = _target(rows)
        expected = cf.generate_retro_counterfactual(history_rows=rows, target_row=target).to_dict()
        dates = [{**row, "feature_date": date.fromisoformat(row["feature_date"])} for row in rows]
        actual = cf.generate_retro_counterfactual(history_rows=dates, target_row={**target, "feature_date": date.fromisoformat(target["feature_date"])}).to_dict()
        self.assertEqual(actual, expected)
        for invalid in (None, "invalid", "2026-02-30"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                cf.generate_retro_counterfactual(history_rows=rows, target_row={**target, "feature_date": invalid})

    def test_determinism_input_purity_serialization_and_no_label_or_coefficient_leak(self):
        rows = _cohort()
        target = _target(rows)
        before = deepcopy((rows, target))
        first = cf.generate_retro_counterfactual(history_rows=rows, target_row=target)
        second = cf.generate_retro_counterfactual(history_rows=rows, target_row=target)
        output = first.to_dict()
        self.assertEqual(output, second.to_dict())
        self.assertEqual((rows, target), before)
        encoded = json.dumps(output, allow_nan=False)
        self.assertEqual(json.loads(encoded), output)
        self.assertEqual(set(output), {"status", "suppression_reason", "counterfactual", "provenance"})
        self.assertEqual(set(output["counterfactual"]), {
            "feature_name", "feature_display_name", "actual_value", "comparison_value",
            "model_delta_low", "model_delta_high", "median_delta", "direction", "framing_label", "caveat",
        })
        self.assertEqual(set(output["provenance"]), {
            "target_date", "history_cutoff_date", "n_model", "model_version", "feature_version",
            "model_alpha", "training_start_date", "training_end_date", "provider_policy", "sleep_provider",
            "baseline_gate_source", "baseline_gate_eligible", "baseline_gate_passed", "point_model_fit_source",
            "total_sleep_sign_stability", "sign_stability_seed", "sign_stability_resamples_configured",
            "sign_stability_resamples_executed", "delta_bootstrap_seed", "delta_bootstrap_resamples_configured",
            "delta_bootstrap_resamples_executed", "envelope_p5", "envelope_p95", "recent_median",
            "candidates_generated", "candidates_plausible",
        })
        self.assertNotIn("coef", encoded)
        self.assertNotIn("history_rows", encoded)
        self.assertNotIn("path", encoded)
        numeric = _numeric_leaves(output)
        point_model = cf.fit_scaled_ridge_once(_matrix(rows), np.asarray([row["feeling"] for row in rows]), alpha=1.0)
        for private_value in [row["feeling"] for row in rows] + list(point_model.model.coef_) + [target["feeling"]]:
            self.assertNotIn(private_value, numeric)
        for key in ("actual_value", "comparison_value", "model_delta_low", "model_delta_high", "median_delta"):
            self.assertIs(type(output["counterfactual"][key]), float)
        self.assertEqual(output["provenance"]["target_date"], target["feature_date"])
        for obj, field, value in ((first, "status", "suppressed"), (first.provenance, "n_model", 0), (first.counterfactual, "comparison_value", 0.0)):
            with self.assertRaises(FrozenInstanceError):
                setattr(obj, field, value)
        output["provenance"]["n_model"] = -1
        self.assertEqual(first.provenance.n_model, len(rows))

    def test_explicit_delta_seed_and_irrelevant_metadata(self):
        rows = _cohort()
        target = _target(rows)
        default = cf.generate_retro_counterfactual(history_rows=rows, target_row=target)
        config = replace(cf.CounterfactualConfig(), delta_bootstrap_seed=42)
        other = cf.generate_retro_counterfactual(history_rows=rows, target_row=target, config=config)
        self.assertEqual(default.provenance.delta_bootstrap_seed, 20260611)
        self.assertEqual(other.provenance.delta_bootstrap_seed, 42)
        self.assertNotEqual(default.counterfactual, other.counterfactual)
        mutated_rows = [{**row, "hrv_avg_ms": -99999.0, "feature_version": "unused"} for row in rows]
        mutated_target = {**target, "hrv_avg_ms": 99999.0, "feeling": -123.456}
        self.assertEqual(default.to_dict(), cf.generate_retro_counterfactual(history_rows=mutated_rows, target_row=mutated_target).to_dict())

    def test_increase_only_and_safe_floor_over_parameter_sweep(self):
        available = 0
        for count in (37, 60, 80):
            rows = _cohort(count)
            for actual in (320.0, 400.0, 419.0, 450.0, 600.0):
                with self.subTest(count=count, actual=actual):
                    result = cf.generate_retro_counterfactual(history_rows=rows, target_row=_target(rows, sleep=actual))
                    if result.status == "available":
                        available += 1
                        self.assertGreater(result.counterfactual.comparison_value, actual)
                        self.assertGreaterEqual(result.counterfactual.comparison_value, 420.0)
                        self.assertLessEqual(result.counterfactual.comparison_value, result.provenance.envelope_p95)
                    else:
                        self.assertIn(result.suppression_reason, cf.SUPPRESSION_REASONS)
        self.assertGreaterEqual(available, 9)


class PlausibilityTests(unittest.TestCase):
    def _plausible(self, matrix, target, candidates, config=cf.CounterfactualConfig()):
        matrix = np.asarray(matrix, dtype=float)
        target = np.asarray(target, dtype=float)
        scaler = StandardScaler().fit(matrix)
        return cf._plausible_candidates(np.asarray(candidates, dtype=float), matrix=matrix, target=target, scaler=scaler, config=config)

    def test_nearest_distance_passes_but_missing_sleep_witness_rejects(self):
        matrix = np.asarray([[350.0, 0.0, 0.2, 5.0], [550.0, 0.0, 0.2, 5.0]])
        scaler = StandardScaler().fit(matrix)
        candidate = np.asarray([450.0, 0.0, 0.2, 5.0])
        nearest = np.linalg.norm(scaler.transform(matrix) - scaler.transform(candidate[None, :]), axis=1).min()
        self.assertLessEqual(nearest, 2.0)
        self.assertEqual(self._plausible(matrix, candidate, [450.0]).size, 0)

    def test_sleep_and_context_witness_pass_but_nearest_distance_rejects(self):
        matrix = [[449.0, 0.0, 0.2, 5.0], [451.0, 0.0, 0.2, 5.0]]
        self.assertLessEqual(abs(470.0 - 451.0), 30.0)
        self.assertEqual(self._plausible(matrix, [470.0, 0.0, 0.2, 5.0], [470.0]).size, 0)
        np.testing.assert_array_equal(self._plausible(matrix, [449.0, 0.0, 0.2, 5.0], [453.0]), [453.0])

    def test_witness_sleep_tolerance_is_inclusive(self):
        matrix = [[350.0, 0.0, 0.2, 5.0], [650.0, 0.0, 0.2, 5.0]]
        np.testing.assert_array_equal(self._plausible(matrix, [350.0, 0.0, 0.2, 5.0], [380.0, 380.000001]), [380.0])

    def test_each_context_tolerance_is_independent_and_inclusive(self):
        matrix = [[449.0, 0.0, 0.0, 0.0], [451.0, 0.0, 0.0, 0.0]]
        np.testing.assert_array_equal(StandardScaler().fit(matrix).scale_[1:], [1.0, 1.0, 1.0])
        target = [449.0, 1.0, 1.0, 1.0]
        np.testing.assert_array_equal(self._plausible(matrix, target, [449.0]), [449.0])
        for index in (1, 2, 3):
            with self.subTest(feature=MODEL_FEATURES[index]):
                outside = target.copy()
                outside[index] += 0.000001
                self.assertEqual(self._plausible(matrix, outside, [449.0]).size, 0)

    def test_nearest_row_and_context_witness_can_be_different_rows(self):
        matrix = np.asarray([
            [400.0, 0.0, 0.0, 0.0], [450.0, 1.0, 1.0, 1.0],
            [1000.0, -1.0, -1.0, -1.0], [1000.0, 1.0, 1.0, 1.0],
            [1000.0, -1.0, -1.0, -1.0], [1000.0, 1.0, 1.0, 1.0],
        ])
        target = np.asarray([450.0, 0.25, 0.25, 0.25])
        scaler = StandardScaler().fit(matrix)
        distances = np.linalg.norm(scaler.transform(matrix) - scaler.transform(target[None, :]), axis=1)
        self.assertLessEqual(distances[0], 1.0)
        self.assertGreater(distances[1], 1.0)
        self.assertGreater(abs(matrix[0, 0] - 450.0), 30.0)
        config = replace(cf.CounterfactualConfig(), max_neighbor_distance=1.0)
        np.testing.assert_array_equal(self._plausible(matrix, target, [450.0], config), [450.0])


class BootstrapAndSelectionTests(unittest.TestCase):
    def test_once_fit_matches_standard_scaler_and_ridge_without_bootstrap(self):
        rows = _cohort(37)
        matrix = _matrix(rows)
        targets = np.asarray([row["feeling"] for row in rows])
        expected_scaler = StandardScaler().fit(matrix)
        expected_model = Ridge(alpha=2.5).fit(expected_scaler.transform(matrix), targets)
        existing_predictor = RidgePredictor(alpha=2.5).fit(rows, targets.tolist())
        with (
            patch.object(cf, "StandardScaler", wraps=StandardScaler) as scaler,
            patch.object(cf, "Ridge", wraps=Ridge) as ridge,
            patch.object(cf, "RidgePredictor", side_effect=AssertionError("no diagnostic bootstrap")),
        ):
            actual = cf.fit_scaled_ridge_once(matrix, targets, alpha=2.5)
        scaler.assert_called_once_with()
        ridge.assert_called_once_with(alpha=2.5)
        self.assertIsInstance(actual, cf.FittedRidge)
        np.testing.assert_allclose(actual.scaler.mean_, expected_scaler.mean_, rtol=0, atol=0)
        np.testing.assert_allclose(actual.model.coef_, expected_model.coef_, rtol=0, atol=0)
        np.testing.assert_allclose(actual.predict(matrix), expected_model.predict(expected_scaler.transform(matrix)), rtol=0, atol=0)
        np.testing.assert_allclose(actual.predict(matrix), existing_predictor.predict(rows), rtol=0, atol=0)

    def test_delta_bootstrap_samples_once_per_refit_and_reuses_for_all_candidates(self):
        rows = _cohort(37)
        matrix = _matrix(rows)
        targets = np.asarray([row["feeling"] for row in rows])
        target = np.asarray([380.0, 0.25, 0.18, 4.25])
        candidates = np.asarray([420.5, 470.75, 550.125])
        config = replace(cf.CounterfactualConfig(), ridge_alpha=2.5)
        real_rng = np.random.default_rng(config.delta_bootstrap_seed)
        expected_samples = [real_rng.choice(len(rows), size=len(rows), replace=True) for _ in range(200)]
        samples = Mock()
        samples.choice.side_effect = expected_samples
        captured_vectors = []

        class LinearModel:
            def predict(self, vectors):
                captured_vectors.append(vectors.copy())
                return 20.0 * vectors[:, 0] + 100.0

        with (
            patch.object(cf.np.random, "default_rng", return_value=samples) as seed,
            patch.object(cf, "fit_scaled_ridge_once", return_value=LinearModel()) as fits,
        ):
            deltas = cf._bootstrap_deltas(matrix, targets, target, candidates, config=config)
        seed.assert_called_once_with(20260611)
        self.assertEqual(samples.choice.call_count, 200)
        self.assertEqual(fits.call_count, 200)
        for call, sample in zip(fits.call_args_list, expected_samples):
            np.testing.assert_array_equal(call.args[0], matrix[sample])
            np.testing.assert_array_equal(call.args[1], targets[sample])
            self.assertEqual(call.kwargs, {"alpha": 2.5})
        for call in samples.choice.call_args_list:
            self.assertEqual(call.args, (len(rows),))
            self.assertEqual(call.kwargs, {"size": len(rows), "replace": True})
        expected_vectors = np.tile(target, (len(candidates) + 1, 1))
        expected_vectors[1:, 0] = candidates
        for vectors in captured_vectors:
            np.testing.assert_array_equal(vectors, expected_vectors)
        np.testing.assert_array_equal(deltas, np.tile(20.0 * (candidates - target[0]), (200, 1)))
        self.assertGreater(deltas.min(), 10.0)  # Raw model deltas are not rating-clipped.

    def test_real_bootstrap_is_candidate_order_invariant(self):
        rows = _cohort(37)
        matrix = _matrix(rows)
        targets = np.asarray([row["feeling"] for row in rows])
        target = np.asarray([380.0, 0.0, 0.18, 5.0])
        candidates = np.asarray([430.0, 490.0, 550.0])
        forward = cf._bootstrap_deltas(matrix, targets, target, candidates, config=cf.CounterfactualConfig())
        reverse = cf._bootstrap_deltas(matrix, targets, target, candidates[::-1], config=cf.CounterfactualConfig())
        np.testing.assert_array_equal(forward, reverse[:, ::-1])

    def test_linear_delta_quantiles_are_returned_without_clipping(self):
        rows = _cohort()
        deltas = np.full((200, 10), -1.0)
        deltas[:, -1] = np.linspace(20.0, 24.0, 200)
        with (
            patch.object(cf, "evaluate_baseline_gate", return_value=_passing_gate()),
            patch.object(cf, "RidgePredictor", return_value=_sign_predictor()),
            patch.object(cf, "_plausible_candidates", side_effect=lambda candidates, **kwargs: candidates),
            patch.object(cf, "_bootstrap_deltas", return_value=deltas),
        ):
            result = cf.generate_retro_counterfactual(history_rows=rows, target_row=_target(rows))
        low, median, high = np.quantile(deltas[:, -1], [0.05, 0.5, 0.95], method="linear")
        self.assertEqual(result.status, "available")
        self.assertEqual(result.counterfactual.comparison_value, result.provenance.envelope_p95)
        self.assertEqual(result.counterfactual.model_delta_low, low)
        self.assertEqual(result.counterfactual.median_delta, median)
        self.assertEqual(result.counterfactual.model_delta_high, high)
        self.assertGreater(result.counterfactual.model_delta_high, 10.0)

    def test_low_must_be_positive_and_median_materiality_is_inclusive(self):
        candidates = np.asarray([430.0, 450.0, 470.0])
        cases = (
            ([[0.0, -0.1, -1.0], [10.0, 10.0, 10.0], [11.0, 11.0, 11.0]], "delta_interval_not_positive"),
            ([[0.0, 0.1, 0.2], [100.0, 0.49999, 0.49], [101.0, 1.0, 1.0]], "delta_below_materiality_floor"),
            ([[0.0, 0.1, 0.2], [100.0, 0.5, 0.49], [101.0, 1.0, 1.0]], 1),
        )
        for quantiles, expected in cases:
            with self.subTest(expected=expected):
                self.assertEqual(cf._select_candidate(candidates, np.asarray(quantiles), actual=400.0, sleep_scale=50.0, config=cf.CounterfactualConfig()), expected)

    def test_jump_penalty_changes_selection_and_exact_ties_choose_smaller(self):
        candidates = np.asarray([420.0, 520.0])
        quantiles = np.asarray([[0.1, 0.1], [0.6, 0.65], [0.7, 0.75]])
        self.assertEqual(cf._select_candidate(candidates, quantiles, actual=400.0, sleep_scale=100.0, config=cf.CounterfactualConfig()), 0)
        self.assertEqual(cf._select_candidate(candidates, quantiles, actual=400.0, sleep_scale=100.0, config=replace(cf.CounterfactualConfig(), jump_penalty=0.0)), 1)
        # Binary-exact values make an exact score tie, rather than a tolerance tie.
        candidates = np.asarray([600.0, 500.0])
        quantiles = np.asarray([[0.1, 0.1], [1.0, 0.75], [1.2, 0.9]])
        config = replace(cf.CounterfactualConfig(), jump_penalty=0.25)
        self.assertEqual(cf._select_candidate(candidates, quantiles, actual=400.0, sleep_scale=100.0, config=config), 1)

    def test_alpha_and_sign_settings_are_forwarded_to_the_single_full_fit(self):
        rows = _cohort()
        predictor = _sign_predictor(stability=0.8)
        config = replace(cf.CounterfactualConfig(), ridge_alpha=2.5, sign_stability_seed=17)
        with (
            patch.object(cf, "RidgePredictor", return_value=predictor) as constructor,
            patch.object(cf, "fit_scaled_ridge_once", wraps=cf.fit_scaled_ridge_once) as fits,
        ):
            result = cf.generate_retro_counterfactual(history_rows=rows, target_row=_target(rows), config=config)
        constructor.assert_called_once_with(alpha=2.5, bootstrap_resamples=200, random_seed=17)
        predictor.fit.assert_called_once_with(rows, [row["feeling"] for row in rows])
        self.assertEqual(fits.call_count, 14)
        self.assertTrue(all(call.kwargs == {"alpha": 2.5} for call in fits.call_args_list))
        self.assertEqual(result.suppression_reason, "mutable_feature_not_stable")
        self.assertEqual(result.provenance.model_alpha, 2.5)
        self.assertEqual(result.provenance.sign_stability_seed, 17)
        self.assertEqual(result.provenance.sign_stability_resamples_executed, 200)
        self.assertEqual(result.provenance.delta_bootstrap_resamples_executed, 0)

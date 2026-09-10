from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import date
from types import MappingProxyType
import unittest

from src.model.display_gate import (
    DisplayState,
    FEATURE_DISPLAY,
    build_insight_view,
    direction_text,
    format_feature_value,
)


AVOIDED_PHRASES = (
    "drivers",
    "biggest drivers",
    "caused",
    "what made you tired",
    "you should",
    "you would have felt",
    "tomorrow prediction",
    "recommendations today",
    "Merged from Oura + 8 Sleep",
    "8 Sleep-adjusted sleep score",
    "8 Sleep says",
)


def trained_record() -> dict:
    return {
        "status": "trained",
        "trained_through_date": "2026-09-09",
        "n_model": 37,
        "latest_logged_feeling": 7.0,
        "baseline_gate_passed": True,
        "baseline_gate_reason": "passed",
        "latest_contributions": [
            {
                "feature_name": "total_sleep_min",
                "feature_value": 372.0,
                "contribution": -0.6,
                "stability_label": "stable",
            },
            {
                "feature_name": "hrv_z",
                "feature_value": -0.9,
                "contribution": -1.2,
                "stability_label": "low_confidence_signal",
            },
            {
                "feature_name": "deep_sleep_pct",
                "feature_value": 0.18,
                "contribution": 9.0,
                "stability_label": "suppressed",
            },
        ],
        "latest_display_metadata": {"hrv_avg_ms": 38.0},
        "latest_prediction_interval": {
            "prediction": 6.0, "low": 5.0, "high": 7.0, "full_width": 2.0
        },
        "confidence_label": "high",
        "model_version": "ridge-v1",
    }


class DisplayGateTests(unittest.TestCase):
    def view(self, record, *, model_ready_days=42, min_model_rows=37):
        return build_insight_view(
            latest_record=record,
            model_ready_days=model_ready_days,
            usual_values={"total_sleep_min": 450.0, "deep_sleep_pct": 0.2},
            min_model_rows=min_model_rows,
        )

    def test_collecting_without_record_uses_live_count_and_custom_minimum(self):
        view = self.view(None, model_ready_days=8)
        self.assertEqual(view.state, DisplayState.COLLECTING)
        self.assertEqual(
            view.message,
            "Collecting model-ready days: 8/37. Insights begin once we have 37 model-ready days.",
        )
        self.assertEqual(view.model_ready_days, 8)
        self.assertEqual(view.min_model_rows, 37)
        self.assertEqual(view.n_model, 0)
        custom = self.view(None, min_model_rows=40)
        self.assertEqual(custom.min_model_rows, 40)
        self.assertIn("42/40", custom.message)

    def test_collecting_takes_precedence_for_skipped_count_and_missing_rating(self):
        cases = (
            {"status": "skipped"},
            {"n_model": 36},
            {"latest_logged_feeling": None},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                record = trained_record()
                record.update(changes)
                record["baseline_gate_passed"] = False
                record["latest_contributions"] = []
                view = self.view(record)
                self.assertEqual(view.state, DisplayState.COLLECTING)
                self.assertEqual(view.contributors, ())
                self.assertIsNone(view.logged_feeling)
                self.assertIsNone(view.interval_low)
        record = trained_record()
        record.update(status="skipped", latest_logged_feeling=None, latest_prediction_interval=None)
        self.assertEqual(self.view(record).state, DisplayState.COLLECTING)
        self.assertEqual(self.view(trained_record()).state, DisplayState.SHOW)

    def test_failed_gate_preserves_reason_and_suppresses_every_model_output(self):
        record = trained_record()
        record.update(baseline_gate_passed=False, baseline_gate_reason="rmse_gate_failed")
        view = self.view(record)
        self.assertEqual(view.state, DisplayState.GATE_FAILED)
        self.assertEqual(
            view.message,
            "Model is not yet better than a simple baseline. Collecting more data.",
        )
        self.assertEqual(view.gate_reason, "rmse_gate_failed")
        self.assertEqual(view.n_model, 37)
        self.assertEqual(view.trained_through_date, date(2026, 9, 9))
        self.assertEqual(view.contributors, ())
        self.assertIsNone(view.confidence_label)
        record["latest_contributions"] = []
        self.assertEqual(self.view(record).state, DisplayState.GATE_FAILED)

    def test_all_suppressed_and_empty_contributions_have_insufficient_signal(self):
        record = trained_record()
        for item in record["latest_contributions"]:
            item["stability_label"] = "suppressed"
        for contributions in (record["latest_contributions"], []):
            with self.subTest(contributions=contributions):
                record["latest_contributions"] = contributions
                view = self.view(record)
                self.assertEqual(view.state, DisplayState.INSUFFICIENT_STABLE_SIGNAL)
                self.assertEqual(view.message, "Insufficient stable signal. Collecting more data.")
                self.assertEqual(view.contributors, ())
                self.assertIsNone(view.interval_low)

    def test_show_sorts_absolute_contribution_and_preserves_confidence_tiers(self):
        record = trained_record()
        original = deepcopy(record)
        view = self.view(record)
        self.assertEqual(view.state, DisplayState.SHOW)
        self.assertEqual(view.message, "")
        self.assertEqual(view.insight_date, date(2026, 9, 9))
        self.assertEqual(view.logged_feeling, 7)
        self.assertEqual(view.model_version, "ridge-v1")
        self.assertEqual([item.feature_name for item in view.contributors], ["hrv_z", "total_sleep_min"])
        hrv, sleep = view.contributors
        self.assertTrue(hrv.low_confidence)
        self.assertEqual(hrv.value_text, "−0.9 (38 ms)")
        self.assertEqual(hrv.direction_text, "▼ below your prior baseline")
        self.assertFalse(sleep.low_confidence)
        self.assertEqual(sleep.value_text, "6h 12m")
        self.assertEqual(sleep.direction_text, "▼ below your usual 7h 30m")
        self.assertEqual(view.interval_low, 5.0)
        self.assertEqual(view.interval_high, 7.0)
        self.assertEqual(view.confidence_label, "high")
        self.assertEqual(record, original)
        with self.assertRaises(FrozenInstanceError):
            view.message = "changed"
        with self.assertRaises(FrozenInstanceError):
            sleep.low_confidence = True

    def test_equal_absolute_contributions_follow_model_feature_order(self):
        record = trained_record()
        record["latest_contributions"] = [
            {"feature_name": name, "feature_value": value, "contribution": contribution, "stability_label": "stable"}
            for name, value, contribution in (
                ("prior_day_feeling", 6.0, -1.0),
                ("deep_sleep_pct", 0.18, 1.0),
                ("hrv_z", -0.9, -1.0),
                ("total_sleep_min", 372.0, 1.0),
            )
        ]
        self.assertEqual(
            [item.feature_name for item in self.view(record).contributors], list(FEATURE_DISPLAY)
        )

    def test_missing_confidence_bucket_is_derived_from_interval_width(self):
        for width, expected in ((2.0, "high"), (2.01, "medium"), (3.5, "medium"), (3.51, "low")):
            with self.subTest(width=width):
                record = trained_record()
                record.pop("confidence_label")
                record["latest_prediction_interval"].update(high=5.0 + width, full_width=width)
                self.assertEqual(self.view(record).confidence_label, expected)
                record["confidence_label"] = None
                self.assertEqual(self.view(record).confidence_label, expected)
        record = trained_record()
        record["confidence_label"] = "low"
        self.assertEqual(self.view(record).confidence_label, "low")

    def test_unused_hints_do_not_override_actual_gate_or_contributions(self):
        record = trained_record()
        record.update(
            baseline_gate_eligible=False,
            visible_contributor_features=["deep_sleep_pct"],
            sign_stable_features=[],
            feature_sign_stability=[],
            latest_feature_values={"total_sleep_min": 9999.0},
        )
        view = self.view(record)
        self.assertEqual(view.state, DisplayState.SHOW)
        self.assertEqual([item.feature_name for item in view.contributors], ["hrv_z", "total_sleep_min"])
        self.assertEqual(view.contributors[1].value_text, "6h 12m")

    def test_malformed_records_are_unavailable(self):
        malformed = [[], "record", 17, {}, {"status": "trained"}]
        for field, value in (
            ("status", None),
            ("n_model", "37"),
            ("n_model", True),
            ("n_model", -1),
            ("latest_logged_feeling", "7"),
            ("latest_logged_feeling", float("nan")),
            ("latest_logged_feeling", float("inf")),
            ("baseline_gate_passed", "true"),
            ("baseline_gate_reason", []),
            ("trained_through_date", "not-a-date"),
            ("trained_through_date", None),
            ("trained_through_date", 20260909),
            ("model_version", {}),
            ("latest_contributions", {}),
            ("latest_contributions", [None]),
            ("latest_display_metadata", "38"),
            ("latest_display_metadata", {"hrv_avg_ms": "38"}),
            ("latest_prediction_interval", None),
            ("latest_prediction_interval", {}),
            ("latest_prediction_interval", {"low": 7.0, "high": 5.0, "full_width": 2.0}),
            ("confidence_label", "certain"),
        ):
            record = trained_record()
            record[field] = value
            malformed.append(record)
        for field in ("status", "n_model", "latest_logged_feeling", "baseline_gate_passed", "latest_contributions", "latest_prediction_interval"):
            record = trained_record()
            del record[field]
            malformed.append(record)
        for field, value in (
            ("feature_name", "unknown"),
            ("feature_name", []),
            ("stability_label", "unknown"),
            ("feature_value", None),
            ("contribution", "0.5"),
            ("contribution", float("inf")),
        ):
            record = trained_record()
            record["latest_contributions"][0][field] = value
            malformed.append(record)
        for record in malformed:
            with self.subTest(record=record):
                view = self.view(record)
                self.assertEqual(view.state, DisplayState.UNAVAILABLE)
                self.assertEqual(view.message, "Model output is unavailable right now.")
                self.assertEqual(view.contributors, ())
                self.assertIsNone(view.interval_low)

    def test_every_state_message_avoids_forbidden_language(self):
        failed = trained_record()
        failed["baseline_gate_passed"] = False
        insufficient = trained_record()
        insufficient["latest_contributions"] = []
        for record in (None, failed, insufficient, trained_record(), {}):
            message = self.view(record).message.lower()
            for phrase in AVOIDED_PHRASES:
                with self.subTest(record=record, phrase=phrase):
                    self.assertNotIn(phrase.lower(), message)


class FeatureDisplayTests(unittest.TestCase):
    def test_display_names_are_immutable_and_in_feature_order(self):
        self.assertIsInstance(FEATURE_DISPLAY, MappingProxyType)
        self.assertEqual(list(FEATURE_DISPLAY.items()), [
            ("total_sleep_min", "Total sleep"),
            ("hrv_z", "HRV (z-score vs baseline)"),
            ("deep_sleep_pct", "Deep sleep %"),
            ("prior_day_feeling", "Yesterday's feeling"),
        ])
        with self.assertRaises(TypeError):
            FEATURE_DISPLAY["hrv_z"] = "changed"

    def test_feature_value_formatting(self):
        for name, value, expected in (
            ("total_sleep_min", 372.0, "6h 12m"),
            ("total_sleep_min", 365.0, "6h 05m"),
            ("total_sleep_min", 419.9, "7h 00m"),
            ("hrv_z", -0.9, "−0.9"),
            ("hrv_z", 1.2, "1.2"),
            ("deep_sleep_pct", 0.18, "18%"),
            ("prior_day_feeling", 6.0, "6 / 10"),
        ):
            with self.subTest(name=name, value=value):
                self.assertEqual(format_feature_value(name, value), expected)
        self.assertEqual(format_feature_value("hrv_z", -0.9, hrv_avg_ms=38.0), "−0.9 (38 ms)")
        with self.assertRaises(ValueError):
            format_feature_value("unknown", 1.0)

    def test_usual_directions_and_inclusive_five_percent_band(self):
        for value, expected in (
            (372.0, "▼ below your usual 7h 30m"),
            (500.0, "▲ above your usual 7h 30m"),
            (450.0, "≈ your usual 7h 30m"),
            (427.5, "≈ your usual 7h 30m"),
            (472.5, "≈ your usual 7h 30m"),
            (427.4, "▼ below your usual 7h 30m"),
            (472.6, "▲ above your usual 7h 30m"),
        ):
            with self.subTest(value=value):
                self.assertEqual(direction_text("total_sleep_min", value, 450.0), expected)
        self.assertEqual(direction_text("deep_sleep_pct", 0.18, 0.2), "▼ below your usual 20%")
        self.assertEqual(direction_text("deep_sleep_pct", 0.23, 0.2), "▲ above your usual 20%")
        self.assertEqual(direction_text("deep_sleep_pct", 0.19, 0.2), "≈ your usual 20%")
        self.assertEqual(direction_text("deep_sleep_pct", 0.21, 0.2), "≈ your usual 20%")
        self.assertEqual(direction_text("deep_sleep_pct", 0.0, 0.0), "≈ your usual 0%")
        self.assertIsNone(direction_text("total_sleep_min", 372.0, None))
        self.assertIsNone(direction_text("deep_sleep_pct", 0.18, None))
        self.assertIsNone(direction_text("prior_day_feeling", 6.0, 7.0))

    def test_hrv_directions_use_prior_baseline_with_inclusive_neutral_band(self):
        for value, expected in (
            (-0.26, "▼ below your prior baseline"),
            (0.26, "▲ above your prior baseline"),
            (-0.25, "≈ your prior baseline"),
            (0.25, "≈ your prior baseline"),
            (0.0, "≈ your prior baseline"),
        ):
            with self.subTest(value=value):
                self.assertEqual(direction_text("hrv_z", value, None), expected)


if __name__ == "__main__":
    unittest.main()


class CounterfactualViewTests(unittest.TestCase):
    AVAILABLE = {
        "status": "available",
        "suppression_reason": None,
        "counterfactual": {
            "feature_name": "total_sleep_min",
            "feature_display_name": "Total sleep",
            "actual_value": 372.0,
            "comparison_value": 447.5,
            "model_delta_low": 0.6,
            "model_delta_high": 1.2,
            "median_delta": 0.9,
            "direction": "increase_only",
            "framing_label": "model-estimated change in your past data",
            "caveat": "correlation, not proven causation",
        },
        "provenance": {"n_model": 40},
    }

    def test_available_payload_is_formatted_for_display(self) -> None:
        from src.model.display_gate import build_counterfactual_view

        view = build_counterfactual_view(self.AVAILABLE)
        self.assertEqual(view.status, "available")
        self.assertEqual(view.actual_text, "6h 12m")
        self.assertEqual(view.comparison_text, "7h 28m")
        self.assertEqual((view.delta_low, view.delta_median, view.delta_high), (0.6, 0.9, 1.2))
        self.assertEqual(view.message, "")

    def test_suppressed_reasons_map_to_explanation_framed_messages(self) -> None:
        from src.model.display_gate import build_counterfactual_view

        cases = {
            "actual_at_or_above_recent_median": "No useful sleep-increase comparison: that night's sleep was already at your normal upper range.",
            "delta_interval_not_positive": "The estimated change is too uncertain to call useful.",
            "delta_below_materiality_floor": "The estimated change is too uncertain to call useful.",
            "no_plausible_candidate": "The estimated change is too uncertain to call useful.",
            "empty_candidate_envelope": "The estimated change is too uncertain to call useful.",
            "mutable_feature_not_stable": "Insufficient stable signal for a sleep comparison.",
            "baseline_gate_not_passed": "Insufficient stable signal for a sleep comparison.",
        }
        for reason, expected in cases.items():
            with self.subTest(reason=reason):
                view = build_counterfactual_view({"status": "suppressed", "suppression_reason": reason})
                self.assertEqual(view.status, "suppressed")
                self.assertEqual(view.reason, reason)
                self.assertEqual(view.message, expected)
                self.assertIsNone(view.comparison_text)

    def test_malformed_or_contract_violating_payloads_collapse_to_unavailable(self) -> None:
        from src.model.display_gate import build_counterfactual_view

        base = deepcopy(self.AVAILABLE)
        violations = [
            None,
            {},
            {"status": "available"},
            {"status": "available", "counterfactual": {**base["counterfactual"], "feature_name": "hrv_z"}},
            {"status": "available", "counterfactual": {**base["counterfactual"], "framing_label": "drivers"}},
            {"status": "available", "counterfactual": {**base["counterfactual"], "comparison_value": 300.0}},
            {"status": "available", "counterfactual": {**base["counterfactual"], "model_delta_low": 0.0}},
            {"status": "available", "counterfactual": {**base["counterfactual"], "model_delta_low": "0.6"}},
            # Below the 7-hour safety floor even though it is an increase.
            {"status": "available", "counterfactual": {**base["counterfactual"], "actual_value": 372.0, "comparison_value": 400.0}},
            # Positive but immaterial interval.
            {"status": "available", "counterfactual": {**base["counterfactual"], "model_delta_low": 0.1, "median_delta": 0.2, "model_delta_high": 0.3}},
            {"status": "available", "counterfactual": {**base["counterfactual"], "direction": "decrease"}},
            {"status": "unavailable", "error_type": "ImportError"},
        ]
        for payload in violations:
            with self.subTest(payload=payload):
                view = build_counterfactual_view(payload)
                self.assertEqual(view.status, "unavailable")
                self.assertEqual(view.message, "Insufficient stable signal for a sleep comparison.")

    def test_show_state_carries_the_counterfactual_view(self) -> None:
        record = trained_record()
        record["latest_counterfactual"] = deepcopy(self.AVAILABLE)
        view = build_insight_view(
            latest_record=record,
            model_ready_days=42,
            usual_values={},
        )
        self.assertEqual(view.state, DisplayState.SHOW)
        self.assertIsNotNone(view.counterfactual)
        self.assertEqual(view.counterfactual.status, "available")
        self.assertEqual(view.counterfactual.comparison_text, "7h 28m")

from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, date, datetime, time, timedelta
import json
import os
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from streamlit.testing.v1 import AppTest

from app import explainer
from app.mood_form import format_date
from src.api.mood_date import resolve_mood_date
from src.warehouse.features import SleepProviderPolicy
from src.warehouse.locking import WarehouseLockTimeout, lock_path_for_database, warehouse_write_lock
from src.warehouse.warehouse import compute_daily_features, connect_duckdb, insert_sleep_night, persist_mood_entry_locked

APP_PATH = Path(__file__).resolve().parents[2] / "app" / "explainer.py"
FAKE_TOKEN = "test-explainer-token"
HOME_TZ = ZoneInfo("America/Toronto")
POLICY = SleepProviderPolicy("oura", "fallback_active", False)
AVOIDED_PHRASES = (
    "drivers", "biggest drivers", "caused", "what made you tired", "you should",
    "you would have felt", "tomorrow prediction", "recommendations today",
    "Merged from Oura + 8 Sleep", "8 Sleep-adjusted sleep score", "8 Sleep says",
)


def rendered_texts(at: AppTest) -> list[str]:
    texts = []
    for group in ("title", "header", "subheader", "markdown", "caption", "info", "warning", "success", "error", "text"):
        texts.extend(str(element.value) for element in getattr(at, group))
    for group in ("radio", "multiselect", "text_area", "text_input", "button"):
        texts.extend(str(element.label) for element in getattr(at, group))
    return texts


def eval_record(day: date, *, passed: bool = True) -> dict:
    contributions = [
        {"feature_name": "total_sleep_min", "feature_value": 372.0, "scaled_value": -1.0,
         "coefficient": 0.3, "contribution": -0.3, "pct_of_abs_contribution": 20.0,
         "sign_stability_pct": 0.95, "stability_label": "stable"},
        {"feature_name": "hrv_z", "feature_value": -0.9, "scaled_value": -0.9,
         "coefficient": 1.0, "contribution": -0.9, "pct_of_abs_contribution": 60.0,
         "sign_stability_pct": 0.85, "stability_label": "low_confidence_signal"},
        {"feature_name": "deep_sleep_pct", "feature_value": 0.18, "scaled_value": -0.2,
         "coefficient": 0.2, "contribution": -0.04, "pct_of_abs_contribution": 5.0,
         "sign_stability_pct": 0.70, "stability_label": "suppressed"},
    ]
    return {
        "date": day.isoformat(), "recorded_at_utc": datetime.combine(day, time(23), UTC).isoformat(),
        "trained_through_date": day.isoformat(), "status": "trained", "n_model": 37,
        "n_eval_days": 30, "eval_window_days": 30, "baseline_gate_eligible": True,
        "baseline_gate_passed": passed, "baseline_gate_reason": "fixture gate reason",
        "ridge_walk_forward_rmse": 0.8, "best_baseline_rmse": 1.1,
        "ridge_to_best_baseline_rmse_ratio": 0.73, "ridge_better_day_count": 25, "better_day_threshold": 21,
        "sign_stable_features": ["total_sleep_min"],
        "visible_contributor_features": ["total_sleep_min", "hrv_z"],
        "feature_sign_stability": [{key: row[key] for key in ("feature_name", "coefficient", "sign_stability_pct", "stability_label")} for row in contributions],
        "latest_contributions": contributions,
        "latest_feature_values": {"total_sleep_min": 372.0, "hrv_z": -0.9, "deep_sleep_pct": 0.18, "prior_day_feeling": 6.0},
        "latest_display_metadata": {"hrv_avg_ms": 38.0}, "latest_logged_feeling": 7.0,
        "latest_prediction_interval": {"prediction": 6.0, "low": 4.6, "high": 7.4, "full_width": 2.8},
        "confidence_label": "medium", "model_version": "fixture-model", "feature_version": "v1.0",
    }


class ExplainerTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.database = self.root / "warehouse.duckdb"
        self.model_dir = self.root / "fake-models"
        self.model_dir.mkdir()
        self.today = resolve_mood_date(datetime.now(UTC), HOME_TZ)
        env_patch = patch.dict(os.environ, {
            "MOOD_FORM_TOKEN": FAKE_TOKEN,
            "HOME_TIMEZONE": str(HOME_TZ),
            "HEALTH_HUB_DATABASE_PATH": str(self.database),
            "HEALTH_HUB_MODEL_DIR": str(self.model_dir),
        })
        env_patch.start()
        self.addCleanup(env_patch.stop)

    def rate(self, day: date, *, feeling: int = 7, energy: int | None = 6, chips: tuple = ()) -> None:
        persist_mood_entry_locked(self.database, {
            "log_id": uuid4(), "logged_at_utc": datetime.combine(day, time(23), UTC),
            "mood_date": day, "feeling": feeling, "energy": energy,
            "notes": None, "context_chips": chips, "source": "manual", "supersedes_log_id": None,
        }, general_log_path=self.root / "test.log")

    def sleep(
        self,
        day: date,
        *,
        source: str = "oura",
        minutes: int = 450,
        hrv: float = 40.0,
        waketime_utc: datetime | None = None,
    ) -> None:
        with warehouse_write_lock(lock_path_for_database(self.database)):
            conn = connect_duckdb(self.database, apply_schema=True)
            try:
                row = {
                    "source": source, "sleep_date": day, "total_sleep_min": minutes,
                    "deep_min": 90, "hrv_avg_ms": hrv, "sleep_score": 82,
                    "ingested_at_utc": datetime.combine(day, time(12), UTC),
                }
                if waketime_utc is not None:
                    row["bedtime_utc"] = waketime_utc - timedelta(hours=7)
                    row["waketime_utc"] = waketime_utc
                insert_sleep_night(
                    conn, row, quarantine_dir=self.root / "quarantine", general_log_path=self.root / "test.log"
                )
            finally:
                conn.close()

    def history(self, days: int = 16) -> None:
        dates = [self.today - timedelta(days=offset) for offset in reversed(range(days))]
        for index, day in enumerate(dates):
            self.rate(day)
            self.sleep(day, hrv=38.0 + index % 7)
        with warehouse_write_lock(lock_path_for_database(self.database)):
            conn = connect_duckdb(self.database)
            try:
                for day in dates:
                    compute_daily_features(conn, day, provider_policy=POLICY)
            finally:
                conn.close()

    def write_records(self, *records: object) -> None:
        (self.model_dir / "eval.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8"
        )

    def assert_safe(self, at: AppTest) -> None:
        self.assertEqual(list(at.exception), [])
        for text in rendered_texts(at):
            self.assertNotIn(FAKE_TOKEN, text)
            for phrase in AVOIDED_PHRASES:
                self.assertNotIn(phrase.lower(), text.lower())

    def start(self) -> AppTest:
        at = AppTest.from_file(str(APP_PATH), default_timeout=30).run()
        self.assert_safe(at)
        return at

    def test_no_database_collects_zero_and_has_exact_provider_caption(self) -> None:
        at = self.start()
        self.assertIn("Collecting model-ready days: 0/37. Insights begin once we have 37 model-ready days.", [item.value for item in at.info])
        self.assertIn("Sleep source: Oura · 8 Sleep: not active in v1 provider path", [item.value for item in at.caption])
        self.assertIn("No data yet.", [item.value for item in at.caption])
        self.assertFalse(self.database.exists())

    def test_no_eval_record_uses_live_model_ready_count(self) -> None:
        self.history()
        at = self.start()
        self.assertTrue(any("Collecting model-ready days: 9/37." in item.value for item in at.info))

    def test_gate_failed_never_renders_contributors(self) -> None:
        self.rate(self.today)
        self.write_records(eval_record(self.today, passed=False))
        at = self.start()
        self.assertIn("Model is not yet better than a simple baseline. Collecting more data.", [item.value for item in at.info])
        self.assertIn("Model-ready days: 37. Re-evaluated nightly.", [item.value for item in at.caption])
        text = "\n".join(rendered_texts(at))
        for phrase in ("Total sleep", "HRV (z-score vs baseline)", "Top model contributors", "Confidence:"):
            self.assertNotIn(phrase, text)

    def test_show_has_stability_order_directions_and_secondary_interval(self) -> None:
        self.history()
        self.write_records(eval_record(self.today))
        at = self.start()
        lines = [item.value for item in at.markdown]
        self.assertIn(f"**{format_date(self.today)} — you rated 7 / 10**", lines)
        contributors = [line for line in lines if line.startswith("•")]
        self.assertEqual(contributors, [
            "• HRV (z-score vs baseline) …… −0.9 (38 ms)  ▼ below your prior baseline *(low-confidence signal)*",
            "• Total sleep …… 6h 12m  ▼ below your usual 7h 30m",
        ])
        self.assertFalse(any("Deep sleep %" in line for line in lines))
        self.assertIn("**Confidence: medium**", lines)
        captions = [item.value for item in at.caption]
        self.assertIn("The retrospective counterfactual is not built yet in this version.", captions)
        self.assertIn("90% interval 4.6 to 7.4 · correlation, not proven causation · may reflect unmeasured factors like stress, illness, schedule", captions)
        charts = at.get("vega_lite_chart")
        self.assertEqual(len(charts), 2)
        for chart, measure in zip(charts, ("Feeling", "Sleep hours")):
            layers = json.loads(chart.proto.spec)["layer"]
            self.assertEqual({layer["encoding"]["y"]["field"] for layer in layers}, {measure})
            self.assertTrue(all(layer["mark"]["type"] != "text" for layer in layers))

    def test_mood_first_embeds_existing_form_and_reruns_after_save(self) -> None:
        at = self.start()
        self.assertEqual(at.text_input[0].label, "Access token")
        self.assertIn("Log today's feeling first", [item.value for item in at.subheader])
        at.text_input[0].input(FAKE_TOKEN)
        at.button[0].click()
        at.run()
        self.assert_safe(at)
        self.assertEqual(at.radio[0].label, "How did I feel overall today?")
        at.radio[0].set_value(7)
        at.button[0].click()
        at.run()
        self.assert_safe(at)
        self.assertEqual(len(at.radio), 0)
        self.assertEqual(len(at.text_input), 0)
        self.assertIn(f"Today's rating is logged ({format_date(self.today)}).", [item.value for item in at.caption])

    def test_today_show_record_is_hidden_until_rating_exists(self) -> None:
        self.sleep(self.today)
        self.write_records(eval_record(self.today))
        at = self.start()
        self.assertIn("Log today's feeling to see its retrospective insight.", [item.value for item in at.info])
        text = "\n".join(rendered_texts(at))
        for phrase in ("you rated", "Top model contributors", "Total sleep", "HRV (z-score", "Confidence:"):
            self.assertNotIn(phrase, text)

    def test_corrected_rating_suppresses_the_cached_card_until_retrain(self) -> None:
        self.history()
        self.write_records(eval_record(self.today))
        at = self.start()
        self.assertIn(f"**{format_date(self.today)} — you rated 7 / 10**", [item.value for item in at.markdown])

        self.rate(self.today, feeling=3)
        at = self.start()
        self.assertIn(
            f"The rating for {format_date(self.today)} changed after the last retrain. "
            "Tonight's retrain will refresh this insight.",
            [item.value for item in at.info],
        )
        text = "\n".join(rendered_texts(at))
        for phrase in ("you rated", "Top model contributors", "Confidence:"):
            self.assertNotIn(phrase, text)

    def test_cached_card_for_an_older_date_needs_a_current_rating(self) -> None:
        yesterday = self.today - timedelta(days=1)
        self.history()
        self.write_records(eval_record(yesterday))
        with warehouse_write_lock(lock_path_for_database(self.database)):
            conn = connect_duckdb(self.database)
            try:
                conn.execute("DELETE FROM mood_current WHERE mood_date = ?", [yesterday])
                conn.execute("CHECKPOINT")
            finally:
                conn.close()
        at = self.start()
        self.assertIn(
            f"Log the rating for {format_date(yesterday)} to see its retrospective insight.",
            [item.value for item in at.info],
        )
        self.assertNotIn("you rated", "\n".join(rendered_texts(at)))

    def test_freshness_is_measured_in_elapsed_hours_from_the_wake_time(self) -> None:
        self.rate(self.today)
        yesterday = self.today - timedelta(days=1)
        for hours_ago, expect_warning in ((40, True), (20, False)):
            with self.subTest(hours_ago=hours_ago):
                wake = datetime.now(UTC) - timedelta(hours=hours_ago)
                with warehouse_write_lock(lock_path_for_database(self.database)):
                    conn = connect_duckdb(self.database, apply_schema=True)
                    try:
                        insert_sleep_night(conn, {
                            "source": "oura", "sleep_date": yesterday, "total_sleep_min": 430,
                            "bedtime_utc": wake - timedelta(hours=7), "waketime_utc": wake,
                            "ingested_at_utc": datetime.now(UTC),
                        }, quarantine_dir=self.root / "quarantine", general_log_path=self.root / "test.log")
                    finally:
                        conn.close()
                at = self.start()
                warnings = [item.value for item in at.warning]
                if expect_warning:
                    self.assertIn(explainer.FRESHNESS_WARNING, warnings)
                else:
                    self.assertNotIn(explainer.FRESHNESS_WARNING, warnings)

    def test_freshness_old_yesterday_and_absent_with_ratings(self) -> None:
        self.rate(self.today)
        at = self.start()
        self.assertIn(explainer.FRESHNESS_WARNING, [item.value for item in at.warning])
        self.sleep(self.today - timedelta(days=3))
        at = self.start()
        self.assertIn(explainer.FRESHNESS_WARNING, [item.value for item in at.warning])
        # Freshness is elapsed time from the wake instant, so pin it to avoid a
        # clock-dependent result late in the evening.
        self.sleep(self.today - timedelta(days=1), waketime_utc=datetime.now(UTC) - timedelta(hours=12))
        at = self.start()
        self.assertNotIn(explainer.FRESHNESS_WARNING, [item.value for item in at.warning])

    def test_last_eval_line_and_pending_retrain_caption(self) -> None:
        yesterday = self.today - timedelta(days=1)
        self.rate(yesterday)
        self.rate(self.today)
        self.write_records(eval_record(self.today, passed=False), eval_record(yesterday))
        at = self.start()
        self.assertIn(f"**{format_date(yesterday)} — you rated 7 / 10**", [item.value for item in at.markdown])
        self.assertIn(f"Tonight's retrain will cover {format_date(self.today)}.", [item.value for item in at.caption])

    def test_insufficient_signal_and_malformed_log_are_safe(self) -> None:
        self.rate(self.today)
        record = eval_record(self.today)
        for row in record["latest_contributions"]:
            row["stability_label"] = "suppressed"
        self.write_records(record)
        at = self.start()
        self.assertIn("Insufficient stable signal. Collecting more data.", [item.value for item in at.info])
        for malformed in ("{incomplete", "[]\n"):
            (self.model_dir / "eval.jsonl").write_text(malformed, encoding="utf-8")
            at = self.start()
            self.assertIn("Model output is unavailable right now.", [item.value for item in at.warning])

    def test_missing_token_has_safe_error(self) -> None:
        with patch.dict(os.environ, {"MOOD_FORM_TOKEN": ""}):
            at = self.start()
        self.assertEqual([item.value for item in at.error], ["MOOD_FORM_TOKEN is not configured"])

    def test_busy_warehouse_is_notice_and_never_opens_connection(self) -> None:
        self.rate(self.today)
        with patch("src.warehouse.locking.warehouse_write_lock", side_effect=WarehouseLockTimeout), patch(
            "src.warehouse.warehouse.connect_duckdb"
        ) as connect:
            at = self.start()
            connect.assert_not_called()
        self.assertIn("The warehouse is busy. Try again in a few seconds.", [item.value for item in at.warning])

    def test_reads_hold_one_lock_and_close_before_verified_loader(self) -> None:
        self.rate(self.today)
        calls = []
        connection = connect_duckdb(self.database, read_only=True)

        class ConnectionProxy:
            def __getattr__(self, name):
                return getattr(connection, name)

            def close(self):
                connection.close()
                calls.append("close")

        @contextmanager
        def lock(path, *, timeout_seconds):
            self.assertEqual(path, lock_path_for_database(self.database))
            self.assertEqual(timeout_seconds, 3.0)
            calls.append("lock")
            yield
            calls.append("unlock")

        def open_connection(*args, **kwargs):
            self.assertEqual(calls, ["lock"])
            calls.append("connect")
            return ConnectionProxy()

        def verified(path):
            self.assertEqual(calls, ["lock", "connect", "close"])
            calls.append("verified")
            return []

        with patch.object(explainer, "warehouse_write_lock", lock), patch.object(
            explainer, "connect_duckdb", open_connection
        ), patch.object(explainer, "load_verified_feature_rows", verified):
            data = explainer.read_explainer_data(self.database, home_tz=HOME_TZ, today=self.today)
        self.assertTrue(data.available)
        self.assertEqual(calls, ["lock", "connect", "close", "verified", "unlock"])

    def test_recent_raw_union_never_uses_other_provider_or_imputes(self) -> None:
        yesterday = self.today - timedelta(days=1)
        self.rate(self.today, energy=None, chips=("travel", "late_meal"))
        self.sleep(yesterday, minutes=372)
        self.sleep(self.today, source="8sleep", minutes=600)
        self.rate(self.today - timedelta(days=28))
        data = explainer.read_explainer_data(self.database, home_tz=HOME_TZ, today=self.today)
        self.assertTrue(data.available)
        self.assertEqual(data.latest_sleep_date, yesterday)
        self.assertEqual(data.latest_mood_date, self.today)
        self.assertEqual(data.usual_values, {})
        self.assertEqual(explainer.timeline_rows(data), [
            {"Date": self.today.isoformat(), "Feeling": "7", "Energy": "", "Sleep": "", "HRV ms": "", "Deep %": "", "Score": "", "Context chips": "travel, late_meal"},
            {"Date": yesterday.isoformat(), "Feeling": "", "Energy": "", "Sleep": "6:12", "HRV ms": "40", "Deep %": "24%", "Score": "82", "Context chips": ""},
        ])

    def test_usual_values_use_last_28_model_ready_rows_and_need_seven(self) -> None:
        self.rate(self.today)
        rows = [{"total_sleep_min": 300 + index * 10, "deep_sleep_pct": index / 100} for index in range(35)]
        with patch.object(explainer, "load_verified_feature_rows", return_value=rows):
            data = explainer.read_explainer_data(self.database, home_tz=HOME_TZ, today=self.today)
        self.assertEqual(data.model_ready_days, 35)
        self.assertEqual(data.usual_values, {name: median(row[name] for row in rows[-28:]) for name in ("total_sleep_min", "deep_sleep_pct")})
        with patch.object(explainer, "load_verified_feature_rows", return_value=rows[:6]):
            data = explainer.read_explainer_data(self.database, home_tz=HOME_TZ, today=self.today)
        self.assertEqual(data.usual_values, {})


if __name__ == "__main__":
    unittest.main()

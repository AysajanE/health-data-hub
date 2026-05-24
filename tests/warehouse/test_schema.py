from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

import duckdb
from pydantic import ValidationError

from src.warehouse.models import (
    DailyFeaturesRow,
    MoodCurrentRow,
    MoodEntryRow,
    SleepMergeDiagnosticsRow,
    SleepNightRow,
    ValidationFailureMetadata,
)
from src.warehouse.warehouse import compute_daily_features, connect_duckdb, insert_mood_entry, insert_sleep_night


REPO_ROOT = Path(__file__).resolve().parents[2]
SCHEMA_PATH = REPO_ROOT / "src" / "db" / "schema.sql"
EXPECTED_TABLES = {
    "daily_features",
    "mood_current",
    "mood_entries",
    "sleep_merge_diagnostics",
    "sleep_nights",
}


class SchemaSqlTest(unittest.TestCase):
    def test_schema_creates_expected_core_tables(self) -> None:
        self.assertTrue(SCHEMA_PATH.exists(), f"missing schema file: {SCHEMA_PATH}")

        conn = duckdb.connect(database=":memory:")
        try:
            conn.execute(SCHEMA_PATH.read_text(encoding="utf-8"))
            rows = conn.execute("SHOW TABLES").fetchall()
        finally:
            conn.close()

        created_tables = {name for (name,) in rows}
        self.assertEqual(created_tables, EXPECTED_TABLES)


class WarehouseModelsTest(unittest.TestCase):
    def test_sleep_night_row_accepts_schema_aligned_payload(self) -> None:
        row = SleepNightRow(
            source="oura",
            sleep_date=date(2026, 5, 23),
            bedtime_utc=datetime(2026, 5, 22, 21, 30, tzinfo=UTC),
            waketime_utc=datetime(2026, 5, 23, 5, 45, tzinfo=UTC),
            total_sleep_min=450,
            rem_min=90,
            deep_min=95,
            light_min=240,
            awake_min=25,
            hrv_avg_ms=42.5,
            rhr_avg_bpm=54,
            body_temp_dev_c=0.1,
            sleep_score=81,
            ingested_at_utc=datetime(2026, 5, 23, 6, 0, tzinfo=UTC),
        )

        self.assertEqual(row.source, "oura")
        self.assertEqual(row.sleep_date, date(2026, 5, 23))

    def test_mood_entry_row_rejects_invalid_scores_and_context_chips(self) -> None:
        with self.assertRaises(ValidationError):
            MoodEntryRow(
                log_id=uuid4(),
                logged_at_utc=datetime(2026, 5, 23, 22, 0, tzinfo=UTC),
                mood_date=date(2026, 5, 23),
                feeling=11,
                energy=3,
                notes="late dinner",
                context_chips=["travel", "unknown_chip"],
                source="ios_shortcut",
                supersedes_log_id=None,
            )

    def test_daily_features_row_rejects_out_of_range_deep_sleep_fraction(self) -> None:
        with self.assertRaises(ValidationError):
            DailyFeaturesRow(
                feature_date=date(2026, 5, 23),
                total_sleep_min=440,
                hrv_z=0.7,
                deep_sleep_pct=1.2,
                prior_day_feeling=6,
                hrv_avg_ms=41.8,
                hrv_z_method="prior_28d",
                feature_version="v1.0",
                prior_day_feeling_imputed=False,
                sleep_source_count=1,
                sleep_merge_warning=None,
                computed_at_utc=datetime(2026, 5, 23, 9, 0, tzinfo=UTC),
            )

    def test_auxiliary_row_models_accept_schema_aligned_payload(self) -> None:
        current_row = MoodCurrentRow(
            mood_date=date(2026, 5, 23),
            log_id=uuid4(),
        )
        diagnostics_row = SleepMergeDiagnosticsRow(
            sleep_date=date(2026, 5, 23),
            oura_present=True,
            eight_present=False,
            total_sleep_delta_min=0,
            hrv_merge_method="oura_primary",
            stage_source="oura",
            warning=None,
            computed_at_utc=datetime(2026, 5, 23, 9, 0, tzinfo=UTC),
        )

        self.assertEqual(current_row.mood_date, date(2026, 5, 23))
        self.assertEqual(diagnostics_row.hrv_merge_method, "oura_primary")

    def test_validation_failure_metadata_hashes_payload_and_summarizes_errors(self) -> None:
        payload = {
            "logged_at_utc": "2026-05-23T22:00:00Z",
            "mood_date": "2026-05-23",
            "feeling": 11,
            "context_chips": ["travel"],
            "source": "ios_shortcut",
        }

        with self.assertRaises(ValidationError) as captured:
            MoodEntryRow.model_validate(payload)

        metadata = ValidationFailureMetadata.from_validation_error(
            source="ios_shortcut",
            payload=payload,
            error=captured.exception,
            detected_at_utc=datetime(2026, 5, 23, 22, 1, tzinfo=UTC),
        )

        self.assertEqual(metadata.source, "ios_shortcut")
        self.assertEqual(metadata.detected_at_utc, datetime(2026, 5, 23, 22, 1, tzinfo=UTC))
        self.assertIn("feeling", metadata.error_summary)
        self.assertRegex(metadata.payload_hash, r"^[0-9a-f]{64}$")


class WarehouseWriteApiTest(unittest.TestCase):
    def test_connect_duckdb_creates_missing_parent_directory_for_filesystem_path(self) -> None:
        with TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "nested" / "warehouse.duckdb"

            conn = connect_duckdb(db_path, apply_schema=True)
            try:
                tables = conn.execute("SHOW TABLES").fetchall()
            finally:
                conn.close()

            self.assertTrue(db_path.parent.exists())
            self.assertTrue(db_path.exists())
            self.assertEqual({name for (name,) in tables}, EXPECTED_TABLES)

    def test_insert_sleep_night_persists_row_into_duckdb(self) -> None:
        conn = connect_duckdb(":memory:", apply_schema=True)
        try:
            row = insert_sleep_night(
                conn,
                {
                    "source": "oura",
                    "sleep_date": date(2026, 5, 23),
                    "bedtime_utc": datetime(2026, 5, 22, 21, 30, tzinfo=UTC),
                    "waketime_utc": datetime(2026, 5, 23, 5, 45, tzinfo=UTC),
                    "total_sleep_min": 450,
                    "rem_min": 90,
                    "deep_min": 90,
                    "light_min": 240,
                    "awake_min": 30,
                    "hrv_avg_ms": 44.0,
                    "rhr_avg_bpm": 53,
                    "body_temp_dev_c": 0.2,
                    "sleep_score": 82,
                    "ingested_at_utc": datetime(2026, 5, 23, 6, 0, tzinfo=UTC),
                },
            )
            stored = conn.execute(
                "SELECT source, sleep_date, total_sleep_min, hrv_avg_ms FROM sleep_nights"
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(row.source, "oura")
        self.assertEqual(stored, ("oura", date(2026, 5, 23), 450, 44.0))

    def test_compute_daily_features_builds_v1_row_and_diagnostics(self) -> None:
        feature_date = date(2026, 5, 23)
        conn = connect_duckdb(":memory:", apply_schema=True)
        try:
            for offset, hrv_value in enumerate([37.0, 38.5, 39.0, 40.0, 41.5, 42.0, 43.0], start=1):
                sleep_date = date(2026, 5, 23 - (8 - offset))
                insert_sleep_night(
                    conn,
                    {
                        "source": "oura",
                        "sleep_date": sleep_date,
                        "bedtime_utc": datetime(2026, 5, sleep_date.day - 1, 21, 30, tzinfo=UTC),
                        "waketime_utc": datetime(2026, 5, sleep_date.day, 5, 45, tzinfo=UTC),
                        "total_sleep_min": 430 + offset,
                        "rem_min": 90,
                        "deep_min": 86,
                        "light_min": 230,
                        "awake_min": 25,
                        "hrv_avg_ms": hrv_value,
                        "rhr_avg_bpm": 53,
                        "body_temp_dev_c": 0.0,
                        "sleep_score": 79,
                        "ingested_at_utc": datetime(2026, 5, sleep_date.day, 6, 0, tzinfo=UTC),
                    },
                )

            insert_mood_entry(
                conn,
                {
                    "log_id": uuid4(),
                    "logged_at_utc": datetime(2026, 5, 22, 22, 0, tzinfo=UTC),
                    "mood_date": date(2026, 5, 22),
                    "feeling": 6,
                    "energy": 6,
                    "notes": None,
                    "context_chips": (),
                    "source": "manual",
                    "supersedes_log_id": None,
                },
            )
            insert_sleep_night(
                conn,
                {
                    "source": "oura",
                    "sleep_date": feature_date,
                    "bedtime_utc": datetime(2026, 5, 22, 21, 45, tzinfo=UTC),
                    "waketime_utc": datetime(2026, 5, 23, 5, 45, tzinfo=UTC),
                    "total_sleep_min": 450,
                    "rem_min": 100,
                    "deep_min": 90,
                    "light_min": 230,
                    "awake_min": 30,
                    "hrv_avg_ms": 45.0,
                    "rhr_avg_bpm": 52,
                    "body_temp_dev_c": 0.1,
                    "sleep_score": 83,
                    "ingested_at_utc": datetime(2026, 5, 23, 6, 15, tzinfo=UTC),
                },
            )

            row = compute_daily_features(conn, feature_date)
            diagnostics = conn.execute(
                """
                SELECT hrv_merge_method, stage_source, warning
                FROM sleep_merge_diagnostics
                WHERE sleep_date = ?
                """,
                [feature_date],
            ).fetchone()
        finally:
            conn.close()

        self.assertIsNotNone(row)
        assert row is not None
        self.assertEqual(row.feature_date, feature_date)
        self.assertEqual(row.total_sleep_min, 450)
        self.assertAlmostEqual(row.deep_sleep_pct or 0.0, 0.2)
        self.assertEqual(row.prior_day_feeling, 6)
        self.assertFalse(row.prior_day_feeling_imputed)
        self.assertEqual(row.sleep_source_count, 1)
        self.assertEqual(row.sleep_merge_warning, None)
        self.assertEqual(row.hrv_avg_ms, 45.0)
        self.assertEqual(row.hrv_z_method, "prior_28d")
        self.assertIsNotNone(row.hrv_z)
        self.assertEqual(diagnostics, ("oura_primary", "oura", None))


if __name__ == "__main__":
    unittest.main()

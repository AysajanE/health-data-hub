from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from src.warehouse.models import DailyFeaturesRow, SleepNightRow
from src.warehouse.warehouse import (
    connect_duckdb,
    insert_daily_features_row,
    insert_sleep_night,
    select_daily_features,
    select_sleep_nights,
)


class WarehouseReadsTest(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.database = Path(temporary.name).resolve() / "warehouse.duckdb"
        self.conn = connect_duckdb(self.database, apply_schema=True)
        self.addCleanup(self.conn.close)
        self.timestamp = datetime(2026, 9, 10, 12, tzinfo=UTC)
        self.dates = [date(2026, 9, day) for day in (7, 8, 9)]

    def add_sleep(self) -> None:
        for day in reversed(self.dates):
            for source in ("8sleep", "oura"):
                insert_sleep_night(
                    self.conn,
                    {
                        "source": source,
                        "sleep_date": day,
                        "total_sleep_min": 450 if source == "oura" else 400,
                        "deep_min": 90,
                        "hrv_avg_ms": 38.5,
                        "sleep_score": 82,
                        "ingested_at_utc": self.timestamp,
                    },
                )

    def add_features(self) -> None:
        for day in reversed(self.dates):
            insert_daily_features_row(
                self.conn,
                {
                    "feature_date": day,
                    "total_sleep_min": 450,
                    "hrv_z": -0.9,
                    "deep_sleep_pct": 0.2,
                    "prior_day_feeling": 6,
                    "hrv_avg_ms": 38.5,
                    "hrv_z_method": "prior_28d",
                    "feature_version": "v1.0",
                    "sleep_source_count": 1,
                    "computed_at_utc": self.timestamp,
                },
            )

    def test_sleep_defaults_to_oura_and_returns_ordered_typed_rows(self) -> None:
        self.add_sleep()

        rows = select_sleep_nights(self.conn)

        self.assertEqual([row.sleep_date for row in rows], self.dates)
        self.assertTrue(all(isinstance(row, SleepNightRow) for row in rows))
        self.assertEqual({row.source for row in rows}, {"oura"})
        self.assertEqual(rows[0].total_sleep_min, 450)
        self.assertEqual(rows[0].hrv_avg_ms, 38.5)
        self.assertEqual(rows[0].sleep_score, 82)
        self.assertEqual(rows[0].ingested_at_utc, self.timestamp)

    def test_sleep_source_filter_is_explicit_and_parameterized(self) -> None:
        self.add_sleep()

        rows = select_sleep_nights(self.conn, source="8sleep")

        self.assertEqual([row.sleep_date for row in rows], self.dates)
        self.assertEqual({row.source for row in rows}, {"8sleep"})
        self.assertEqual(rows[0].total_sleep_min, 400)
        self.assertEqual(select_sleep_nights(self.conn, source="oura' OR 1=1 --"), [])

    def test_sleep_date_bounds_are_inclusive(self) -> None:
        self.add_sleep()
        cases = (
            ({"start_date": self.dates[1]}, self.dates[1:]),
            ({"end_date": self.dates[1]}, self.dates[:2]),
            ({"start_date": self.dates[1], "end_date": self.dates[1]}, self.dates[1:2]),
            ({"start_date": self.dates[2], "end_date": self.dates[0]}, []),
        )
        for bounds, expected in cases:
            with self.subTest(bounds=bounds):
                rows = select_sleep_nights(self.conn, **bounds)
                self.assertEqual([row.sleep_date for row in rows], expected)

    def test_daily_features_include_unlabeled_ordered_typed_rows(self) -> None:
        self.add_features()

        rows = select_daily_features(self.conn)

        self.assertEqual([row.feature_date for row in rows], self.dates)
        self.assertTrue(all(isinstance(row, DailyFeaturesRow) for row in rows))
        self.assertEqual(rows[0].total_sleep_min, 450)
        self.assertEqual(rows[0].hrv_z, -0.9)
        self.assertEqual(rows[0].deep_sleep_pct, 0.2)
        self.assertEqual(rows[0].prior_day_feeling, 6)
        self.assertEqual(rows[0].hrv_avg_ms, 38.5)
        self.assertEqual(rows[0].feature_version, "v1.0")
        self.assertEqual(rows[0].computed_at_utc, self.timestamp)

    def test_daily_features_date_bounds_are_inclusive(self) -> None:
        self.add_features()
        cases = (
            ({"start_date": self.dates[1]}, self.dates[1:]),
            ({"end_date": self.dates[1]}, self.dates[:2]),
            ({"start_date": self.dates[1], "end_date": self.dates[1]}, self.dates[1:2]),
            ({"start_date": self.dates[2], "end_date": self.dates[0]}, []),
        )
        for bounds, expected in cases:
            with self.subTest(bounds=bounds):
                rows = select_daily_features(self.conn, **bounds)
                self.assertEqual([row.feature_date for row in rows], expected)

    def test_empty_tables_return_empty_lists(self) -> None:
        self.assertEqual(select_sleep_nights(self.conn), [])
        self.assertEqual(select_daily_features(self.conn), [])


if __name__ == "__main__":
    unittest.main()

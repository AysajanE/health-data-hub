from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4
from zoneinfo import ZoneInfo

from src.warehouse.warehouse import (
    connect_duckdb,
    insert_mood_entry,
    insert_sleep_night,
    select_current_mood_entries,
)


LOGGED_AT = datetime(2026, 9, 10, 20, 0, tzinfo=UTC)
BEDTIME = datetime(2026, 9, 10, 3, 14, 15, tzinfo=UTC)
WAKETIME = datetime(2026, 9, 10, 11, 36, 41, tzinfo=UTC)


class UtcTimestampRoundTripTest(unittest.TestCase):
    """Regression: DuckDB's session TimeZone defaults to the machine zone, which
    silently shifted every stored *_utc instant by the local offset."""

    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "warehouse.duckdb"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_session_timezone_is_utc_for_writers_and_readers(self) -> None:
        conn = connect_duckdb(self.database_path, apply_schema=True)
        try:
            self.assertEqual(conn.execute("SELECT current_setting('TimeZone')").fetchone()[0], "UTC")
        finally:
            conn.close()
        conn = connect_duckdb(self.database_path, read_only=True)
        try:
            self.assertEqual(conn.execute("SELECT current_setting('TimeZone')").fetchone()[0], "UTC")
        finally:
            conn.close()

    def test_mood_and_sleep_instants_round_trip_exactly(self) -> None:
        conn = connect_duckdb(self.database_path, apply_schema=True)
        try:
            insert_mood_entry(
                conn,
                {
                    "log_id": uuid4(),
                    "logged_at_utc": LOGGED_AT.astimezone(ZoneInfo("America/Toronto")),
                    "mood_date": date(2026, 9, 10),
                    "feeling": 6,
                    "energy": None,
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
                    "sleep_date": date(2026, 9, 10),
                    "bedtime_utc": BEDTIME,
                    "waketime_utc": WAKETIME,
                    "total_sleep_min": 452,
                    "ingested_at_utc": LOGGED_AT,
                },
            )
            raw_sleep = conn.execute("SELECT bedtime_utc, waketime_utc FROM sleep_nights").fetchone()
        finally:
            conn.close()

        # Stored naive values must be the UTC wall clock, not the local one.
        self.assertEqual(raw_sleep, (BEDTIME.replace(tzinfo=None), WAKETIME.replace(tzinfo=None)))

        conn = connect_duckdb(self.database_path, read_only=True)
        try:
            entries = select_current_mood_entries(conn)
        finally:
            conn.close()
        self.assertEqual(entries[0].logged_at_utc, LOGGED_AT)


if __name__ == "__main__":
    unittest.main()

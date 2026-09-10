from __future__ import annotations

from datetime import UTC, date, datetime
import unittest
from zoneinfo import ZoneInfo

from src.ingestion.oura_sync import (
    OuraSyncError,
    map_sleep_record,
    select_main_sleep_records,
)


class OuraDateMappingTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home_tz = ZoneInfo("America/Toronto")
        self.ingested_at = datetime(2026, 12, 1, 12, tzinfo=UTC)

    def record(self, bedtime_start: str, bedtime_end: str, **changes: object) -> dict:
        record = {
            "id": "fixture-sleep",
            "day": "2026-09-10",
            "type": "long_sleep",
            "bedtime_start": bedtime_start,
            "bedtime_end": bedtime_end,
            "total_sleep_duration": 25_200,
            "deep_sleep_duration": None,
            "light_sleep_duration": None,
            "rem_sleep_duration": None,
            "awake_time": None,
            "average_hrv": None,
            "average_heart_rate": None,
            "readiness": None,
        }
        record.update(changes)
        return record

    def map_record(self, record: dict, *, home_tz: ZoneInfo | None = None) -> dict:
        return map_sleep_record(
            record,
            home_tz=home_tz or self.home_tz,
            ingested_at_utc=self.ingested_at,
            sleep_score=None,
        )

    def test_dst_start_night_maps_offsets_to_utc_and_wake_day(self) -> None:
        record = self.record(
            "2026-03-07T23:30:00-05:00",
            "2026-03-08T07:30:00-04:00",
            day="2026-03-08",
        )
        mapped = self.map_record(record)

        self.assertEqual(mapped["sleep_date"], date(2026, 3, 8))
        self.assertEqual(mapped["bedtime_utc"], datetime(2026, 3, 8, 4, 30, tzinfo=UTC))
        self.assertEqual(mapped["waketime_utc"], datetime(2026, 3, 8, 11, 30, tzinfo=UTC))
        self.assertEqual(set(select_main_sleep_records([record], self.home_tz)), {date(2026, 3, 8)})

    def test_dst_end_night_maps_offsets_to_utc_and_wake_day(self) -> None:
        record = self.record(
            "2026-10-31T23:30:00-04:00",
            "2026-11-01T07:30:00-05:00",
            day="2026-11-01",
        )
        mapped = self.map_record(record)

        self.assertEqual(mapped["sleep_date"], date(2026, 11, 1))
        self.assertEqual(mapped["bedtime_utc"], datetime(2026, 11, 1, 3, 30, tzinfo=UTC))
        self.assertEqual(mapped["waketime_utc"], datetime(2026, 11, 1, 12, 30, tzinfo=UTC))

    def test_ending_just_after_local_midnight_uses_new_calendar_day(self) -> None:
        record = self.record(
            "2026-09-09T16:30:00-04:00",
            "2026-09-10T00:15:00-04:00",
        )

        self.assertEqual(self.map_record(record)["sleep_date"], date(2026, 9, 10))

    def test_utc_next_day_can_still_be_previous_day_in_toronto(self) -> None:
        record = self.record(
            "2026-09-10T18:00:00+00:00",
            "2026-09-11T01:30:00+00:00",
            day="2026-09-11",
        )

        self.assertEqual(self.map_record(record)["sleep_date"], date(2026, 9, 10))
        self.assertEqual(set(select_main_sleep_records([record], self.home_tz)), {date(2026, 9, 10)})

    def test_local_next_day_before_utc_midnight_uses_eastward_home_timezone(self) -> None:
        # Toronto is west of UTC, so the converse midnight ordering requires
        # an eastern home timezone, not a contradictory Toronto fixture.
        record = self.record(
            "2026-09-10T15:00:00+00:00",
            "2026-09-10T22:30:00+00:00",
        )
        home_tz = ZoneInfo("Asia/Tokyo")

        self.assertEqual(self.map_record(record, home_tz=home_tz)["sleep_date"], date(2026, 9, 11))
        self.assertEqual(set(select_main_sleep_records([record], home_tz)), {date(2026, 9, 11)})

    def test_home_timezone_is_authoritative_even_when_record_day_disagrees(self) -> None:
        record = self.record(
            "2026-09-09T20:00:00-04:00",
            "2026-09-10T03:30:00-04:00",
            day="2026-09-09",
        )

        self.assertEqual(self.map_record(record)["sleep_date"], date(2026, 9, 10))
        self.assertEqual(set(select_main_sleep_records([record], self.home_tz)), {date(2026, 9, 10)})

    def test_naive_bedtime_or_waketime_is_rejected(self) -> None:
        timestamps = {
            "bedtime_start": "2026-09-09T23:00:00-04:00",
            "bedtime_end": "2026-09-10T07:00:00-04:00",
        }
        for field in timestamps:
            with self.subTest(field=field):
                record = self.record(**timestamps)
                record[field] = record[field][:-6]

                with self.assertRaises(OuraSyncError):
                    self.map_record(record)

        record = self.record(**timestamps)
        record["bedtime_end"] = "2026-09-10T07:00:00"
        with self.assertRaises(OuraSyncError):
            select_main_sleep_records([record], self.home_tz)

    def test_invalid_timestamp_errors_do_not_echo_payload(self) -> None:
        private_value = "do-not-echo-health-payload-938271"
        record = self.record("2026-09-09T23:00:00-04:00", private_value)

        for operation in (
            lambda: self.map_record(record),
            lambda: select_main_sleep_records([record], self.home_tz),
        ):
            with self.subTest(operation=operation):
                with self.assertRaises(OuraSyncError) as caught:
                    operation()
                self.assertNotIn(private_value, str(caught.exception))


if __name__ == "__main__":
    unittest.main()

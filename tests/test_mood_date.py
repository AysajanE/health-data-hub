from __future__ import annotations

from datetime import UTC, date, datetime
import unittest
from zoneinfo import ZoneInfo

from src.api.mood_date import resolve_mood_date


HOME_TIMEZONE = "America/Toronto"


class ResolveMoodDateTest(unittest.TestCase):
    home_tz = ZoneInfo(HOME_TIMEZONE)

    def test_cutoff_examples(self) -> None:
        cases = (
            (datetime(2026, 5, 23, 23, 30, tzinfo=self.home_tz), date(2026, 5, 23)),
            (datetime(2026, 5, 24, 0, 30, tzinfo=self.home_tz), date(2026, 5, 23)),
            (datetime(2026, 5, 24, 3, 59, tzinfo=self.home_tz), date(2026, 5, 23)),
            (datetime(2026, 5, 24, 4, 1, tzinfo=self.home_tz), date(2026, 5, 24)),
        )

        for logged_at_local, expected_date in cases:
            with self.subTest(logged_at_local=logged_at_local.isoformat()):
                logged_at_utc = logged_at_local.astimezone(UTC)
                self.assertEqual(resolve_mood_date(logged_at_utc, self.home_tz), expected_date)

    def test_dst_transition_night_stays_previous_day_before_cutoff(self) -> None:
        logged_at_local = datetime(2024, 3, 10, 3, 30, tzinfo=self.home_tz)
        logged_at_utc = logged_at_local.astimezone(UTC)

        self.assertEqual(
            resolve_mood_date(logged_at_utc, HOME_TIMEZONE),
            date(2024, 3, 9),
        )

    def test_rejects_naive_datetime_to_avoid_ambient_local_time(self) -> None:
        with self.assertRaises(ValueError):
            resolve_mood_date(datetime(2026, 5, 24, 7, 30), HOME_TIMEZONE)


if __name__ == "__main__":
    unittest.main()

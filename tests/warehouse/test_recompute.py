from __future__ import annotations

from datetime import UTC, date, datetime
import unittest
from unittest.mock import call, patch, sentinel

from src.warehouse.recompute import recompute_daily_features


class RecomputeDailyFeaturesTest(unittest.TestCase):
    def setUp(self) -> None:
        self.computed_at = datetime(2026, 9, 10, 12, tzinfo=UTC)

    def test_visits_inclusive_dates_in_ascending_order_and_counts_non_none(self) -> None:
        dates = [date(2026, 2, 27), date(2026, 2, 28), date(2026, 3, 1), date(2026, 3, 2)]
        with patch(
            "src.warehouse.recompute.compute_daily_features",
            side_effect=[sentinel.first_row, None, {}, sentinel.last_row],
        ) as compute:
            count = recompute_daily_features(
                sentinel.connection,
                start_date=dates[0],
                end_date=dates[-1],
                provider_policy=sentinel.provider_policy,
                computed_at_utc=self.computed_at,
            )

        self.assertEqual(count, 3)
        self.assertEqual(
            compute.call_args_list,
            [
                call(
                    sentinel.connection,
                    feature_date,
                    computed_at_utc=self.computed_at,
                    provider_policy=sentinel.provider_policy,
                )
                for feature_date in dates
            ],
        )

    def test_single_day_is_included_once(self) -> None:
        feature_date = date(2026, 9, 10)
        with patch(
            "src.warehouse.recompute.compute_daily_features",
            return_value=sentinel.row,
        ) as compute:
            count = recompute_daily_features(
                sentinel.connection,
                start_date=feature_date,
                end_date=feature_date,
                provider_policy=sentinel.provider_policy,
                computed_at_utc=self.computed_at,
            )

        self.assertEqual(count, 1)
        compute.assert_called_once_with(
            sentinel.connection,
            feature_date,
            computed_at_utc=self.computed_at,
            provider_policy=sentinel.provider_policy,
        )

    def test_reversed_range_writes_no_rows(self) -> None:
        with patch("src.warehouse.recompute.compute_daily_features") as compute:
            count = recompute_daily_features(
                sentinel.connection,
                start_date=date(2026, 9, 11),
                end_date=date(2026, 9, 10),
                provider_policy=sentinel.provider_policy,
                computed_at_utc=self.computed_at,
            )

        self.assertEqual(count, 0)
        compute.assert_not_called()


if __name__ == "__main__":
    unittest.main()

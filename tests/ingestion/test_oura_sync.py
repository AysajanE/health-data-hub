from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit
from zoneinfo import ZoneInfo

from src.ingestion import oura_sync as sync_module
from src.ingestion.oura_auth import OuraAuthError, OuraCredentials, TokenStore
from src.ingestion.oura_sync import (
    OuraSyncError, fetch_collection, map_sleep_record, select_main_sleep_records, sync_oura,
)
from src.warehouse.features import SleepProviderPolicy, compute_prior_only_hrv_z
from src.warehouse.locking import lock_path_for_database, warehouse_write_lock
from src.warehouse.warehouse import connect_duckdb, insert_sleep_night


NOW = datetime(2026, 9, 10, 20, tzinfo=UTC)
DAY = date(2026, 9, 10)
TZ = ZoneInfo("America/Toronto")
POLICY = SleepProviderPolicy("oura", "fallback_active", False)


def sleep_record(day=DAY, **changes):
    record = {
        "id": "private-record-id", "day": day.isoformat(), "type": "long_sleep",
        "bedtime_start": f"{day - timedelta(days=1)}T23:14:15-04:00",
        "bedtime_end": f"{day}T07:36:41-04:00",
        "total_sleep_duration": 27123, "rem_sleep_duration": 6187,
        "deep_sleep_duration": 4811, "light_sleep_duration": 16125,
        "awake_time": 1777, "average_hrv": 61.75, "average_heart_rate": 53.6,
        "lowest_heart_rate": 41, "readiness": {"temperature_deviation": -0.42},
        "heart_rate": {"items": [47, 56, 49]}, "hrv": {"items": [66, 71]},
        "sleep_phase_5_min": "private-sleep-series", "movement_30_sec": "private-movement",
    }
    record.update(changes)
    return record


def page(records=(), next_token=None):
    return 200, {"data": list(records), "next_token": next_token}


class FakeTransport:
    def __init__(self, sleep=(), scores=(), *, responses=None):
        self.responses = responses or {"sleep": [page(sleep)], "daily_sleep": [page(scores)]}
        self.calls = []

    def get_json(self, url, *, headers, timeout):
        self.calls.append((url, headers, timeout))
        result = self.responses[urlsplit(url).path.rsplit("/", 1)[-1]].pop(0)
        if isinstance(result, Exception):
            raise result
        return result


class FetchTest(unittest.TestCase):
    def test_exhausts_three_pages_and_projects_unused_fields(self):
        transport = FakeTransport(responses={"sleep": [
            page([sleep_record()], "page+two/secret"),
            page([sleep_record(DAY - timedelta(days=1))], "third"),
            page([sleep_record(DAY - timedelta(days=2))]),
        ]})
        records = fetch_collection(transport, "fake-access", "sleep", DAY - timedelta(days=2), DAY)
        self.assertEqual(len(records), 3)
        self.assertEqual(len(transport.calls), 3)
        query = parse_qs(urlsplit(transport.calls[1][0]).query)
        self.assertEqual(query["next_token"], ["page+two/secret"])
        self.assertEqual(query["end_date"], [DAY.isoformat()])
        for _, headers, timeout in transport.calls:
            self.assertEqual(headers, {"Authorization": "Bearer fake-access"})
            self.assertEqual(timeout, 20)
        for key in ("id", "heart_rate", "hrv", "sleep_phase_5_min", "movement_30_sec"):
            self.assertNotIn(key, records[0])

    def test_http_errors_after_first_page_never_return_partial_results(self):
        for status, error_class in ((401, OuraAuthError), (429, OuraSyncError), (500, OuraSyncError)):
            with self.subTest(status=status):
                transport = FakeTransport(responses={"sleep": [
                    page([sleep_record()], "next"), (status, {"error": "fake-access"}),
                ]})
                with self.assertRaises(error_class) as caught:
                    fetch_collection(transport, "fake-access", "sleep", DAY, DAY)
                self.assertNotIn("fake-access", str(caught.exception))
                if status == 429:
                    self.assertEqual(str(caught.exception), "Oura rate limit")

    def test_malformed_pages_and_pagination_cycles_fail_closed(self):
        for body in ({}, {"data": {}}, {"data": [None], "next_token": None},
                     {"data": []}, {"data": [], "next_token": []},
                     {"data": [], "next_token": "same"}):
            with self.subTest(body=body):
                transport = FakeTransport(responses={"sleep": [page([], "same"), (200, body)]})
                with self.assertRaises(OuraSyncError):
                    fetch_collection(transport, "fake-access", "sleep", DAY, DAY)

    def test_transport_exception_is_sanitized(self):
        transport = FakeTransport(responses={"sleep": [RuntimeError("fake-access 27123")]})
        with self.assertRaises(OuraSyncError) as caught:
            fetch_collection(transport, "fake-access", "sleep", DAY, DAY)
        self.assertEqual(str(caught.exception), "Oura request failed")


class MappingTest(unittest.TestCase):
    def test_selects_longest_long_sleep_only_per_wake_date(self):
        short = sleep_record(total_sleep_duration=600)
        longest = sleep_record()
        records = [short, longest, sleep_record(type="late_nap", total_sleep_duration=40000)]
        self.assertEqual(select_main_sleep_records(records, TZ), {DAY: longest})

    def test_mapping_rounding_nulls_and_metadata(self):
        mapped = map_sleep_record(sleep_record(), home_tz=TZ, ingested_at_utc=NOW, sleep_score=87)
        self.assertEqual(mapped["source"], "oura")
        self.assertEqual(mapped["sleep_date"], DAY)
        for key, value in {"total_sleep_min": 452, "rem_min": 103, "deep_min": 80,
                           "light_min": 269, "awake_min": 30, "hrv_avg_ms": 61.75,
                           "rhr_avg_bpm": 54, "body_temp_dev_c": -0.42, "sleep_score": 87}.items():
            self.assertEqual(mapped[key], value)
        self.assertEqual(mapped["waketime_utc"], datetime(2026, 9, 10, 11, 36, 41, tzinfo=UTC))
        nullable = sleep_record(**dict.fromkeys((
            "total_sleep_duration", "rem_sleep_duration", "deep_sleep_duration", "light_sleep_duration",
            "awake_time", "average_hrv", "average_heart_rate", "readiness",
        )))
        nulls = map_sleep_record(nullable, home_tz=TZ, ingested_at_utc=NOW, sleep_score=None)
        for key in ("total_sleep_min", "rem_min", "deep_min", "light_min", "awake_min",
                    "hrv_avg_ms", "rhr_avg_bpm", "body_temp_dev_c", "sleep_score"):
            self.assertIsNone(nulls[key])
        rounded = map_sleep_record(sleep_record(awake_time=90), home_tz=TZ, ingested_at_utc=NOW, sleep_score=None)
        self.assertEqual(rounded["awake_min"], 2)

    def test_bad_numbers_are_rejected_without_payload(self):
        for value in (-1, float("nan"), float("inf"), True, "private-health-value", 1.5):
            with self.subTest(value=value), self.assertRaises(OuraSyncError) as caught:
                map_sleep_record(sleep_record(total_sleep_duration=value), home_tz=TZ,
                                 ingested_at_utc=NOW, sleep_score=None)
            self.assertNotIn("private-health-value", str(caught.exception))


class SyncTest(unittest.TestCase):
    def setUp(self):
        self.tempdir = TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name).resolve()
        self.database = self.root / "warehouse.duckdb"
        self.tokens = SimpleNamespace(access_token="fake-access", refresh_token="fake-refresh")
        self.ensure_patcher = patch.object(sync_module, "ensure_access_token", return_value=self.tokens)
        self.ensure = self.ensure_patcher.start()
        self.addCleanup(self.ensure_patcher.stop)

    def sync(self, records=None, scores=(), *, transport=None, provider_policy=POLICY, **kwargs):
        return sync_oura(
            database_path=self.database,
            transport=transport or FakeTransport([sleep_record()] if records is None else records, scores),
            store=TokenStore(self.root / "unused-token-file.json"),
            credentials=OuraCredentials("fake-client", "fake-secret"),
            home_tz=TZ, start_date=DAY - timedelta(days=14), end_date=DAY,
            provider_policy=provider_policy, now=NOW, **kwargs,
        )

    def query(self, sql):
        conn = connect_duckdb(self.database, read_only=True)
        try:
            return conn.execute(sql).fetchall()
        finally:
            conn.close()

    def test_requests_one_day_past_the_window_because_oura_end_date_is_exclusive(self):
        # Regression: with end_date=D Oura omits the night whose day is D, so
        # the sync must ask through D + 1 while reporting the logical window.
        transport = FakeTransport([sleep_record()], [])
        report = self.sync(transport=transport)
        self.assertEqual(report.status, "ok")
        self.assertEqual(report.end_date, DAY)
        for url, _, _ in transport.calls:
            query = parse_qs(urlsplit(url).query)
            self.assertEqual(query["start_date"], [(DAY - timedelta(days=14)).isoformat()])
            self.assertEqual(query["end_date"], [(DAY + timedelta(days=1)).isoformat()])
        self.assertEqual(len(transport.calls), 2)

    def test_idempotent_upserts_preserve_eight_sleep_and_exclude_it_from_features(self):
        conn = connect_duckdb(self.database, apply_schema=True)
        eight = map_sleep_record(sleep_record(), home_tz=TZ, ingested_at_utc=NOW, sleep_score=None)
        eight.update(source="8sleep", total_sleep_min=999, hrv_avg_ms=999)
        insert_sleep_night(conn, eight)
        conn.close()
        scores = [{"day": DAY.isoformat(), "score": 87}]
        first = self.sync(scores=scores)
        before = self.query("SELECT * FROM sleep_nights ORDER BY source")
        second = self.sync(scores=scores)
        self.assertEqual(first.status, "ok", first.error)
        self.assertEqual(second.status, "ok", second.error)
        self.assertEqual(before, self.query("SELECT * FROM sleep_nights ORDER BY source"))
        self.assertEqual(len(before), 2)
        self.assertEqual(self.query("SELECT total_sleep_min FROM sleep_nights WHERE source = '8sleep'"), [(999,)])
        self.assertEqual(self.query("SELECT total_sleep_min, hrv_avg_ms, sleep_source_count FROM daily_features"),
                         [(452, 61.75, 1)])
        self.assertEqual(self.query("SELECT sleep_score FROM sleep_nights WHERE source = 'oura'"), [(87,)])
        self.assertEqual(first.rows_upserted, 1)
        self.assertEqual(first.recompute_start, DAY - timedelta(days=45))

    def test_scores_join_oura_day_but_sleep_key_uses_home_timezone(self):
        record = sleep_record()
        record["day"] = "2026-09-09"
        report = self.sync([record], [{"day": "2026-09-09", "score": 77}])
        self.assertEqual(report.status, "ok", report.error)
        self.assertEqual(self.query("SELECT sleep_date, sleep_score FROM sleep_nights"), [(DAY, 77)])

    def test_duplicate_daily_scores_keep_the_latest_timestamp_and_are_counted(self):
        # Oura re-delivers a day's daily_sleep record after a ring re-sync.
        scores = [
            {"day": DAY.isoformat(), "score": 60, "timestamp": f"{DAY}T00:00:00-04:00"},
            {"day": DAY.isoformat(), "score": 81, "timestamp": f"{DAY}T09:15:00-04:00"},
            {"day": DAY.isoformat(), "score": 55, "timestamp": f"{DAY}T00:00:00-04:00"},
        ]
        report = self.sync([sleep_record()], scores)
        self.assertEqual(report.status, "ok", report.error)
        self.assertEqual(report.duplicate_daily_scores, 2)
        self.assertEqual(report.daily_scores_fetched, 3)
        self.assertEqual(self.query("SELECT sleep_score FROM sleep_nights"), [(81,)])

        # Without usable timestamps the later delivery wins.
        report = self.sync([sleep_record()], [{"day": DAY.isoformat(), "score": 40}, {"day": DAY.isoformat(), "score": 42}])
        self.assertEqual(report.status, "ok", report.error)
        self.assertEqual(report.duplicate_daily_scores, 1)
        self.assertEqual(self.query("SELECT sleep_score FROM sleep_nights"), [(42,)])

        # A dated record is never displaced by an undated, malformed, or naive one,
        # regardless of delivery order.
        mixed = [
            {"day": DAY.isoformat(), "score": 81, "timestamp": f"{DAY}T09:15:00-04:00"},
            {"day": DAY.isoformat(), "score": 40},
            {"day": DAY.isoformat(), "score": 41, "timestamp": "not-a-timestamp"},
            {"day": DAY.isoformat(), "score": 43, "timestamp": f"{DAY}T23:00:00"},
            {"day": DAY.isoformat(), "score": 55, "timestamp": f"{DAY}T00:00:00-04:00"},
        ]
        for ordering in (mixed, list(reversed(mixed))):
            report = self.sync([sleep_record()], ordering)
            self.assertEqual(report.status, "ok", report.error)
            self.assertEqual(report.duplicate_daily_scores, 4)
            self.assertEqual(self.query("SELECT sleep_score FROM sleep_nights"), [(81,)])

    def test_skipped_counts_are_aggregate_and_unknown_types_are_not_echoed(self):
        records = [sleep_record(), sleep_record(total_sleep_duration=500)]
        records.extend(sleep_record(type=kind) for kind in ("sleep", "late_nap", "rest", "deleted", "private-type"))
        report = self.sync(records)
        self.assertEqual(report.status, "ok", report.error)
        self.assertEqual(report.records_fetched, 7)
        self.assertEqual(report.main_sleep_nights, 1)
        self.assertEqual(report.skipped_by_type,
                         {"long_sleep": 1, "sleep": 1, "late_nap": 1, "rest": 1, "deleted": 1, "unknown": 1})
        self.assertNotIn("private-type", json.dumps(report.to_dict()))

    def test_fetch_or_score_failure_does_not_open_warehouse(self):
        for response in ((500, {"error": "fake-access"}), (429, {}), (200, {"data": None})):
            with self.subTest(response=response):
                transport = FakeTransport(responses={"sleep": [page([sleep_record()], "next"), response]})
                report = self.sync(transport=transport)
                self.assertEqual(report.status, "error")
                self.assertFalse(self.database.exists())
                self.assertFalse(lock_path_for_database(self.database).exists())
        transport = FakeTransport(responses={"sleep": [page([sleep_record()])], "daily_sleep": [(500, {})]})
        self.assertEqual(self.sync(transport=transport).status, "error")
        self.assertFalse(self.database.exists())

    def test_one_auth_refresh_restarts_fetch_without_duplicate_rows(self):
        transport = FakeTransport(responses={
            "sleep": [page([sleep_record()], "next"), (401, {}), page([sleep_record()])],
            "daily_sleep": [page([])],
        })
        report = self.sync(transport=transport)
        self.assertEqual(report.status, "ok", report.error)
        self.assertEqual(report.records_fetched, 1)
        self.assertEqual(self.ensure.call_count, 2)
        self.assertTrue(self.ensure.call_args.kwargs["force_refresh"])
        self.assertNotIn("next_token", parse_qs(urlsplit(transport.calls[2][0]).query))

    def test_auth_failure_after_retry_or_in_second_collection_never_writes(self):
        for responses in (
            {"sleep": [(401, {}), (401, {})]},
            {"sleep": [(401, {}), page([sleep_record()])], "daily_sleep": [(401, {})]},
        ):
            with self.subTest(responses=responses):
                self.ensure.reset_mock()
                report = self.sync(transport=FakeTransport(responses=responses))
                self.assertEqual(report.status, "auth_required")
                self.assertEqual(self.ensure.call_count, 2)
                self.assertFalse(self.database.exists())

    def test_daily_collection_can_use_the_single_refresh(self):
        transport = FakeTransport(responses={
            "sleep": [page([sleep_record()])], "daily_sleep": [(401, {}), page([])],
        })
        self.assertEqual(self.sync(transport=transport).status, "ok")
        self.assertEqual(self.ensure.call_count, 2)

    def test_missing_authorization_is_aggregate_only(self):
        self.ensure.side_effect = OuraAuthError("fake-access fake-refresh fake-secret")
        report = self.sync()
        self.assertEqual(report.status, "auth_required")
        self.assertIn("scripts/oura_authorize.py", report.error)
        self.assertFalse(self.database.exists())
        self.assertNotIn("fake-", report.error)

    def test_insert_failure_midway_rolls_back_every_sleep_row(self):
        def fail_second(conn, row):
            if row.sleep_date == DAY:
                raise RuntimeError("private-record-id 27123 fake-access")
            return insert_sleep_night(conn, row)

        with patch.object(sync_module, "insert_sleep_night", side_effect=fail_second):
            report = self.sync([sleep_record(DAY - timedelta(days=1)), sleep_record()])
        self.assertEqual(report.status, "error")
        self.assertEqual(report.rows_upserted, 0)
        self.assertEqual(self.query("SELECT COUNT(*) FROM sleep_nights"), [(0,)])
        self.assertEqual(self.query("SELECT COUNT(*) FROM daily_features"), [(0,)])
        self.assertEqual(report.error, "RuntimeError: Oura sync failed")

    def test_invalid_mapping_never_calls_quarantine_or_creates_files(self):
        with patch("src.warehouse.warehouse._record_validation_failure") as quarantine:
            report = self.sync([sleep_record(), sleep_record(DAY - timedelta(days=1), total_sleep_duration=-999)])
        self.assertEqual(report.status, "error")
        quarantine.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])

    def test_recompute_failure_rolls_back_sleep_rows_and_features_together(self):
        self.assertEqual(self.sync().status, "ok")
        before_features = self.query("SELECT * FROM daily_features")
        before_sleep = self.query("SELECT total_sleep_min FROM sleep_nights")
        self.assertEqual(before_sleep, [(452,)])

        def fail_recompute(conn, **kwargs):
            conn.execute("DELETE FROM daily_features")
            raise ValueError("private feature value")

        with patch.object(sync_module, "recompute_daily_features", side_effect=fail_recompute):
            report = self.sync([sleep_record(total_sleep_duration=24000)])
        self.assertEqual(report.status, "error")
        self.assertEqual(report.rows_upserted, 0)
        self.assertEqual(report.feature_rows_written, 0)
        self.assertEqual(self.query("SELECT * FROM daily_features"), before_features)
        self.assertEqual(self.query("SELECT total_sleep_min FROM sleep_nights"), before_sleep)

    def test_recompute_extends_through_later_stored_dates(self):
        later = DAY + timedelta(days=3)
        stale = NOW - timedelta(days=10)
        conn = connect_duckdb(self.database, apply_schema=True)
        try:
            insert_sleep_night(
                conn,
                map_sleep_record(sleep_record(later), home_tz=TZ, ingested_at_utc=stale, sleep_score=None),
            )
            conn.execute("CHECKPOINT")
        finally:
            conn.close()

        report = self.sync()

        self.assertEqual(report.status, "ok")
        self.assertEqual(report.recompute_end, later)
        computed = self.query(
            f"SELECT computed_at_utc FROM daily_features WHERE feature_date = DATE '{later.isoformat()}'"
        )
        self.assertEqual(len(computed), 1)
        self.assertEqual(computed[0][0].replace(tzinfo=UTC), NOW)

    def test_lock_timeout_never_opens_connection(self):
        with warehouse_write_lock(lock_path_for_database(self.database)):
            report = self.sync(lock_timeout_seconds=0)
        self.assertEqual(report.status, "error")
        self.assertTrue(report.error.startswith("WarehouseLockTimeout:"))
        self.assertFalse(self.database.exists())

    def test_checkpoint_failure_after_commits_remains_ok(self):
        with patch.object(sync_module, "_checkpoint", side_effect=RuntimeError("fake-access 27123")):
            report = self.sync()
        self.assertEqual(report.status, "ok", report.error)
        self.assertEqual(report.checkpoint, "failed")
        self.assertEqual(report.rows_upserted, 1)
        self.assertEqual(report.feature_rows_written, 1)
        self.assertEqual(self.query("SELECT COUNT(*) FROM sleep_nights"), [(1,)])
        self.assertEqual(self.database.stat().st_mode & 0o777, 0o600)

    def test_corrected_earlier_night_recomputes_later_prior_only_hrv(self):
        values = [43, 41, 62, 53, 59, 70, 48, 80, 65]
        earliest = DAY - timedelta(days=len(values) - 1)
        records = [sleep_record(earliest + timedelta(days=i), average_hrv=value) for i, value in enumerate(values)]
        self.assertEqual(self.sync(records).status, "ok")
        before = self.query("SELECT feature_date, hrv_z FROM daily_features ORDER BY feature_date")
        expected, _ = compute_prior_only_hrv_z(current_value=values[-1], recent_history=values[:-1], prior_history=values[:-1])
        self.assertAlmostEqual(before[-1][1], expected)
        self.assertTrue(all(row[1] is None for row in before[:7]))
        with patch("src.warehouse.recompute.compute_daily_features", wraps=sync_module.recompute_daily_features.__globals__["compute_daily_features"]) as compute:
            report = self.sync([sleep_record(earliest, average_hrv=121)])
        self.assertEqual(report.status, "ok", report.error)
        dates = [call.args[1] for call in compute.call_args_list]
        self.assertEqual(dates, sorted(dates))
        self.assertEqual(dates[0], DAY - timedelta(days=45))
        self.assertEqual(dates[-1], DAY)
        after = self.query("SELECT hrv_z FROM daily_features ORDER BY feature_date")
        updated = [121, *values[1:-1]]
        corrected, _ = compute_prior_only_hrv_z(current_value=values[-1], recent_history=updated, prior_history=updated)
        self.assertAlmostEqual(after[-1][0], corrected)
        self.assertNotEqual(before[-1][1], after[-1][0])
        self.assertEqual(report.feature_rows_written, len(values))

    def test_empty_fetch_still_recomputes_lookback(self):
        report = self.sync([])
        self.assertEqual(report.status, "ok", report.error)
        self.assertEqual(report.rows_upserted, 0)
        self.assertIsNone(report.earliest_changed_sleep_date)
        self.assertEqual(report.recompute_start, DAY - timedelta(days=45))

    def test_report_and_disk_retain_no_raw_payload_or_sleep_values(self):
        report = self.sync()
        serialized = json.dumps(report.to_dict())
        forbidden = {"id", "data", "bedtime_start", "bedtime_end", "bedtime_utc", "waketime_utc",
                     "total_sleep_duration", "total_sleep_min", "average_hrv", "hrv_avg_ms",
                     "heart_rate", "hrv", "readiness", "sleep_score", "next_token", "access_token"}
        self.assertFalse(forbidden.intersection(report.to_dict()))
        for value in ("27123", "452", "61.75", "07:36:41", "23:14:15", "private-record-id", "private-sleep-series", "fake-access"):
            self.assertNotIn(value, serialized)
        self.assertEqual({p.name for p in self.root.rglob("*") if p.is_file()},
                         {"warehouse.duckdb", ".healthhub.lock"})

    def test_database_symlinks_fail_before_open(self):
        target = self.root / "elsewhere.duckdb"
        self.database.symlink_to(target)
        report = self.sync()
        self.assertEqual(report.status, "error")
        self.assertFalse(target.exists())

    def test_provider_policy_is_validated_before_authentication_or_writes(self):
        report = self.sync(provider_policy=SleepProviderPolicy("8sleep", "fallback_active", True))
        self.assertEqual(report.status, "error")
        self.ensure.assert_not_called()
        self.assertEqual(list(self.root.iterdir()), [])


if __name__ == "__main__":
    unittest.main()

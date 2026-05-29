from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts.evidence.oura_smoke import collect


class OuraSmokeCollectorTests(unittest.TestCase):
    def test_collect_writes_aggregate_only_report(self) -> None:
        class Response:
            status = 200

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, _: int) -> bytes:
                return b'{"data":[{"id":"sleep-1","day":"2026-05-08","bedtime_end":"2026-05-08T07:14:00+00:00"}]}'

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {
                "OURA_ACCESS_TOKEN": "test-token",
                "OURA_SLEEP_START_DATE": "2026-05-01",
                "OURA_SLEEP_END_DATE": "2026-05-08",
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch(
                "urllib.request.urlopen",
                return_value=Response(),
            ):
                report = collect(root)

            evidence_path = root / str(report["evidence"])
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            serialized = json.dumps(payload, sort_keys=True)
            evidence_mode = stat.S_IMODE(evidence_path.stat().st_mode)

        self.assertEqual(report["status"], "ok", report)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["provider_path"], "direct_oura_api_v2_periodic_pull")
        self.assertEqual(payload["query_window_days"], 7)
        self.assertEqual(payload["record_count"], 1)
        self.assertTrue(payload["sleep_data_present"])
        self.assertEqual(payload["latest_sleep_bucket"], "same_day")
        self.assertEqual(evidence_mode, 0o600)
        self.assertNotIn("start_date", payload)
        self.assertNotIn("end_date", payload)
        self.assertNotIn("2026-05-01", serialized)
        self.assertNotIn("2026-05-08", serialized)
        self.assertNotIn("sleep-1", serialized)
        self.assertNotIn("test-token", serialized)


if __name__ == "__main__":
    unittest.main()

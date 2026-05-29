from __future__ import annotations

import json
import os
import stat
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from scripts.evidence.oura_smoke import collect
from scripts.evidence.oura_smoke import main


def load_jsonl(path: Path) -> list[dict[str, object]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


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

    def test_main_missing_token_records_open_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stdout = StringIO()
            with mock.patch.dict(os.environ, {}, clear=True), redirect_stdout(stdout):
                exit_code = main(["--root", str(root), "--json"])

            report = json.loads(stdout.getvalue())
            evidence_path = root / str(report["evidence"])
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            rows = load_jsonl(root / "ops/autonomy/failure_ledger.jsonl")

        self.assertEqual(exit_code, 1)
        self.assertEqual(report["status"], "blocked_external")
        self.assertEqual(payload["status"], "blocked_external")
        self.assertEqual(payload["env"], {"OURA_ACCESS_TOKEN": "[UNSET]"})
        self.assertEqual(payload["missing_env"], ["OURA_ACCESS_TOKEN"])
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["schema_version"], "autokeel.failure_ledger.v2")
        self.assertEqual(rows[0]["slice"], "S03")
        self.assertEqual(rows[0]["failure_class"], "blocked_external_missing_evidence")
        self.assertTrue(rows[0]["open"])
        self.assertEqual(rows[0]["evidence_path"], report["evidence"])

    def test_main_offline_mode_redacts_token_and_records_failure_once(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {"OURA_ACCESS_TOKEN": "test-token"}
            first_stdout = StringIO()
            second_stdout = StringIO()
            with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(first_stdout):
                first_exit = main(["--root", str(root), "--offline", "--json"])
            with mock.patch.dict(os.environ, env, clear=True), redirect_stdout(second_stdout):
                second_exit = main(["--root", str(root), "--offline", "--json"])

            report = json.loads(second_stdout.getvalue())
            evidence_path = root / str(report["evidence"])
            payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            ledger_path = root / "ops/autonomy/failure_ledger.jsonl"
            rows = load_jsonl(ledger_path)
            serialized_report = json.dumps(payload, sort_keys=True)
            ledger_text = ledger_path.read_text(encoding="utf-8")

        self.assertEqual(first_exit, 1)
        self.assertEqual(second_exit, 1)
        self.assertEqual(report["status"], "blocked_external")
        self.assertEqual(payload["env"], {"OURA_ACCESS_TOKEN": "[REDACTED]"})
        self.assertTrue(payload["offline"])
        self.assertNotIn("test-token", serialized_report)
        self.assertNotIn("test-token", ledger_text)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["failure_class"], "blocked_external_missing_evidence")


if __name__ == "__main__":
    unittest.main()

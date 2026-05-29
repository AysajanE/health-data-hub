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

from scripts.evidence.pyeight_smoke import EightSleepConfig, ProviderIssue, collect, main


class PyEightSmokeCollectorTests(unittest.TestCase):
    def test_main_records_fallback_decision_and_returns_success_when_env_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            stdout = StringIO()
            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("network should not run")),
                redirect_stdout(stdout),
            ):
                exit_code = main(["--root", str(root), "--json"])

            report = json.loads(stdout.getvalue())
            evidence_path = root / str(report["evidence"])
            evidence_payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            evidence_mode = stat.S_IMODE(evidence_path.stat().st_mode)
            decisions = sorted((root / "ops/autonomy/decisions").glob("S03-pyeight-fallback-*.json"))
            self.assertEqual(len(decisions), 1)
            decision_payload = json.loads(decisions[0].read_text(encoding="utf-8"))

        self.assertEqual(exit_code, 0)
        self.assertEqual(report["status"], "fallback_accepted")
        self.assertEqual(report["errors"], [])
        self.assertEqual(evidence_payload["status"], "fallback_accepted")
        self.assertEqual(evidence_payload["fallback"], "oura_only_v1")
        self.assertEqual(
            evidence_payload["decision"],
            str(decisions[0].relative_to(root)),
        )
        self.assertEqual(evidence_mode, 0o600)
        self.assertEqual(decision_payload["status"], "fallback_accepted")
        self.assertEqual(decision_payload["slice"], "S03")
        self.assertEqual(decision_payload["tripwire"], "pyeight_smoke_failure")
        self.assertEqual(decision_payload["action"], "oura_only_v1")

    def test_collect_records_fallback_when_provider_issue_occurs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def failing_runner(_: Path, __: EightSleepConfig) -> dict[str, object]:
                raise ProviderIssue("8 Sleep API unavailable", status="blocked_external")

            with mock.patch.dict(
                os.environ,
                {
                    "PYEIGHT_EMAIL": "user@example.com",
                    "PYEIGHT_PASSWORD": "secret",
                    "PYEIGHT_TIMEZONE": "America/Toronto",
                    "PYEIGHT_CLIENT_ID": "client",
                    "PYEIGHT_CLIENT_SECRET": "client-secret",
                },
                clear=True,
            ):
                report = collect(root, runner=failing_runner)

            evidence_path = root / str(report["evidence"])
            evidence_payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            decisions = sorted((root / "ops/autonomy/decisions").glob("S03-pyeight-fallback-*.json"))
            self.assertEqual(report["status"], "blocked_external")
            self.assertEqual(report["errors"], ["8 Sleep API unavailable"])
            self.assertEqual(evidence_payload["status"], "blocked_external")
            self.assertEqual(evidence_payload["fallback"], "oura_only_v1")
            self.assertEqual(len(decisions), 1)
            decision_payload = json.loads(decisions[0].read_text(encoding="utf-8"))
            self.assertEqual(decision_payload["status"], "fallback_accepted")
            self.assertEqual(decision_payload["evidence_status"], "blocked_external")
            self.assertEqual(decision_payload["action"], "oura_only_v1")

    def test_collect_records_fallback_when_summary_is_not_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def blocked_runner(_: Path, __: EightSleepConfig) -> dict[str, object]:
                return {"status": "blocked_external", "reason": "no recent sleep interval"}

            with mock.patch.dict(
                os.environ,
                {
                    "PYEIGHT_EMAIL": "user@example.com",
                    "PYEIGHT_PASSWORD": "secret",
                    "PYEIGHT_TIMEZONE": "America/Toronto",
                    "PYEIGHT_CLIENT_ID": "client",
                    "PYEIGHT_CLIENT_SECRET": "client-secret",
                },
                clear=True,
            ):
                report = collect(root, runner=blocked_runner)

            evidence_path = root / str(report["evidence"])
            evidence_payload = json.loads(evidence_path.read_text(encoding="utf-8"))
            decisions = sorted((root / "ops/autonomy/decisions").glob("S03-pyeight-fallback-*.json"))
            self.assertEqual(report["status"], "blocked_external")
            self.assertEqual(report["errors"], ["no recent sleep interval"])
            self.assertEqual(evidence_payload["status"], "blocked_external")
            self.assertEqual(len(decisions), 1)
            decision_payload = json.loads(decisions[0].read_text(encoding="utf-8"))
            self.assertEqual(decision_payload["evidence_status"], "blocked_external")
            self.assertIn("no recent sleep interval", decision_payload["reason"])

    def test_collect_writes_success_decision_when_summary_is_ok(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)

            def ok_runner(_: Path, __: EightSleepConfig) -> dict[str, object]:
                return {
                    "status": "ok",
                    "auth_flow": "oauth_password_grant_current_api",
                    "authenticated": True,
                    "credential_cache_used": False,
                    "user_present": True,
                    "device_count": 1,
                    "current_device_present": True,
                    "query_window_days": 7,
                    "trend_day_count": 3,
                    "sleep_day_count": 2,
                    "recent_complete_sleep_interval_present": True,
                    "sleep_score_present": True,
                    "sleep_stages_present": True,
                    "heart_rate_signal_present": True,
                    "resp_rate_signal_present": True,
                    "hrv_signal_present": True,
                    "freshness_bucket": "0-1d",
                }

            with mock.patch.dict(
                os.environ,
                {
                    "PYEIGHT_EMAIL": "user@example.com",
                    "PYEIGHT_PASSWORD": "secret",
                    "PYEIGHT_TIMEZONE": "America/Toronto",
                    "PYEIGHT_CLIENT_ID": "client",
                    "PYEIGHT_CLIENT_SECRET": "client-secret",
                },
                clear=True,
            ):
                report = collect(root, runner=ok_runner)

            decisions = sorted((root / "ops/autonomy/decisions").glob("S03-pyeight-evidence-*.json"))
            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["errors"], [])
            self.assertEqual(len(decisions), 1)
            decision_payload = json.loads(decisions[0].read_text(encoding="utf-8"))
            self.assertEqual(decision_payload["status"], "ok")
            self.assertEqual(decision_payload["provider"], "pyeight")
            self.assertEqual(decision_payload["decision"], "include_8_sleep_under_tripwire")
            self.assertFalse(decision_payload["fallback_active"])

    def test_existing_fallback_decision_short_circuits_collection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            decision_dir = root / "ops/autonomy/decisions"
            decision_dir.mkdir(parents=True)
            decision = decision_dir / "S03-pyeight-fallback-20260529T000000-0400.json"
            decision.write_text(
                json.dumps(
                    {
                        "created_at": "2026-05-29T00:00:00-04:00",
                        "status": "fallback_accepted",
                        "action": "oura_only_v1",
                    }
                ),
                encoding="utf-8",
            )

            with (
                mock.patch.dict(os.environ, {}, clear=True),
                mock.patch("urllib.request.urlopen", side_effect=AssertionError("network should not run")),
            ):
                report = collect(root)

            self.assertEqual(report["status"], "fallback_accepted")
            self.assertEqual(report["errors"], [])
            self.assertEqual(report["evidence"].startswith("private/evidence/S03/pyeight_smoke/"), True)


if __name__ == "__main__":
    unittest.main()

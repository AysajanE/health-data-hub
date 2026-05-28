from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ops.autonomy.autokeel import AutoKeel
from scripts.acceptance_policy import command_allowed
from scripts.check_schema_contract import check_schema
from scripts.evidence.mood_shortcut_smoke import collect as collect_mood_shortcut
from scripts.evidence.oura_smoke import collect as collect_oura
from scripts.evidence.pyeight_smoke import fallback_decision_exists
from scripts.validate_playbook_autonomous import validate_playbook


class AutoKeelV2RemainingTests(unittest.TestCase):
    def test_manual_gate_header_with_none_values_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            policy = root / "policy.yaml"
            policy.write_text(
                "playbook_validation:\n"
                "  banned_language:\n"
                "    - manual approval\n"
                "    - human approval\n"
                "    - manual_gate\n"
                "  required_columns:\n"
                "    - action\n"
                "    - deliverable\n"
                "    - allowed_write_roots\n"
                "    - requires_red_green\n"
                "    - required_verification_commands\n"
                "    - exit_criteria\n",
                encoding="utf-8",
            )
            playbook = root / "playbook.md"
            playbook.write_text(
                """| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|
| 01 | Build schema | src/db/schema.sql | src/db | true | python -m pytest tests/warehouse -q | tests pass | none | none |
""",
                encoding="utf-8",
            )
            report = validate_playbook(playbook, policy_path=policy)
            self.assertEqual(report["status"], "ok", report)

    def test_schema_checker_accepts_create_table_if_not_exists(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            schema = Path(temp) / "schema.sql"
            schema.write_text(
                """
CREATE TABLE IF NOT EXISTS sleep_nights (
  source VARCHAR NOT NULL,
  sleep_date DATE NOT NULL,
  bedtime_utc TIMESTAMP,
  waketime_utc TIMESTAMP,
  total_sleep_min INTEGER,
  rem_min INTEGER,
  deep_min INTEGER,
  light_min INTEGER,
  awake_min INTEGER,
  hrv_avg_ms DOUBLE,
  rhr_avg_bpm INTEGER,
  body_temp_dev_c DOUBLE,
  sleep_score INTEGER,
  ingested_at_utc TIMESTAMP NOT NULL,
  PRIMARY KEY (source, sleep_date)
);
CREATE TABLE IF NOT EXISTS mood_entries (
  log_id UUID PRIMARY KEY,
  logged_at_utc TIMESTAMP NOT NULL,
  mood_date DATE NOT NULL,
  feeling INTEGER NOT NULL,
  energy INTEGER,
  notes TEXT,
  context_chips VARCHAR[],
  source VARCHAR,
  supersedes_log_id UUID
);
CREATE TABLE IF NOT EXISTS mood_current (
  mood_date DATE PRIMARY KEY,
  log_id UUID NOT NULL
);
CREATE TABLE IF NOT EXISTS daily_features (
  feature_date DATE PRIMARY KEY,
  total_sleep_min INTEGER,
  hrv_z DOUBLE,
  deep_sleep_pct DOUBLE,
  prior_day_feeling INTEGER,
  hrv_avg_ms DOUBLE,
  hrv_z_method VARCHAR,
  feature_version VARCHAR,
  prior_day_feeling_imputed BOOLEAN DEFAULT FALSE,
  sleep_source_count INTEGER,
  sleep_merge_warning VARCHAR,
  computed_at_utc TIMESTAMP NOT NULL
);
CREATE TABLE IF NOT EXISTS sleep_merge_diagnostics (
  sleep_date DATE PRIMARY KEY,
  oura_present BOOLEAN,
  eight_present BOOLEAN,
  total_sleep_delta_min INTEGER,
  hrv_merge_method VARCHAR,
  stage_source VARCHAR,
  warning VARCHAR,
  computed_at_utc TIMESTAMP NOT NULL
);
""",
                encoding="utf-8",
            )
            report = check_schema(schema)
            self.assertEqual(report["status"], "ok", report)

    def test_pyeight_fallback_ignores_malformed_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            decisions = root / "ops/autonomy/decisions"
            decisions.mkdir(parents=True)
            (decisions / "pyeight_bad.json").write_text(json.dumps({"status": "ok"}), encoding="utf-8")
            self.assertIsNone(fallback_decision_exists(root))

    def test_pyeight_fallback_takes_precedence_over_installed_module(self) -> None:
        from scripts.evidence.pyeight_smoke import collect as collect_pyeight

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            decisions = root / "ops/autonomy/decisions"
            decisions.mkdir(parents=True)
            (decisions / "pyeight_good.json").write_text(
                json.dumps({"status": "fallback_accepted", "action": "oura_only_v1"}),
                encoding="utf-8",
            )
            with mock.patch("importlib.util.find_spec", side_effect=AssertionError("module lookup should not run")):
                report = collect_pyeight(root)
            self.assertEqual(report["status"], "fallback_accepted", report)

    def test_acceptance_allowlist_reads_policy(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            policy = root / "ops/autonomy/policy.yaml"
            policy.parent.mkdir(parents=True)
            policy.write_text(
                "acceptance_commands:\n"
                "  allow_prefixes:\n"
                "    - git status\n",
                encoding="utf-8",
            )
            self.assertTrue(command_allowed("git status --short", root))
            self.assertFalse(command_allowed("python scripts/check.py", root))

    def test_acceptance_default_rejects_arbitrary_python_scripts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self.assertTrue(command_allowed("python scripts/check_no_tracked_data.py", root))
            self.assertTrue(command_allowed("python scripts/check_autonomous_review_exists.py S01", root))
            self.assertTrue(command_allowed("python scripts/evidence/oura_smoke.py --json", root))
            self.assertFalse(command_allowed("python scripts/arbitrary.py", root))

    def test_acceptance_policy_rejects_broad_python_script_prefix(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            policy = root / "ops/autonomy/policy.yaml"
            policy.parent.mkdir(parents=True)
            policy.write_text(
                "acceptance_commands:\n"
                "  allow_prefixes:\n"
                "    - python scripts/\n",
                encoding="utf-8",
            )
            self.assertFalse(command_allowed("python scripts/arbitrary.py", root))
            self.assertTrue(command_allowed("python -m pytest tests/autonomy -q", root))

    def test_oura_tripwire_is_not_auto_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            autonomy = root / "ops/autonomy"
            autonomy.mkdir(parents=True)
            (autonomy / "policy.yaml").write_text(
                "mode: autonomous_zero_human\n"
                "manual_gates:\n"
                "  forbidden_commands:\n"
                "    - mark-manual-gate\n",
                encoding="utf-8",
            )
            (autonomy / "slices.json").write_text("[]\n", encoding="utf-8")
            (autonomy / "autonomy_state.json").write_text("{}\n", encoding="utf-8")
            (autonomy / "events.jsonl").write_text("", encoding="utf-8")
            (autonomy / "failure_ledger.jsonl").write_text("", encoding="utf-8")

            op = AutoKeel(root)
            applied = op.apply_tripwire_fallbacks(
                {
                    "fired": [
                        {
                            "name": "on_oura_failure_week_1",
                            "action": "direct_oura_oauth",
                            "evidence": "private/evidence/S03/oura_smoke",
                            "evidence_status": {"status": "blocked_external"},
                        }
                    ]
                }
            )
            self.assertFalse(applied)
            self.assertEqual(list((root / "private/evidence/S03/oura_smoke").glob("*.json")), [])
            decision = next((root / "ops/autonomy/decisions").glob("*oura*json"))
            self.assertEqual(json.loads(decision.read_text(encoding="utf-8"))["status"], "fallback_required")
            ledger = (root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn("tripwire_triggered", ledger)

    def test_mood_shortcut_payload_has_valid_empty_context_chips(self) -> None:
        captured: dict[str, object] = {}

        class Response:
            status = 200

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, _: int) -> bytes:
                return b'{"ok":true}'

        def fake_urlopen(request: object, timeout: int = 20) -> Response:
            captured["payload"] = json.loads(getattr(request, "data").decode("utf-8"))
            captured["timeout"] = timeout
            return Response()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {
                "MOOD_SHORTCUT_TEST_URL": "http://127.0.0.1:9999/mood",
                "MOOD_SHORTCUT_TOKEN": "test-token",
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch("urllib.request.urlopen", fake_urlopen):
                os.environ.pop("MOOD_SHORTCUT_VERIFY_SQLITE", None)
                report = collect_mood_shortcut(root)
        self.assertEqual(report["status"], "ok", report)
        self.assertEqual(captured["payload"], {"feeling": 3, "energy": 3, "notes": "autokeel smoke", "context_chips": []})

    def test_oura_smoke_fetches_sleep_records_not_personal_info(self) -> None:
        captured: dict[str, object] = {}

        class Response:
            status = 200

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self, _: int) -> bytes:
                return b'{"data":[{"id":"sleep-1"}]}'

        def fake_urlopen(request: object, timeout: int = 20) -> Response:
            captured["url"] = getattr(request, "full_url")
            captured["timeout"] = timeout
            return Response()

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            env = {
                "OURA_ACCESS_TOKEN": "test-token",
                "OURA_SLEEP_START_DATE": "2026-05-01",
                "OURA_SLEEP_END_DATE": "2026-05-08",
            }
            with mock.patch.dict(os.environ, env, clear=False), mock.patch("urllib.request.urlopen", fake_urlopen):
                report = collect_oura(root)
        self.assertEqual(report["status"], "ok", report)
        self.assertIn("/v2/usercollection/sleep?", str(captured["url"]))
        self.assertNotIn("personal_info", str(captured["url"]))

    def test_forbidden_command_safety_prose_is_allowed_but_executable_command_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            policy = root / "policy.yaml"
            policy.write_text(
                "playbook_validation:\n"
                "  forbidden_commands:\n"
                "    - mark-manual-gate\n"
                "  required_columns:\n"
                "    - action\n"
                "    - deliverable\n"
                "    - allowed_write_roots\n"
                "    - requires_red_green\n"
                "    - required_verification_commands\n"
                "    - exit_criteria\n",
                encoding="utf-8",
            )

            allowed = root / "allowed.md"
            allowed.write_text(
                """# Safety Policy

Never call mark-manual-gate.

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|
| 01 | Build schema | src/db/schema.sql | src/db | true | python -m pytest tests/warehouse -q | tests pass | none | none |
""",
                encoding="utf-8",
            )
            report = validate_playbook(allowed, policy_path=policy)
            self.assertEqual(report["status"], "ok", report)

            blocked = root / "blocked.md"
            blocked.write_text(
                """| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|
| 01 | Bad command | scripts/bad.py | scripts | true | keel-run mark-manual-gate --run-id run_1 | command runs | none | none |
""",
                encoding="utf-8",
            )
            report = validate_playbook(blocked, policy_path=policy)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("forbidden executable command" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()

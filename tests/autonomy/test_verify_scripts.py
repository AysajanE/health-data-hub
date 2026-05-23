from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ops.autonomy.autokeel import write_json_atomic
from scripts.check_no_tracked_data import check_no_tracked_data
from scripts.keel_status_digest import digest_status
from scripts.verify_v1 import verify_v1


class VerifyScriptsTests(unittest.TestCase):
    def test_status_digest_extracts_terminal_state(self) -> None:
        payload = {"run_id": "run_1", "items": [{"state": "passed"}, {"state": "blocked_external"}]}
        digest = digest_status(payload)
        self.assertEqual(digest["terminal_state"], "blocked_external")

    def test_check_no_tracked_data_rejects_data_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / "data/raw").mkdir(parents=True)
            (root / "data/raw/oura.json").write_text("{}", encoding="utf-8")
            subprocess.run(["git", "add", "data/raw/oura.json"], cwd=root, check=True)
            report = check_no_tracked_data(root)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("tracked sensitive path" in error for error in report["errors"]))

    def test_verify_v1_fails_with_incomplete_slices(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ops/autonomy").mkdir(parents=True)
            write_json_atomic(root / "ops/autonomy/slices.json", [{"id": "S01", "required": True, "status": "pending"}])
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", {"active_run": None})
            (root / "ops/autonomy/failure_ledger.jsonl").write_text("", encoding="utf-8")
            report = verify_v1(root)
            self.assertEqual(report["status"], "error")
            self.assertIn("S01", report["incomplete_slices"])

    def test_verify_v1_fails_with_open_manual_gate_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ops/autonomy").mkdir(parents=True)
            write_json_atomic(root / "ops/autonomy/slices.json", [{"id": "S01", "required": True, "status": "complete"}])
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", {"active_run": None})
            (root / "ops/autonomy/failure_ledger.jsonl").write_text(json.dumps({"slice": "S01", "failure_class": "manual_gate_leak", "severity": "high", "open": True}) + "\n", encoding="utf-8")
            report = verify_v1(root)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("open critical" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()

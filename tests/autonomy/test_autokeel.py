from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ops.autonomy.autokeel import AutoKeel, CommandRunner, PolicyError, write_json_atomic


ROOT = Path(__file__).resolve().parents[2]


def copy_autonomy_fixture(dst: Path) -> None:
    shutil.copytree(ROOT / "ops", dst / "ops")
    shutil.copytree(ROOT / "scripts", dst / "scripts")
    (dst / ".gitignore").write_text("data/\nprivate/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n", encoding="utf-8")


class AutoKeelTests(unittest.TestCase):
    def test_command_runner_blocks_manual_gate_command(self) -> None:
        policy = {"manual_gates": {"forbidden_commands": ["keel-run mark-manual-gate", "mark-manual-gate"]}}
        runner = CommandRunner(ROOT, policy, dry_run=True)
        with self.assertRaises(PolicyError):
            runner.run(["keel-run", "mark-manual-gate", "--run-id", "run_1"])

    def test_log_event_redacts_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            token_key = "MOOD_" + "TOKEN"
            token_value = "supersecret" + "value123456"
            access_key = "access_" + "token"
            access_value = "abc123" + "45678901234567890"
            op.log_event("secret_test", {"message": f"{token_key}={token_value}", access_key: access_value}, slice_id="S01")
            content = (root / "ops/autonomy/events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(token_value, content)
            self.assertNotIn(access_value, content)
            self.assertIn("[REDACTED]", content)

    def test_choose_next_slice_skips_complete_required_slices(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            slices_path = root / "ops/autonomy/slices.json"
            slices = json.loads(slices_path.read_text(encoding="utf-8"))
            slices[0]["status"] = "complete"
            write_json_atomic(slices_path, slices)
            op = AutoKeel(root=root, dry_run=True)
            self.assertEqual(op.choose_next_slice()["id"], "S02")

    def test_awaiting_human_gate_records_manual_gate_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            result = op.handle_po_status("S01", "run_test", {"terminal_state": "awaiting_human_gate"})
            self.assertEqual(result, "manual_gate_leak")
            ledger = (root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn("manual_gate_leak", ledger)
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            self.assertEqual(slices[0]["status"], "replan_required")

    def test_blocked_external_creates_local_evidence_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            result = op.handle_po_status("S03", "run_ext", {"terminal_state": "blocked_external"})
            self.assertEqual(result, "blocked_external")
            evidence_roots = list((root / "private/evidence/S03").glob("*"))
            self.assertEqual(len(evidence_roots), 1)
            self.assertTrue((evidence_roots[0] / "README.md").exists())

    def test_verify_v1_does_not_pass_without_real_deliverables(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                item["status"] = "complete"
                item["acceptance"] = []
                item["review_artifacts"] = []
            write_json_atomic(root / "ops/autonomy/slices.json", slices)
            op = AutoKeel(root=root, dry_run=False)
            result = op.run_verify_v1()
            self.assertFalse(result.ok)
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            self.assertFalse(state.get("v1_complete", False))


if __name__ == "__main__":
    unittest.main()

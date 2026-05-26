from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ops.autonomy.autokeel import AutoKeel, CommandResult, CommandRunner, write_json_atomic
from scripts.check_no_tracked_data import check_no_tracked_data
from scripts.evaluate_tripwires import evaluate_tripwires
from scripts.evidence.pyeight_smoke import collect as collect_pyeight
from scripts.validate_playbook_autonomous import validate_playbook
from scripts.verify_slice import verify_slice


ROOT = Path(__file__).resolve().parents[2]


def copy_fixture(dst: Path) -> None:
    shutil.copytree(ROOT / "ops", dst / "ops")
    shutil.copytree(ROOT / "scripts", dst / "scripts")
    (dst / ".gitignore").write_text("data/\nprivate/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n", encoding="utf-8")
    for rel in ("ops/autonomy/failures/archived_playbooks", "ops/autonomy/failures/archived_autoplans", "ops/autonomy/heartbeats"):
        shutil.rmtree(dst / rel, ignore_errors=True)
    slices_path = dst / "ops/autonomy/slices.json"
    slices = json.loads(slices_path.read_text(encoding="utf-8"))
    for item in slices:
        item["status"] = "pending"
        item.pop("retry_count", None)
        item.pop("failure_path", None)
        item.pop("run_id", None)
    write_json_atomic(slices_path, slices)
    write_json_atomic(
        dst / "ops/autonomy/autonomy_state.json",
        {"active_run": None, "completed_slices": [], "current_slice": None, "last_event_id": 0, "v1_complete": False},
    )
    (dst / "ops/autonomy/failure_ledger.jsonl").write_text("", encoding="utf-8")


class AutoKeelV1FeedbackTests(unittest.TestCase):
    def test_command_runner_timeout_returns_124(self) -> None:
        runner = CommandRunner(ROOT, {"manual_gates": {"forbidden_commands": []}}, timeout=0.01)
        result = runner.run(["python", "-c", "import time; time.sleep(1)"])
        self.assertEqual(result.exit_code, 124)
        self.assertIn("command timed out", result.stderr)

    def test_replan_archives_playbook_and_clears_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            playbook = root / "docs/playbooks/s01-warehouse.playbook.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text("old playbook", encoding="utf-8")
            autoplan = root / "docs/gstack/s01-warehouse-autoplan.md"
            autoplan.parent.mkdir(parents=True)
            autoplan.write_text(
                "# S01 autoplan\n\n"
                "Deliverables and verification are listed below.\n\n"
                "Manual gates are forbidden.\n\n"
                "## Implementation Tasks\n\n"
                "- [ ] Add schema.\n"
                "  Files: `src/db/schema.sql`; `tests/warehouse/test_schema.py`\n"
                "  Verify: `python -m pytest tests/warehouse/test_schema.py -q`\n",
                encoding="utf-8",
            )
            (root / "docs/gstack/health-data-hub-office-hours.md").write_text("design", encoding="utf-8")
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            state["active_run"] = {"slice": "S01", "run_id": "run_old", "started_at": "2026-05-23T00:00:00-04:00"}
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", state)

            class CompileRunner:
                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    return CommandResult(list(argv), 0, "compiled", "")

            op = AutoKeel(root=root, dry_run=False)
            op.runner = CompileRunner()
            op.mark_slice_status("S01", "replan_required")
            slices = op.load_slices()
            result = op.ensure_playbook(next(item for item in slices if item["id"] == "S01"))

            self.assertTrue(result.ok)
            self.assertIsNone(op.load_state().get("active_run"))
            archived = list((root / "ops/autonomy/failures/archived_playbooks").glob("S01-*.md"))
            self.assertEqual(len(archived), 1)

    def test_evidence_ready_resumes_po_with_evidence_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            evidence = root / "private/evidence/S03/request"
            evidence.mkdir(parents=True)
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            state["active_run"] = {"slice": "S03", "run_id": "run_ext", "evidence_request": "private/evidence/S03/request"}
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", state)
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S03":
                    item["status"] = "evidence_ready"
                    item["evidence_request"] = "private/evidence/S03/request"
            write_json_atomic(root / "ops/autonomy/slices.json", slices)

            op = AutoKeel(root=root, dry_run=True)
            result = op.start_or_resume_po(next(item for item in slices if item["id"] == "S03"))
            self.assertTrue(result.ok)
            self.assertIn("dry_run", result.stdout)

    def test_high_risk_swr_missing_lane_decision_blocks_not_downgrades(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S02":
                    item.pop("lane_decision", None)
            write_json_atomic(root / "ops/autonomy/slices.json", slices)
            op = AutoKeel(root=root, dry_run=True)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_lane_decision(slice_)

            self.assertFalse(result.ok)
            self.assertIn("missing lane_decision artifact", result.stderr)
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertEqual(updated["status"], "blocked_compile_inputs")
            self.assertNotIn("lane_decision", updated)
            self.assertIn("missing lane_decision artifact", updated["reason"])
            ledger = (root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn("lane_decision_missing", ledger)
            self.assertNotIn("compile_failure", ledger)

    def test_high_risk_swr_invalid_lane_decision_blocks_with_dedicated_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            invalid = root / "ops/autonomy/decisions/S02-invalid.json"
            invalid.write_text(
                json.dumps(
                    {
                        "created_at": "2026-05-26T00:00:00-04:00",
                        "slice": "S02",
                        "lane": "swr_preferred",
                        "decision": "block",
                        "risk": "high",
                        "review_artifacts": ["docs/reviews/s02-autonomous-security-review.md"],
                        "commands": [{"command": "python scripts/verify_autonomy_preflight.py --json", "exit_code": 1}],
                        "verdict": "fail",
                    }
                ),
                encoding="utf-8",
            )
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S02":
                    item["lane_decision"] = "ops/autonomy/decisions/S02-invalid.json"
            write_json_atomic(root / "ops/autonomy/slices.json", slices)
            op = AutoKeel(root=root, dry_run=True)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_lane_decision(slice_)

            self.assertFalse(result.ok)
            self.assertIn("lane_decision verdict blocks execution", result.stderr)
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertEqual(updated["status"], "blocked_compile_inputs")
            ledger = (root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn("lane_decision_invalid", ledger)

    def test_high_risk_swr_valid_lane_decision_allows_compile_precheck(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_lane_decision(slice_)

            self.assertTrue(result.ok, result.stderr)
            self.assertIn("lane decision exists", result.stdout)

    def test_complete_status_clears_stale_failure_fields_and_records_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            slices[0]["failure_path"] = "ops/autonomy/failures/old.md"
            slices[0]["reason"] = "old failure"
            slices[0]["retry_count"] = 2
            write_json_atomic(root / "ops/autonomy/slices.json", slices)
            op = AutoKeel(root=root, dry_run=True)

            op.mark_slice_status(
                "S01",
                "complete",
                run_id="RUN_DONE",
                ship_branch="ship/s01",
                ship_commit="abc123",
            )

            updated = next(item for item in op.load_slices() if item["id"] == "S01")
            self.assertEqual(updated["retry_count"], 0)
            self.assertNotIn("failure_path", updated)
            self.assertNotIn("reason", updated)
            history = op.load_state()["run_history"]
            self.assertTrue(any(item["slice"] == "S01" and item["run_id"] == "RUN_DONE" for item in history))

    def test_tripwire_rejects_latest_blocked_external_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            policy = (root / "ops/autonomy/policy.yaml").read_text(encoding="utf-8")
            policy = policy.replace("date: 2026-05-30", "date: 2026-01-01")
            (root / "ops/autonomy/policy.yaml").write_text(policy, encoding="utf-8")
            report_dir = root / "private/evidence/S03/oura_smoke"
            report_dir.mkdir(parents=True)
            (report_dir / "report.json").write_text(json.dumps({"status": "blocked_external"}), encoding="utf-8")

            report = evaluate_tripwires(root)
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["fired"][0]["evidence_status"]["status"], "blocked_external")

    def test_s03_required_oura_preflight_blocks_before_po_without_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            op = AutoKeel(root=root, dry_run=False)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S03")

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OURA_ACCESS_TOKEN", None)
                result = op.required_external_evidence_ready(slice_)

            self.assertFalse(result.ok)
            updated = next(item for item in op.load_slices() if item["id"] == "S03")
            self.assertEqual(updated["status"], "blocked_external")
            self.assertEqual(updated["reason"], "required Oura evidence unavailable")
            ledger = (root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn("blocked_external_missing_evidence", ledger)

    def test_pyeight_can_use_fallback_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            decision = root / "ops/autonomy/decisions/pyeight_smoke_failure-test.json"
            decision.write_text(json.dumps({"status": "fallback_accepted", "action": "oura_only_v1"}), encoding="utf-8")
            report = collect_pyeight(root)
            self.assertEqual(report["status"], "fallback_accepted")

    def test_tracked_lock_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / ".gitignore").write_text("data/\nprivate/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n", encoding="utf-8")
            lock = root / "ops/autonomy/.autokeel.lock"
            lock.parent.mkdir(parents=True)
            lock.write_text("123\n", encoding="utf-8")
            subprocess.run(["git", "add", "-f", "ops/autonomy/.autokeel.lock"], cwd=root, check=True)
            report = check_no_tracked_data(root)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any(".autokeel.lock" in error for error in report["errors"]))

    def test_manual_approval_language_is_global_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "playbook.md"
            path.write_text(
                """# Playbook

This requires manual approval.

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|
| 01 | Update code | src/app.py | src/app.py | true | python -m pytest tests -q | tests pass | none | none |
""",
                encoding="utf-8",
            )
            policy = Path(temp) / "policy.yaml"
            policy.write_text("playbook_validation:\n  banned_language:\n    - manual approval\n", encoding="utf-8")
            report = validate_playbook(path, policy_path=policy)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("manual approval" in error for error in report["errors"]))

    def test_verify_slice_rejects_non_allowlisted_acceptance_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            slices = [
                {
                    "id": "S01",
                    "required": True,
                    "status": "pending",
                    "playbook": "docs/playbooks/s01.md",
                    "acceptance": ["bash scripts/unsafe.sh"],
                    "review_artifacts": [],
                    "deliverables": [],
                }
            ]
            write_json_atomic(root / "ops/autonomy/slices.json", slices)
            playbook = root / "docs/playbooks/s01.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text(
                """| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|
| 01 | Check | scripts/check.py | scripts/check.py | true | python scripts/check.py | check passes | none | none |
""",
                encoding="utf-8",
            )
            report = verify_slice(root, "S01", dry_run=True)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("not allowlisted" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()

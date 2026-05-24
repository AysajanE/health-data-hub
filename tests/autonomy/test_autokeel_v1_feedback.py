from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ops.autonomy.autokeel import AutoKeel, CommandRunner, write_json_atomic
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
            autoplan.write_text("S01 autoplan\n\nDeliverables: schema.\n\nVerification: tests.\n\nManual gates are forbidden.\n", encoding="utf-8")
            (root / "docs/gstack/health-data-hub-office-hours.md").write_text("design", encoding="utf-8")
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            state["active_run"] = {"slice": "S01", "run_id": "run_old", "started_at": "2026-05-23T00:00:00-04:00"}
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", state)

            op = AutoKeel(root=root, dry_run=True)
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

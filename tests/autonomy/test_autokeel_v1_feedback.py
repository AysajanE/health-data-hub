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

    def test_high_risk_swr_compile_decision_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            decision = root / "ops/autonomy/decisions/S02-compile.json"
            decision.write_text(
                json.dumps(
                    {
                        "created_at": "2026-05-26T00:00:00-04:00",
                        "status": "accepted",
                        "slice": "S02",
                        "lane": "swr_preferred",
                        "decision": "compile_with_keel_compile",
                        "risk": "high",
                        "review_artifacts": [
                            "docs/reviews/s02-autonomous-security-review.md",
                            "docs/reviews/s02-autonomous-privacy-review.md",
                        ],
                        "commands": [{"command": "python scripts/verify_autonomy_preflight.py --json", "exit_code": 0}],
                        "verdict": "pass",
                    }
                ),
                encoding="utf-8",
            )
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S02":
                    item["lane_decision"] = "ops/autonomy/decisions/S02-compile.json"
            write_json_atomic(root / "ops/autonomy/slices.json", slices)
            op = AutoKeel(root=root, dry_run=True)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_lane_decision(slice_)

            self.assertFalse(result.ok)
            self.assertIn("must be use_swr", result.stderr)
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertEqual(updated["status"], "blocked_compile_inputs")

    def test_high_risk_swr_valid_lane_decision_allows_swr_precheck(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_lane_decision(slice_)

            self.assertTrue(result.ok, result.stderr)
            self.assertIn("lane decision exists", result.stdout)

    def test_swr_preferred_playbook_generation_routes_through_keel_swr(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            fake_keel = root / "fake-keel"
            task_pack_source = fake_keel / "tools/staged-workflow-runner/automation/task_packs/gstack_design_to_po_playbook"
            task_pack_source.mkdir(parents=True)
            (fake_keel / "bin").mkdir(parents=True)

            policy = (root / "ops/autonomy/policy.yaml").read_text(encoding="utf-8")
            policy = policy.replace("keel_root: /Users/aeziz-local/keel", f"keel_root: {fake_keel}")
            (root / "ops/autonomy/policy.yaml").write_text(policy, encoding="utf-8")

            (root / "docs/gstack").mkdir(parents=True)
            (root / "docs/briefs").mkdir(parents=True)
            (root / "docs/playbooks").mkdir(parents=True)
            (root / "docs/gstack/health-data-hub-office-hours.md").write_text("S02 design", encoding="utf-8")
            (root / "docs/briefs/s02-mood-api.autonomous-brief.md").write_text("S02 brief", encoding="utf-8")
            (root / "docs/gstack/s02-mood-api-autoplan.md").write_text(
                "# S02 autoplan\n\n"
                "Deliverables and verification are listed below.\n\n"
                "Manual gates are forbidden; use autonomous_gate_review evidence instead.\n\n"
                "## Implementation Tasks\n\n"
                "- [ ] Implement the Mood API loop.\n"
                "  Files: `src/api/mood.py`; `tests/test_api_security.py`\n"
                "  Verify: `python -m pytest tests/test_api_security.py -q`\n",
                encoding="utf-8",
            )
            stale_playbook = root / "docs/playbooks/s02-mood-api.playbook.md"
            stale_playbook.write_text("compiler generated stale playbook\n", encoding="utf-8")

            op = AutoKeel(root=root, dry_run=True)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")
            result = op.ensure_playbook(slice_)

            self.assertTrue(result.ok, result.stderr)
            self.assertIn("keel-swr", result.argv[0])
            self.assertIn("run", result.argv)
            self.assertNotIn("keel-compile", " ".join(result.argv))
            events = (root / "ops/autonomy/events.jsonl").read_text(encoding="utf-8")
            self.assertIn("dry_run_non_swr_playbook_archive_skipped", events)
            self.assertIn("swr_playbook_generation_planned", events)

    def test_completed_swr_manifest_materializes_canonical_playbook(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            fake_keel = root / "fake-keel"
            task_pack_source = fake_keel / "tools/staged-workflow-runner/automation/task_packs/gstack_design_to_po_playbook"
            (task_pack_source / "workflows").mkdir(parents=True)
            (task_pack_source / "workflows/gstack_design_to_po_playbook.workflow.json").write_text("{}", encoding="utf-8")
            (fake_keel / "bin").mkdir(parents=True)

            policy = (root / "ops/autonomy/policy.yaml").read_text(encoding="utf-8")
            policy = policy.replace("keel_root: /Users/aeziz-local/keel", f"keel_root: {fake_keel}")
            (root / "ops/autonomy/policy.yaml").write_text(policy, encoding="utf-8")

            (root / "docs/gstack").mkdir(parents=True)
            (root / "docs/briefs").mkdir(parents=True)
            (root / "docs/gstack/health-data-hub-office-hours.md").write_text("S02 design", encoding="utf-8")
            (root / "docs/briefs/s02-mood-api.autonomous-brief.md").write_text("S02 brief", encoding="utf-8")
            (root / "docs/gstack/s02-mood-api-autoplan.md").write_text(
                "# S02 autoplan\n\n"
                "Deliverables and verification are listed below.\n\n"
                "Manual gates are forbidden; use autonomous_gate_review evidence instead.\n\n"
                "## Implementation Tasks\n\n"
                "- [ ] Implement the Mood API loop.\n"
                "  Files: `src/api/mood.py`; `tests/test_api_security.py`\n"
                "  Verify: `python -m pytest tests/test_api_security.py -q`\n",
                encoding="utf-8",
            )

            class SwrRunner:
                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    run_dir = root / ".local/autokeel/swr/runs/test-run"
                    stage_dir = run_dir / "stages/05_final_markdown_playbook"
                    stage_dir.mkdir(parents=True)
                    response_path = stage_dir / "response.final.json"
                    response_path.write_text(
                        json.dumps(
                            {
                                "output": [
                                    {
                                        "type": "message",
                                        "content": [
                                            {
                                                "type": "output_text",
                                                "text": "# S02 Mood API Playbook\n\nmarkdown_playbook_v1\n",
                                            }
                                        ],
                                    }
                                ]
                            }
                        ),
                        encoding="utf-8",
                    )
                    manifest = {
                        "status": "completed",
                        "stages": [
                            {
                                "stage_id": "final_markdown_playbook",
                                "status": "completed",
                                "response_json_path": str(response_path.relative_to(root)),
                            }
                        ],
                    }
                    manifest_path = run_dir / "run_manifest.json"
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    return CommandResult(list(argv), 0, str(manifest_path.relative_to(root)) + "\n", "")

            op = AutoKeel(root=root, dry_run=False)
            op.runner = SwrRunner()
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")
            result = op.ensure_playbook(slice_)

            self.assertTrue(result.ok, result.stderr)
            playbook = root / "docs/playbooks/s02-mood-api.playbook.md"
            self.assertTrue(playbook.exists())
            self.assertEqual(playbook.read_text(encoding="utf-8"), "# S02 Mood API Playbook\n\nmarkdown_playbook_v1\n")
            evidence = json.loads((root / "docs/evidence/s02-mood-api-swr-playbook-evidence.json").read_text(encoding="utf-8"))
            self.assertEqual(evidence["tool"], "keel-swr")
            self.assertEqual(evidence["swr_source"]["manifest"], ".local/autokeel/swr/runs/test-run/run_manifest.json")
            events = (root / "ops/autonomy/events.jsonl").read_text(encoding="utf-8")
            self.assertIn("swr_playbook_materialized", events)

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

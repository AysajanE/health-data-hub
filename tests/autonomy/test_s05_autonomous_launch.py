from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from ops.autonomy.autokeel import AutoKeel, CommandResult, write_json_atomic
from scripts.verify_autokeel_invariants import verify_autokeel_invariants
from scripts.verify_s05_readiness import verify_s05_readiness


ROOT = Path(__file__).resolve().parents[2]


def copy_autonomy_fixture(dst: Path) -> None:
    shutil.copytree(ROOT / "ops", dst / "ops")
    shutil.copytree(ROOT / "scripts", dst / "scripts")
    shutil.copytree(ROOT / "src", dst / "src")
    (dst / ".gitignore").write_text(
        "data/\nprivate/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n",
        encoding="utf-8",
    )
    (dst / "ops/autonomy/failure_ledger.jsonl").write_text("", encoding="utf-8")


def set_completed_through_s04(root: Path) -> list[dict]:
    slices_path = root / "ops/autonomy/slices.json"
    slices = json.loads(slices_path.read_text(encoding="utf-8"))
    for item in slices:
        if item["id"] in {"S01", "S02", "S03", "S04"}:
            item["status"] = "complete"
            item.setdefault("run_id", f"RUN_TEST_{item['id']}")
            item.setdefault("ship_branch", f"ship/{item['id'].lower()}")
            item.setdefault("ship_commit", "test-commit")
        elif item["id"] == "S05":
            item["status"] = "pending"
            item.pop("lane_decision", None)
            item.pop("swr_run_id", None)
            item.pop("swr_run_manifest", None)
            item.pop("swr_review_repair", None)
            item.pop("swr_validation_repair", None)
            item.pop("failure_path", None)
            item.pop("reason", None)
        else:
            item["status"] = "pending"
    write_json_atomic(slices_path, slices)
    write_json_atomic(
        root / "ops/autonomy/autonomy_state.json",
        {
            "active_run": None,
            "active_swr_run": None,
            "completed_slices": ["S01", "S02", "S03", "S04"],
            "current_slice": None,
            "last_event_id": 0,
            "v1_complete": False,
        },
    )
    return slices


def write_s05_lane_decision(root: Path, slices: list[dict]) -> None:
    decision_path = root / "ops/autonomy/decisions/S05-lane-decision-test.json"
    write_json_atomic(
        decision_path,
        {
            "created_at": "2026-05-31T18:00:00-04:00",
            "status": "accepted",
            "slice": "S05",
            "lane": "swr_preferred",
            "decision": "use_swr",
            "risk": "high",
            "review_artifacts": [
                "docs/reviews/s05-autonomous-model-gate-review.md",
                "docs/reviews/s05-autonomous-statistical-validity-review.md",
            ],
            "commands": [{"command": "test fixture", "exit_code": 0}],
            "verdict": "pass",
        },
    )
    for item in slices:
        if item["id"] == "S05":
            item["lane_decision"] = "ops/autonomy/decisions/S05-lane-decision-test.json"


class S05AutonomousLaunchTests(unittest.TestCase):
    def test_missing_s05_lane_decision_blocks_control_plane(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            set_completed_through_s04(root)

            report = verify_autokeel_invariants(root)

            self.assertEqual(report["status"], "error")
            self.assertTrue(any("S05" in error and "lane_decision" in error for error in report["errors"]))

    def test_valid_s05_lane_decision_clears_lane_invariant(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            slices = set_completed_through_s04(root)

            decision_path = root / "ops/autonomy/decisions/S05-lane-decision-test.json"
            write_json_atomic(
                decision_path,
                {
                    "created_at": "2026-05-31T18:00:00-04:00",
                    "status": "accepted",
                    "slice": "S05",
                    "lane": "swr_preferred",
                    "decision": "use_swr",
                    "risk": "high",
                    "review_artifacts": [
                        "docs/reviews/s05-autonomous-model-gate-review.md",
                        "docs/reviews/s05-autonomous-statistical-validity-review.md",
                    ],
                    "commands": [
                        {
                            "command": "test fixture",
                            "exit_code": 0,
                            "stdout_tail": "ok",
                            "stderr_tail": "",
                        }
                    ],
                    "verdict": "pass",
                },
            )
            for item in slices:
                if item["id"] == "S05":
                    item["lane_decision"] = "ops/autonomy/decisions/S05-lane-decision-test.json"
            write_json_atomic(root / "ops/autonomy/slices.json", slices)

            report = verify_autokeel_invariants(root)

            self.assertFalse(any("S05" in error and "lane_decision" in error for error in report["errors"]))

    def test_swr_review_repair_invariant_requires_existing_manifest_and_blocked_status(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            slices = set_completed_through_s04(root)
            write_s05_lane_decision(root, slices)
            for item in slices:
                if item["id"] == "S05":
                    item["status"] = "waiting_for_playbook"
                    item["swr_review_repair"] = {
                        "repair_action": "rerun_review_lane",
                        "repair_stage_id": "source_authority_map",
                        "run_dir": ".local/autokeel/swr/runs/missing",
                        "run_manifest": ".local/autokeel/swr/runs/missing/run_manifest.json",
                        "stage_artifact_errors": [],
                    }
            write_json_atomic(root / "ops/autonomy/slices.json", slices)

            report = verify_autokeel_invariants(root)

            self.assertEqual(report["status"], "error")
            self.assertTrue(any("swr_review_repair exists while slice is not blocked_compile_inputs" in error for error in report["errors"]))
            self.assertTrue(any("swr_review_repair missing existing run_manifest" in error for error in report["errors"]))

    def test_swr_review_repair_invariant_rejects_stage_rerun_without_source_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            slices = set_completed_through_s04(root)
            write_s05_lane_decision(root, slices)
            run_dir = root / ".local/autokeel/swr/runs/s05-repair-test"
            write_json_atomic(run_dir / "run_manifest.json", {"run_id": "run_s05_repair_test"})
            for item in slices:
                if item["id"] == "S05":
                    item["status"] = "blocked_compile_inputs"
                    item["swr_review_repair"] = {
                        "repair_action": "rerun_single_stage",
                        "repair_stage_id": "repo_grounding",
                        "run_dir": ".local/autokeel/swr/runs/s05-repair-test",
                        "run_manifest": ".local/autokeel/swr/runs/s05-repair-test/run_manifest.json",
                    }
            write_json_atomic(root / "ops/autonomy/slices.json", slices)

            report = verify_autokeel_invariants(root)

            self.assertEqual(report["status"], "error")
            self.assertTrue(any("single-stage review repair missing source_review_bundle" in error for error in report["errors"]))

    def test_s05_readiness_fails_closed_without_swr_auth(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            slices = set_completed_through_s04(root)
            decision_path = root / "ops/autonomy/decisions/S05-lane-decision-test.json"
            write_json_atomic(
                decision_path,
                {
                    "created_at": "2026-05-31T18:00:00-04:00",
                    "status": "accepted",
                    "slice": "S05",
                    "lane": "swr_preferred",
                    "decision": "use_swr",
                    "risk": "high",
                    "review_artifacts": [
                        "docs/reviews/s05-autonomous-model-gate-review.md",
                        "docs/reviews/s05-autonomous-statistical-validity-review.md",
                    ],
                    "commands": [{"command": "test fixture", "exit_code": 0}],
                    "verdict": "pass",
                },
            )
            for item in slices:
                if item["id"] == "S05":
                    item["lane_decision"] = "ops/autonomy/decisions/S05-lane-decision-test.json"
            write_json_atomic(root / "ops/autonomy/slices.json", slices)

            with patch.dict("os.environ", {}, clear=True):
                report = verify_s05_readiness(root)

            self.assertEqual(report["status"], "error")
            self.assertTrue(any("OPENAI_API_KEY" in error for error in report["errors"]))

    def test_active_s05_swr_run_skips_prelaunch_readiness_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            slices = set_completed_through_s04(root)

            decision_path = root / "ops/autonomy/decisions/S05-lane-decision-test.json"
            write_json_atomic(
                decision_path,
                {
                    "created_at": "2026-05-31T18:00:00-04:00",
                    "status": "accepted",
                    "slice": "S05",
                    "lane": "swr_preferred",
                    "decision": "use_swr",
                    "risk": "high",
                    "review_artifacts": [
                        "docs/reviews/s05-autonomous-model-gate-review.md",
                        "docs/reviews/s05-autonomous-statistical-validity-review.md",
                    ],
                    "commands": [{"command": "test fixture", "exit_code": 0}],
                    "verdict": "pass",
                },
            )
            manifest = root / ".local/autokeel/swr/runs/s05-test/run_manifest.json"
            write_json_atomic(
                manifest,
                {
                    "run_id": "run_s05_test",
                    "status": "running",
                    "current_stage_id": "source_authority_map",
                    "stages": [
                        {
                            "stage_id": "source_authority_map",
                            "status": "in_progress",
                            "gate": "review_required",
                        }
                    ],
                },
            )
            for item in slices:
                if item["id"] == "S05":
                    item["status"] = "waiting_for_playbook"
                    item["lane_decision"] = "ops/autonomy/decisions/S05-lane-decision-test.json"
                    item["swr_run_id"] = "run_s05_test"
                    item["swr_run_manifest"] = ".local/autokeel/swr/runs/s05-test/run_manifest.json"
            write_json_atomic(root / "ops/autonomy/slices.json", slices)
            write_json_atomic(
                root / "ops/autonomy/autonomy_state.json",
                {
                    "active_run": None,
                    "active_swr_run": {
                        "slice": "S05",
                        "status": "running",
                        "run_id": "run_s05_test",
                        "run_manifest": ".local/autokeel/swr/runs/s05-test/run_manifest.json",
                    },
                    "completed_slices": ["S01", "S02", "S03", "S04"],
                    "current_slice": "S05",
                    "last_event_id": 0,
                    "v1_complete": False,
                },
            )

            autokeel = AutoKeel(root)
            readiness = Mock(return_value=CommandResult([], 1, "", "active_swr_run must be null before S05 launch"))
            ensure_playbook = Mock(return_value=CommandResult(["test"], 31, "manifest", "SWR background run is still in progress"))

            with (
                patch.object(autokeel, "run_autokeel_invariants", return_value=CommandResult([], 0, "{}", "")),
                patch.object(autokeel, "run_verify_v1", return_value=CommandResult([], 1, "{}", "")),
                patch.object(autokeel, "evaluate_tripwires", return_value=CommandResult([], 0, "{}", "")),
                patch.object(autokeel, "ensure_slice_brief"),
                patch.object(autokeel, "failure_budget_exceeded", return_value=CommandResult([], 0, "", "")),
                patch.object(autokeel, "recover_passed_slice_run", return_value=None),
                patch.object(autokeel, "restore_repaired_escalated_slice_run", return_value=None),
                patch.object(autokeel, "run_slice_readiness", readiness),
                patch.object(autokeel, "ensure_lane_decision", return_value=CommandResult([], 0, "", "")),
                patch.object(autokeel, "required_external_evidence_ready", return_value=CommandResult([], 0, "", "")),
                patch.object(autokeel, "ensure_playbook", ensure_playbook),
            ):
                code = autokeel.run_once(requested_slice="S05")

            self.assertEqual(code, 0)
            readiness.assert_not_called()
            ensure_playbook.assert_called_once()

    def test_planned_s05_swr_review_repair_skips_prelaunch_readiness_gate(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            slices = set_completed_through_s04(root)
            write_s05_lane_decision(root, slices)
            manifest = root / ".local/autokeel/swr/runs/s05-review-repair-test/run_manifest.json"
            write_json_atomic(
                manifest,
                {
                    "run_id": "run_s05_review_repair_test",
                    "status": "quarantined",
                    "current_stage_id": "source_authority_map",
                    "quarantined_reason": "SWR independent review failed closed",
                    "stages": [
                        {
                            "stage_id": "source_authority_map",
                            "status": "passed",
                            "gate": "review_required",
                        }
                    ],
                },
            )
            for item in slices:
                if item["id"] == "S05":
                    item["status"] = "blocked_compile_inputs"
                    item["lane_decision"] = "ops/autonomy/decisions/S05-lane-decision-test.json"
                    item["swr_run_id"] = "run_s05_review_repair_test"
                    item["swr_run_manifest"] = ".local/autokeel/swr/runs/s05-review-repair-test/run_manifest.json"
                    item["swr_review_repair"] = {
                        "repair_action": "rerun_review_lane",
                        "repair_stage_id": "source_authority_map",
                        "run_dir": ".local/autokeel/swr/runs/s05-review-repair-test",
                        "run_manifest": ".local/autokeel/swr/runs/s05-review-repair-test/run_manifest.json",
                        "stage_artifact_errors": [],
                    }
            write_json_atomic(root / "ops/autonomy/slices.json", slices)

            autokeel = AutoKeel(root)
            readiness = Mock(return_value=CommandResult([], 1, "", "S05 is not in an actionable pre-launch status"))
            ensure_playbook = Mock(return_value=CommandResult(["test"], 31, "manifest", "SWR review repair is still in progress; do not relaunch"))

            with (
                patch.object(autokeel, "run_autokeel_invariants", return_value=CommandResult([], 0, "{}", "")),
                patch.object(autokeel, "run_verify_v1", return_value=CommandResult([], 1, "{}", "")),
                patch.object(autokeel, "evaluate_tripwires", return_value=CommandResult([], 0, "{}", "")),
                patch.object(autokeel, "ensure_slice_brief"),
                patch.object(autokeel, "failure_budget_exceeded", return_value=CommandResult([], 0, "", "")),
                patch.object(autokeel, "recover_passed_slice_run", return_value=None),
                patch.object(autokeel, "restore_repaired_escalated_slice_run", return_value=None),
                patch.object(autokeel, "run_slice_readiness", readiness),
                patch.object(autokeel, "ensure_lane_decision", return_value=CommandResult([], 0, "", "")),
                patch.object(autokeel, "required_external_evidence_ready", return_value=CommandResult([], 0, "", "")),
                patch.object(autokeel, "ensure_playbook", ensure_playbook),
            ):
                code = autokeel.run_once(requested_slice="S05")

            self.assertEqual(code, 0)
            readiness.assert_not_called()
            ensure_playbook.assert_called_once()

    def test_s05_swr_review_block_is_not_recorded_as_generic_compile_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            set_completed_through_s04(root)

            autokeel = AutoKeel(root)
            ensure_playbook = Mock(return_value=CommandResult(["test"], 32, "", "SWR review history failed closed"))
            record_failure = Mock()

            with (
                patch.object(autokeel, "run_autokeel_invariants", return_value=CommandResult([], 0, "{}", "")),
                patch.object(autokeel, "run_verify_v1", return_value=CommandResult([], 1, "{}", "")),
                patch.object(autokeel, "evaluate_tripwires", return_value=CommandResult([], 0, "{}", "")),
                patch.object(autokeel, "ensure_slice_brief"),
                patch.object(autokeel, "failure_budget_exceeded", return_value=CommandResult([], 0, "", "")),
                patch.object(autokeel, "recover_passed_slice_run", return_value=None),
                patch.object(autokeel, "restore_repaired_escalated_slice_run", return_value=None),
                patch.object(autokeel, "run_slice_readiness", return_value=CommandResult([], 0, "", "")),
                patch.object(autokeel, "ensure_lane_decision", return_value=CommandResult([], 0, "", "")),
                patch.object(autokeel, "required_external_evidence_ready", return_value=CommandResult([], 0, "", "")),
                patch.object(autokeel, "ensure_playbook", ensure_playbook),
                patch.object(autokeel, "record_failure", record_failure),
            ):
                code = autokeel.run_once(requested_slice="S05")

            self.assertEqual(code, 32)
            ensure_playbook.assert_called_once()
            record_failure.assert_not_called()


if __name__ == "__main__":
    unittest.main()

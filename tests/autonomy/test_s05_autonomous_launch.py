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


if __name__ == "__main__":
    unittest.main()

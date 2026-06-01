from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from ops.autonomy.autokeel import AutoKeel, CommandResult, write_json_atomic


ROOT = Path(__file__).resolve().parents[2]


def copy_fixture(dst: Path) -> None:
    shutil.copytree(ROOT / "ops", dst / "ops")
    shutil.copytree(ROOT / "scripts", dst / "scripts")
    shutil.copytree(ROOT / "tests", dst / "tests")
    (dst / ".gitignore").write_text(
        "data/\nprivate/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n.local/\n",
        encoding="utf-8",
    )
    (dst / "ops/autonomy/failure_ledger.jsonl").write_text("", encoding="utf-8")
    write_json_atomic(
        dst / "ops/autonomy/autonomy_state.json",
        {"active_run": None, "active_swr_run": None, "completed_slices": ["S01", "S02", "S03", "S04"], "current_slice": None, "last_event_id": 0, "v1_complete": False},
    )


def configure_s05_repair(root: Path) -> Path:
    run_dir = root / ".local/autokeel/swr/runs/test-s05"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = run_dir / "run_manifest.json"
    write_json_atomic(manifest, {"run_id": "run_test_s05", "status": "waiting_for_review", "stages": []})

    slices_path = root / "ops/autonomy/slices.json"
    slices = json.loads(slices_path.read_text(encoding="utf-8"))
    for item in slices:
        if item.get("id") in {"S01", "S02", "S03", "S04"}:
            item["status"] = "complete"
        if item.get("id") == "S05":
            item["status"] = "blocked_compile_inputs"
            item["swr_review_repair"] = {
                "status": "planned",
                "repair_action": "rerun_review_lane",
                "repair_stage_id": "source_authority_map",
                "run_id": "run_test_s05",
                "run_dir": str(run_dir.relative_to(root)),
                "run_manifest": str(manifest.relative_to(root)),
                "created_at": "2026-06-01T11:50:56-04:00",
            }
    write_json_atomic(slices_path, slices)
    return manifest


class S05SWRControlPlaneRegressionTests(unittest.TestCase):
    def test_planned_repair_is_not_fresh_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            configure_s05_repair(root)

            op = AutoKeel(root=root, dry_run=True)
            s05 = next(item for item in op.load_slices() if item["id"] == "S05")

            self.assertEqual(op.swr_execution_phase(s05), "swr_review_repair")
            self.assertFalse(op.should_run_slice_readiness(s05))

    def test_stale_review_repair_directory_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            configure_s05_repair(root)
            op = AutoKeel(root=root, dry_run=True)

            review_dir = root / ".local/autokeel/swr/review_lane/stale"
            review_dir.mkdir(parents=True)
            (review_dir / "old_reviewer_sidecar.json").write_text("{}", encoding="utf-8")

            result = op.assert_fresh_review_repair_dir(review_dir, "new-cycle")

            self.assertFalse(result.ok)
            self.assertIn("prior sidecars", result.stderr)

    def test_repair_cycle_id_matches_review_decision_schema(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            configure_s05_repair(root)
            op = AutoKeel(root=root, dry_run=True)
            s05 = next(item for item in op.load_slices() if item["id"] == "S05")

            cycle_id = op.swr_repair_cycle_id(s05, "source_authority_map")

            self.assertRegex(cycle_id, r"^[a-z0-9][a-z0-9._-]{0,127}$")

    def test_budget_checkpoint_required_for_control_plane_overage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            configure_s05_repair(root)

            ledger = root / "ops/autonomy/failure_ledger.jsonl"
            rows = []
            for idx in range(4):
                evidence = root / f"docs/evidence/control-plane-{idx}.md"
                evidence.parent.mkdir(parents=True, exist_ok=True)
                evidence.write_text("closure evidence\n", encoding="utf-8")
                rows.append(
                    {
                        "schema_version": "autokeel.failure_ledger.v2",
                        "slice": "S05",
                        "failure_class": "audit_failure",
                        "severity": "high",
                        "open": False,
                        "failure_origin": "autokeel_wrapper",
                        "description": f"pre-launch readiness false positive {idx}",
                        "root_cause_id": "S05-AUDIT-FAILURE",
                        "closure_evidence": str(evidence.relative_to(root)),
                        "closure_note": "closed with local evidence",
                        "supersedes": [],
                        "superseded_by": None,
                        "false_positive": True,
                        "closure_validation_command": "test fixture",
                    }
                )
            ledger.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")

            op = AutoKeel(root=root, dry_run=True)
            s05 = next(item for item in op.load_slices() if item["id"] == "S05")
            result = op.failure_budget_exceeded(s05)

            self.assertFalse(result.ok)
            self.assertIn("control-plane repair checkpoint required", result.stderr)


if __name__ == "__main__":
    unittest.main()

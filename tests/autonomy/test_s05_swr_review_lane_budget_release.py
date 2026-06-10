from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ops.autonomy.autokeel import AutoKeel, write_json_atomic


ROOT = Path(__file__).resolve().parents[2]


def copy_fixture(dst: Path) -> None:
    shutil.copytree(ROOT / "ops", dst / "ops")
    # The real repo's state-digest sidecar must never leak into fixtures:
    # fixture roots hand-edit state, and the first fixture tick re-baselines.
    (dst / "ops/autonomy/state_digest.json").unlink(missing_ok=True)
    shutil.copytree(ROOT / "scripts", dst / "scripts")
    shutil.copytree(ROOT / "docs", dst / "docs")
    shutil.copytree(ROOT / "src", dst / "src")
    release_artifact = dst / "docs/evidence/s05-swr-review-lane-budget-release.json"
    if release_artifact.exists():
        release_artifact.unlink()
    (dst / ".gitignore").write_text(
        "data/\nprivate/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n.local/\n",
        encoding="utf-8",
    )
    (dst / "ops/autonomy/failure_ledger.jsonl").write_text("", encoding="utf-8")


def configure_s05_with_planned_repair(root: Path) -> None:
    run_dir = root / ".local/autokeel/swr/runs/test-s05"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = run_dir / "run_manifest.json"
    write_json_atomic(manifest, {"run_id": "run_20260601_133046_ae09e1ea", "status": "quarantined", "stages": []})

    slices_path = root / "ops/autonomy/slices.json"
    slices = json.loads(slices_path.read_text(encoding="utf-8"))
    for item in slices:
        if item.get("id") in {"S01", "S02", "S03", "S04"}:
            item["status"] = "complete"
        if item.get("id") == "S05":
            item["status"] = "blocked_compile_inputs"
            item["swr_review_repair"] = {
                "status": "planned",
                "created_at": "2026-06-01T11:50:56-04:00",
                "repair_action": "rerun_review_lane",
                "repair_stage_id": "source_authority_map",
                "run_id": "run_20260601_133046_ae09e1ea",
                "run_dir": str(run_dir.relative_to(root)),
                "run_manifest": str(manifest.relative_to(root)),
            }
    write_json_atomic(slices_path, slices)
    write_json_atomic(
        root / "ops/autonomy/autonomy_state.json",
        {
            "active_run": None,
            "active_swr_run": None,
            "completed_slices": ["S01", "S02", "S03", "S04"],
            "current_slice": "S05",
            "last_event_id": 0,
            "v1_complete": False,
        },
    )


class S05SWRReviewLaneBudgetReleaseTests(unittest.TestCase):
    def test_swr_review_lane_overage_requires_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            configure_s05_with_planned_repair(root)

            op = AutoKeel(root=root, dry_run=True)
            s05 = next(item for item in op.load_slices() if item["id"] == "S05")

            result = op.swr_review_lane_budget_release_valid(s05)

            self.assertFalse(result.ok)
            self.assertIn("budget release missing", result.stderr)

    def test_release_key_must_match_current_repair_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            configure_s05_with_planned_repair(root)

            op = AutoKeel(root=root, dry_run=True)
            s05 = next(item for item in op.load_slices() if item["id"] == "S05")
            release_path = root / "docs/evidence/s05-swr-review-lane-budget-release.json"
            write_json_atomic(
                release_path,
                {
                    "schema_version": "autokeel.swr_review_lane_budget_release.v1",
                    "slice": "S05",
                    "release_type": "swr_review_lane_budget",
                    "verdict": "pass",
                    "allow_one_next_repair_tick": True,
                    "consumed_at": None,
                    "expires_at": "2999-01-01T00:00:00+00:00",
                    "release_key": "wrong-key",
                    "repair_plan": {},
                },
            )

            result = op.swr_review_lane_budget_release_valid(s05)

            self.assertFalse(result.ok)
            self.assertIn("does not match current repair plan", result.stderr)

    def test_valid_release_is_accepted_for_current_plan(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            configure_s05_with_planned_repair(root)

            op = AutoKeel(root=root, dry_run=True)
            s05 = next(item for item in op.load_slices() if item["id"] == "S05")
            release_path = root / "docs/evidence/s05-swr-review-lane-budget-release.json"
            write_json_atomic(
                release_path,
                {
                    "schema_version": "autokeel.swr_review_lane_budget_release.v1",
                    "slice": "S05",
                    "release_type": "swr_review_lane_budget",
                    "verdict": "pass",
                    "allow_one_next_repair_tick": True,
                    "consumed_at": None,
                    "expires_at": "2999-01-01T00:00:00+00:00",
                    "release_key": op.swr_review_repair_plan_key(s05),
                    "repair_plan": s05["swr_review_repair"],
                },
            )

            result = op.swr_review_lane_budget_release_valid(s05)

            self.assertTrue(result.ok)


if __name__ == "__main__":
    unittest.main()

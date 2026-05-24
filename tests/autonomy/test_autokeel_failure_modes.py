from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from ops.autonomy.autokeel import AutoKeel, write_json_atomic
from scripts.verify_v1 import verify_v1


ROOT = Path(__file__).resolve().parents[2]


def copy_autonomy_fixture(dst: Path) -> None:
    shutil.copytree(ROOT / "ops", dst / "ops")
    shutil.copytree(ROOT / "scripts", dst / "scripts")
    (dst / ".gitignore").write_text("data/\nprivate/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n", encoding="utf-8")


class AutoKeelFailureModeTests(unittest.TestCase):
    def test_blocked_external_is_not_reselected_as_actionable(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            slices[0]["status"] = "blocked_external"
            slices[1]["status"] = "pending"
            write_json_atomic(root / "ops/autonomy/slices.json", slices)

            op = AutoKeel(root=root, dry_run=True)
            self.assertEqual(op.choose_next_slice()["id"], "S02")

    def test_requested_slice_requires_dependencies_unless_forced(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            with self.assertRaisesRegex(Exception, "incomplete dependencies"):
                op.choose_next_slice("S04")
            self.assertEqual(op.choose_next_slice("S04", force=True)["id"], "S04")

    def test_verify_v1_fails_when_completed_slice_has_missing_deliverables(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            import subprocess
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

            slices = [
                {
                    "id": "S01",
                    "required": True,
                    "status": "complete",
                    "deliverables": ["src/db/schema.sql"],
                    "review_artifacts": [],
                    "acceptance": [],
                }
            ]
            write_json_atomic(root / "ops/autonomy/slices.json", slices)
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", {"active_run": None})
            (root / "ops/autonomy/failure_ledger.jsonl").write_text("", encoding="utf-8")

            report = verify_v1(root, run_acceptance_commands=False)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("missing deliverable" in error for error in report["errors"]))

    def test_empty_review_artifact_does_not_pass(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            review = root / "docs/reviews/s01-autonomous-schema-review.md"
            review.parent.mkdir(parents=True)
            review.write_text("pass", encoding="utf-8")

            from scripts.check_autonomous_review_exists import check_review
            report = check_review(root, "S01")
            self.assertEqual(report["status"], "error")
            self.assertTrue(report["errors"])


if __name__ == "__main__":
    unittest.main()

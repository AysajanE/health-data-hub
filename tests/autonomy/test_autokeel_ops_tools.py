from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.close_failure import close_failure
from scripts.evaluate_tripwires import evaluate_tripwires


ROOT = Path(__file__).resolve().parents[2]


def copy_autonomy_fixture(dst: Path) -> None:
    shutil.copytree(ROOT / "ops", dst / "ops")
    shutil.copytree(ROOT / "scripts", dst / "scripts")
    (dst / ".gitignore").write_text("data/\nprivate/\n.env\n", encoding="utf-8")


class AutoKeelOpsToolTests(unittest.TestCase):
    def test_close_failure_marks_matching_open_rows_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            evidence = root / "docs/reviews/closure.md"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("Verdict: pass\n", encoding="utf-8")
            ledger = root / "ops/autonomy/failure_ledger.jsonl"
            ledger.write_text(
                json.dumps({"slice": "S01", "failure_class": "manual_gate_leak", "severity": "high", "open": True}) + "\n",
                encoding="utf-8",
            )

            report = close_failure(root, "S01", "manual_gate_leak", "docs/reviews/closure.md", "closed in test")

            self.assertEqual(report["status"], "ok")
            rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertFalse(rows[0]["open"])
            self.assertEqual(rows[0]["closure_evidence"], "docs/reviews/closure.md")

    def test_tripwires_future_deadlines_do_not_fire(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            report = evaluate_tripwires(root)
            self.assertEqual(report["status"], "ok", report)
            self.assertEqual(report["fired"], [])

    def test_preflight_checks_git_repo_and_policy_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            from scripts.verify_autonomy_preflight import preflight

            report = preflight(root, Path("/Users/aeziz-local/keel"), strict_tools=False, run_keel_smoke=False)
            self.assertTrue(report["checks"]["git_repo"])
            self.assertFalse(any("policy.yaml missing required key" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()

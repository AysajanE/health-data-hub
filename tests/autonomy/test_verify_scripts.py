from __future__ import annotations

import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from ops.autonomy.autokeel import write_json_atomic
from scripts.check_autonomous_review_exists import check_review
from scripts.check_no_tracked_data import check_no_tracked_data
from scripts.keel_status_digest import digest_status
from scripts.verify_s02_readiness import verify_s02_readiness
from scripts.verify_v1 import verify_v1


class VerifyScriptsTests(unittest.TestCase):
    def test_status_digest_extracts_terminal_state(self) -> None:
        payload = {"run_id": "run_1", "items": [{"state": "passed"}, {"state": "blocked_external"}]}
        digest = digest_status(payload)
        self.assertEqual(digest["terminal_state"], "blocked_external")

    def test_check_no_tracked_data_rejects_data_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / "data/raw").mkdir(parents=True)
            (root / "data/raw/oura.json").write_text("{}", encoding="utf-8")
            subprocess.run(["git", "add", "data/raw/oura.json"], cwd=root, check=True)
            report = check_no_tracked_data(root)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("tracked sensitive path" in error for error in report["errors"]))

    def test_verify_v1_fails_with_incomplete_slices(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ops/autonomy").mkdir(parents=True)
            write_json_atomic(root / "ops/autonomy/slices.json", [{"id": "S01", "required": True, "status": "pending"}])
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", {"active_run": None})
            (root / "ops/autonomy/failure_ledger.jsonl").write_text("", encoding="utf-8")
            report = verify_v1(root)
            self.assertEqual(report["status"], "error")
            self.assertIn("S01", report["incomplete_slices"])

    def test_verify_v1_fails_with_open_manual_gate_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ops/autonomy").mkdir(parents=True)
            write_json_atomic(root / "ops/autonomy/slices.json", [{"id": "S01", "required": True, "status": "complete"}])
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", {"active_run": None})
            (root / "ops/autonomy/failure_ledger.jsonl").write_text(json.dumps({"slice": "S01", "failure_class": "manual_gate_leak", "severity": "high", "open": True}) + "\n", encoding="utf-8")
            report = verify_v1(root)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("open critical" in error for error in report["errors"]))

    def test_review_requires_existing_command_evidence_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ops/autonomy").mkdir(parents=True)
            review = root / "docs/reviews/s01.md"
            review.parent.mkdir(parents=True)
            review.write_text(
                "# Autonomous Slice Review: S01\n\n"
                "Autonomous slice review provenance: independent reviewer.\n\n"
                "Verdict: pass\n"
                "Evidence files checked:\n- `src/db/schema.sql`\n"
                "Exact commands run:\n- `python scripts/check_schema_contract.py`\n"
                "Command evidence: docs/evidence/s01-command-output.json\n"
                "Blocking findings: none\n",
                encoding="utf-8",
            )
            write_json_atomic(
                root / "ops/autonomy/slices.json",
                [{"id": "S01", "required": True, "review_artifacts": ["docs/reviews/s01.md"]}],
            )

            missing = check_review(root, "S01")
            self.assertEqual(missing["status"], "error")
            self.assertTrue(any("command evidence path does not exist" in error for error in missing["errors"]))

            evidence = root / "docs/evidence/s01-command-output.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text('{"status":"ok"}\n', encoding="utf-8")
            structurally_invalid = check_review(root, "S01")
            self.assertEqual(structurally_invalid["status"], "error")
            self.assertTrue(any("non-empty commands list" in error for error in structurally_invalid["errors"]))

            evidence.write_text(
                json.dumps(
                    {
                        "commands": [
                            {
                                "command": "python scripts/check_schema_contract.py",
                                "exit_code": 0,
                                "stdout_tail": "ok",
                                "stderr_tail": "",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            present = check_review(root, "S01")
            self.assertEqual(present["status"], "ok", present)

    def test_review_rejects_failed_or_secret_command_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ops/autonomy").mkdir(parents=True)
            review = root / "docs/reviews/s01.md"
            review.parent.mkdir(parents=True)
            review.write_text(
                "# Autonomous Slice Review: S01\n\n"
                "Autonomous slice review provenance: independent reviewer.\n\n"
                "Verdict: pass\n"
                "Evidence files checked:\n- `src/db/schema.sql`\n"
                "Exact commands run:\n- `python scripts/check_schema_contract.py`\n"
                "Command evidence: docs/evidence/s01-command-output.json\n"
                "Blocking findings: none\n",
                encoding="utf-8",
            )
            write_json_atomic(
                root / "ops/autonomy/slices.json",
                [{"id": "S01", "required": True, "review_artifacts": ["docs/reviews/s01.md"]}],
            )
            evidence = root / "docs/evidence/s01-command-output.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text(
                json.dumps(
                    {
                        "commands": [
                            {
                                "command": "python scripts/check_schema_contract.py",
                                "exit_code": 1,
                                "stdout_tail": "authorization token was redacted",
                                "stderr_tail": "failed",
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            report = check_review(root, "S01")
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("nonzero exit_code" in error for error in report["errors"]))
            self.assertTrue(any("secret marker" in error for error in report["errors"]))

    def test_verify_s02_readiness_requires_lane_decision_and_reviews(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / ".gitignore").write_text("data/\nprivate/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n", encoding="utf-8")
            (root / "ops/autonomy").mkdir(parents=True)
            write_json_atomic(
                root / "ops/autonomy/slices.json",
                [
                    {"id": "S01", "required": True, "status": "complete"},
                    {
                        "id": "S02",
                        "required": True,
                        "status": "pending",
                        "lane": "swr_preferred",
                        "risk": "high",
                        "review_artifacts": [
                            "docs/reviews/s02-autonomous-security-review.md",
                            "docs/reviews/s02-autonomous-privacy-review.md",
                        ],
                    },
                ],
            )

            missing = verify_s02_readiness(root)
            self.assertEqual(missing["status"], "error")
            self.assertTrue(any("missing lane_decision" in error for error in missing["errors"]))

    def test_verify_s02_readiness_passes_with_decision_reviews_and_safe_git_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / ".gitignore").write_text("data/\nprivate/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n", encoding="utf-8")
            (root / "ops/autonomy/decisions").mkdir(parents=True)
            write_json_atomic(
                root / "ops/autonomy/decisions/S02-lane.json",
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
                    "commands": [
                        {
                            "command": "python scripts/verify_autonomy_preflight.py --json",
                            "exit_code": 0,
                            "stdout_tail": "ok",
                            "stderr_tail": "",
                        }
                    ],
                    "verdict": "pass",
                },
            )
            write_json_atomic(
                root / "ops/autonomy/slices.json",
                [
                    {"id": "S01", "required": True, "status": "complete"},
                    {
                        "id": "S02",
                        "required": True,
                        "status": "pending",
                        "lane": "swr_preferred",
                        "risk": "high",
                        "lane_decision": "ops/autonomy/decisions/S02-lane.json",
                        "review_artifacts": [
                            "docs/reviews/s02-autonomous-security-review.md",
                            "docs/reviews/s02-autonomous-privacy-review.md",
                        ],
                    },
                ],
            )
            evidence = root / "docs/evidence/s02-command-output.json"
            evidence.parent.mkdir(parents=True)
            write_json_atomic(
                evidence,
                {
                    "commands": [
                        {
                            "command": "python -m pytest tests/test_api_security.py -q",
                            "exit_code": 0,
                            "stdout_tail": "passed",
                            "stderr_tail": "",
                        }
                    ]
                },
            )
            for rel in (
                "docs/reviews/s02-autonomous-security-review.md",
                "docs/reviews/s02-autonomous-privacy-review.md",
            ):
                path = root / rel
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(
                    "# Autonomous Slice Review: S02\n\n"
                    "Autonomous slice review provenance: independent reviewer.\n\n"
                    "Verdict: pass\n"
                    "Evidence files checked:\n- `src/api/mood.py`\n"
                    "Exact commands run:\n- `python -m pytest tests/test_api_security.py -q`\n"
                    "Command evidence: docs/evidence/s02-command-output.json\n"
                    "Blocking findings: none\n",
                    encoding="utf-8",
                )

            report = verify_s02_readiness(root)
            self.assertEqual(report["status"], "ok", report)

    def test_verify_v1_fails_when_ship_branch_head_differs_from_recorded_commit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Tests"], cwd=root, check=True)
            (root / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "seed"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            old_commit = subprocess.run(
                ["git", "rev-parse", "HEAD"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            subprocess.run(["git", "checkout", "-b", "ship/s01"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / "new.txt").write_text("new\n", encoding="utf-8")
            subprocess.run(["git", "add", "new.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "ship moved"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)

            (root / "ops/autonomy").mkdir(parents=True)
            write_json_atomic(
                root / "ops/autonomy/slices.json",
                [
                    {
                        "id": "S01",
                        "required": True,
                        "status": "complete",
                        "run_id": "RUN_TEST",
                        "ship_branch": "ship/s01",
                        "ship_commit": old_commit,
                        "acceptance": [],
                        "deliverables": [],
                        "review_artifacts": [],
                    }
                ],
            )
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", {"active_run": None})
            (root / "ops/autonomy/failure_ledger.jsonl").write_text("", encoding="utf-8")

            report = verify_v1(root, run_acceptance_commands=False)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("recorded ship_commit" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()

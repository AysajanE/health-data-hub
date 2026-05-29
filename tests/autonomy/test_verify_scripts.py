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
from scripts.verify_failure_ledger import verify_failure_ledger
from scripts.verify_run_retarget_evidence import verify_run_retarget_evidence
from scripts.verify_ship_invariants import verify_ship_invariants
from scripts.verify_s02_readiness import verify_s02_readiness
from scripts.verify_s03_readiness import evidence_report_exists, verify_s03_readiness
from scripts.verify_v1 import verify_v1


def write_s02_readiness_fixture(root: Path, *, active_run: dict | None = None, with_policy: bool = False) -> None:
    subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    (root / ".gitignore").write_text("data/\nprivate/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n", encoding="utf-8")
    (root / "ops/autonomy/decisions").mkdir(parents=True)
    if with_policy:
        (root / "ops/autonomy/policy.yaml").write_text("lanes:\n  swr_preferred: use_swr\n", encoding="utf-8")
    write_json_atomic(
        root / "ops/autonomy/autonomy_state.json",
        {"active_run": active_run, "completed_slices": ["S01"], "current_slice": None, "last_event_id": 0},
    )
    write_json_atomic(
        root / "ops/autonomy/decisions/S02-lane.json",
        {
            "created_at": "2026-05-26T00:00:00-04:00",
            "status": "accepted",
            "slice": "S02",
            "lane": "swr_preferred",
            "decision": "use_swr",
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
                "playbook": "docs/playbooks/s02-mood-api.playbook.md",
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

    def test_check_no_tracked_data_allows_exact_fake_test_tokens_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / ".gitignore").write_text(
                "data/\nprivate/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n",
                encoding="utf-8",
            )
            tests_dir = root / "tests"
            tests_dir.mkdir()
            fixture = tests_dir / "test_api_security.py"
            fixture.write_text(
                '# explicit fake test fixture context\n'
                'FAKE_MOOD_TOKEN = "test-mood-token"\n'
                'ENV = {"MOOD_TOKEN": "fake-mood-token"}\n',
                encoding="utf-8",
            )
            subprocess.run(["git", "add", ".gitignore", str(fixture.relative_to(root))], cwd=root, check=True)

            report = check_no_tracked_data(root)
            self.assertEqual(report["status"], "ok", report)

            real_token = "sk_live_" + "realistic_token_value"
            fixture.write_text(f'MOOD_TOKEN = "{real_token}"\n', encoding="utf-8")
            report = check_no_tracked_data(root)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("secret/token value" in error for error in report["errors"]))

            fixture.write_text('# fake test fixture\nMOOD_' + 'TOKEN = "test-only-mood-' + 'token"\n', encoding="utf-8")
            report = check_no_tracked_data(root)
            self.assertEqual(report["status"], "error")

    def test_check_no_tracked_data_allows_policy_env_var_names(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / ".gitignore").write_text(
                "data/\nprivate/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n",
                encoding="utf-8",
            )
            policy = root / "ops/autonomy/policy.yaml"
            policy.parent.mkdir(parents=True)
            policy.write_text(
                "swr:\n"
                "  required_env:\n"
                "    - OPENAI_API_KEY\n"
                "swr_repair_authorization:\n"
                "  auto_authorize_single_stage_repair: true\n",
                encoding="utf-8",
            )
            subprocess.run(["git", "add", ".gitignore", str(policy.relative_to(root))], cwd=root, check=True)

            report = check_no_tracked_data(root)
            self.assertEqual(report["status"], "ok", report)

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
                    "decision": "use_swr",
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

    def test_s02_readiness_fails_with_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_s02_readiness_fixture(
                root,
                active_run={
                    "slice": "S02",
                    "run_id": "RUN_20260526T173005Z_e4b9f767c7024cc9b3741d04055ec544",
                },
            )

            report = verify_s02_readiness(root)

            self.assertEqual(report["status"], "error")
            self.assertEqual(
                report["checks"]["active_run"]["run_id"],
                "RUN_20260526T173005Z_e4b9f767c7024cc9b3741d04055ec544",
            )
            self.assertTrue(any("active_run must be null" in error for error in report["errors"]))

    def test_s02_readiness_fails_when_playbook_exists_without_swr_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            write_s02_readiness_fixture(root, with_policy=True)
            playbook = root / "docs/playbooks/s02-mood-api.playbook.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text("# stale compiler playbook\n", encoding="utf-8")

            report = verify_s02_readiness(root)

            self.assertEqual(report["status"], "error")
            self.assertTrue(report["checks"]["canonical_playbook_exists"])
            self.assertFalse(report["checks"]["swr_evidence_exists"])
            self.assertTrue(any("without matching SWR evidence" in error for error in report["errors"]))

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

    def test_verify_failure_ledger_rejects_open_high_and_missing_closure_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ops/autonomy").mkdir(parents=True)
            (root / "ops/autonomy/failure_ledger.jsonl").write_text(
                json.dumps(
                    {
                        "schema_version": "autokeel.failure_ledger.v2",
                        "slice": "S03",
                        "failure_class": "provider_auth_failure",
                        "severity": "high",
                        "open": True,
                        "root_cause_id": "S03-PROVIDER-AUTH",
                        "failure_origin": "external_provider",
                        "supersedes": [],
                        "superseded_by": None,
                        "false_positive": False,
                        "closure_validation_command": "",
                    }
                )
                + "\n"
                + json.dumps({"slice": "S03", "failure_class": "audit_failure", "severity": "high", "open": False})
                + "\n",
                encoding="utf-8",
            )

            report = verify_failure_ledger(root, slice_id="S03")
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("provider_auth_failure is open" in error for error in report["errors"]))
            self.assertTrue(any("closure_evidence" in error for error in report["errors"]))

    def test_verify_s03_readiness_requires_s01_s02_and_provider_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / ".gitignore").write_text(
                "data/\nprivate/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n",
                encoding="utf-8",
            )
            (root / "ops/autonomy").mkdir(parents=True)
            write_json_atomic(
                root / "ops/autonomy/slices.json",
                [
                    {"id": "S01", "required": True, "status": "complete"},
                    {"id": "S02", "required": True, "status": "complete"},
                    {
                        "id": "S03",
                        "required": True,
                        "status": "pending",
                        "review_artifacts": ["docs/reviews/s03-autonomous-ingestion-evidence-review.md"],
                    },
                ],
            )
            (root / "ops/autonomy/failure_ledger.jsonl").write_text("", encoding="utf-8")

            report = verify_s03_readiness(root)
            self.assertEqual(report["status"], "error")
            joined = "\n".join(report["errors"])
            self.assertIn("Oura evidence preflight", joined)
            self.assertIn("pyEight evidence/fallback", joined)

            evidence = root / "private/evidence/S03/oura_smoke/report.json"
            evidence.parent.mkdir(parents=True)
            evidence.write_text(json.dumps({"status": "blocked_external"}), encoding="utf-8")
            decision = root / "ops/autonomy/decisions/pyeight-fallback.json"
            decision.parent.mkdir(parents=True)
            decision.write_text(json.dumps({"status": "fallback_accepted", "action": "oura_only_v1"}), encoding="utf-8")
            report = verify_s03_readiness(root)
            self.assertEqual(report["status"], "ok", report)
            self.assertTrue(report["checks"]["pyeight_provider_state_explicit"])
            self.assertTrue(report["checks"]["pyeight_fallback_explicit"])

    def test_verify_s03_readiness_accepts_sanitized_pyeight_evidence_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / ".gitignore").write_text(
                "data/\nprivate/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n",
                encoding="utf-8",
            )
            (root / "ops/autonomy/decisions").mkdir(parents=True)
            write_json_atomic(
                root / "ops/autonomy/slices.json",
                [
                    {"id": "S01", "required": True, "status": "complete"},
                    {"id": "S02", "required": True, "status": "complete"},
                    {
                        "id": "S03",
                        "required": True,
                        "status": "pending",
                        "review_artifacts": ["docs/reviews/s03-autonomous-ingestion-evidence-review.md"],
                    },
                ],
            )
            (root / "ops/autonomy/failure_ledger.jsonl").write_text("", encoding="utf-8")
            oura = root / "private/evidence/S03/oura_smoke/report.json"
            oura.parent.mkdir(parents=True)
            oura.write_text(json.dumps({"status": "blocked_external"}), encoding="utf-8")
            write_json_atomic(
                root / "ops/autonomy/decisions/S03-pyeight-evidence-20260529.json",
                {
                    "schema_version": "autokeel.provider_evidence_decision.v1",
                    "slice": "S03",
                    "provider": "pyeight",
                    "status": "ok",
                    "evidence_status": "ok",
                    "evidence_path": "private/evidence/S03/pyeight_smoke/pyeight_smoke-20260529T175729-0400.json",
                    "fallback_active": False,
                },
            )

            report = verify_s03_readiness(root)

            self.assertEqual(report["status"], "ok", report)
            self.assertTrue(report["checks"]["pyeight_provider_state_explicit"])
            self.assertFalse(report["checks"]["pyeight_fallback_explicit"])
            self.assertEqual(
                report["checks"]["pyeight_decision"],
                "ops/autonomy/decisions/S03-pyeight-evidence-20260529.json",
            )

    def test_verify_s03_readiness_reports_newest_provider_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence_dir = root / "private/evidence/S03/pyeight_smoke"
            evidence_dir.mkdir(parents=True)
            older = evidence_dir / "pyeight_smoke-20260101T000000-0000.json"
            newer = evidence_dir / "pyeight_smoke-20260102T000000-0000.json"
            older.write_text(json.dumps({"status": "blocked_external"}), encoding="utf-8")
            newer.write_text(json.dumps({"status": "error"}), encoding="utf-8")

            ok, path = evidence_report_exists(root, "private/evidence/S03/pyeight_smoke")

            self.assertTrue(ok)
            self.assertEqual(path, "private/evidence/S03/pyeight_smoke/pyeight_smoke-20260102T000000-0000.json")

    def test_ship_invariants_require_detached_worktree_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Tests"], cwd=root, check=True)
            (root / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "seed"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            subprocess.run(["git", "branch", "ship/s01", commit], cwd=root, check=True)
            (root / "ops/autonomy").mkdir(parents=True)
            write_json_atomic(
                root / "ops/autonomy/slices.json",
                [{"id": "S01", "status": "complete", "run_id": "RUN_TEST", "ship_branch": "ship/s01", "ship_commit": commit}],
            )
            (root / "ops/autonomy/events.jsonl").write_text(
                json.dumps({"slice": "S01", "event": "slice_ship_branch_created", "details": {"operator_branch_before": "main", "operator_branch_after": "main"}})
                + "\n",
                encoding="utf-8",
            )

            report = verify_ship_invariants(root, "S01")
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("detached ship worktree" in error for error in report["errors"]))

    def test_run_retarget_evidence_requires_ancestry_and_closure_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Tests"], cwd=root, check=True)
            (root / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "seed"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            old_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            (root / "new.txt").write_text("new\n", encoding="utf-8")
            subprocess.run(["git", "add", "new.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "new"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            new_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            merge_base = subprocess.run(["git", "merge-base", old_head, new_head], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            closure = root / "docs/evidence/root-cause.md"
            closure.parent.mkdir(parents=True)
            closure.write_text("closed\n", encoding="utf-8")
            evidence = root / "docs/evidence/S02-run-retarget-test.json"
            evidence.write_text(
                json.dumps(
                    {
                        "slice": "S02",
                        "run_id": "RUN_TEST",
                        "old_run_branch_head": old_head,
                        "new_target_commit": new_head,
                        "merge_base": merge_base,
                        "item_checkpoint_ancestry_proof": "checkpoint commit is ancestor",
                        "terminal_counts_before": {"passed": 6},
                        "terminal_counts_after": {"passed": 6},
                        "reason": "repair item 07 only",
                        "closure_evidence": str(closure.relative_to(root)),
                    }
                ),
                encoding="utf-8",
            )

            report = verify_run_retarget_evidence(evidence, root=root)
            self.assertEqual(report["status"], "ok", report)

            payload = json.loads(evidence.read_text(encoding="utf-8"))
            payload["closure_evidence"] = "docs/evidence/missing.md"
            evidence.write_text(json.dumps(payload), encoding="utf-8")
            report = verify_run_retarget_evidence(evidence, root=root)
            self.assertEqual(report["status"], "error")


if __name__ == "__main__":
    unittest.main()

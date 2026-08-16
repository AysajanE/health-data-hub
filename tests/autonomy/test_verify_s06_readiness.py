from __future__ import annotations

import json
import os
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Any, Iterator
from unittest.mock import patch

import scripts.verify_s06_readiness as readiness
from scripts.materialize_swr_lane_decision import decision_input_paths


HEAD_COMMIT = "a" * 40
CURRENT_DIGESTS = {
    "ops/autonomy/autonomy_state.json": "state-digest",
    "ops/autonomy/slices.json": "slices-digest",
}
LANE_DECISION_PATH = "ops/autonomy/decisions/S06-lane-decision-test.json"
S06_REVIEW_ARTIFACTS = [
    "docs/reviews/s06-autonomous-causal-framing-review.md",
    "docs/reviews/s06-autonomous-counterfactual-safety-review.md",
]


def write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def prerequisite_input_tree() -> dict[str, str]:
    paths = (
        *readiness.REQUIRED_DEPENDENCY_SURFACES,
        *readiness.REQUIRED_RECOVERY_DEPENDENCY_SURFACES,
        *readiness.REQUIRED_INPUT_DOCS,
        *readiness.REQUIRED_CONTROL_SURFACES,
    )
    return {rel: f"blob:{rel}" for rel in paths}


def make_fixture(
    root: Path,
    *,
    s11_status: str = "complete",
    decision_head: str = HEAD_COMMIT,
    decision_input_tree: dict[str, str] | None = None,
) -> None:
    slices = [
        {"id": "S11", "status": s11_status, "depends_on": []},
        {"id": "S12", "status": "complete", "depends_on": ["S11"]},
        {
            "id": "S06",
            "status": "pending",
            "depends_on": ["S11", "S12"],
            "lane": "swr_preferred",
            "risk": "high",
            "review_artifacts": S06_REVIEW_ARTIFACTS,
            "lane_decision": LANE_DECISION_PATH,
        },
    ]
    write_json(root / "ops/autonomy/slices.json", slices)
    write_json(
        root / LANE_DECISION_PATH,
        {
            "created_at": "2026-08-16T12:00:00-04:00",
            "status": "accepted",
            "slice": "S06",
            "lane": "swr_preferred",
            "decision": "use_swr",
            "risk": "high",
            "review_artifacts": S06_REVIEW_ARTIFACTS,
            "commands": [
                {
                    "command": "python -m ops.autonomy.autokeel --doctor --strict-swr S06",
                    "exit_code": 0,
                    "stdout_tail": "ok",
                    "stderr_tail": "",
                }
            ],
            "verdict": "pass",
            "head_commit": decision_head,
            "input_tree": prerequisite_input_tree() if decision_input_tree is None else decision_input_tree,
        },
    )
    write_json(root / "ops/autonomy/state_digest.json", {"digests": CURRENT_DIGESTS})
    write_json(
        root / "ops/autonomy/autonomy_state.json",
        {"active_run": None, "active_swr_run": None},
    )
    failure_ledger = root / "ops/autonomy/failure_ledger.jsonl"
    failure_ledger.parent.mkdir(parents=True, exist_ok=True)
    failure_ledger.write_text("", encoding="utf-8")


@contextmanager
def mocked_readiness_dependencies(
    *,
    tripwire_report: dict[str, Any] | None = None,
    dirty_primary_input: str | None = None,
) -> Iterator[None]:
    input_tree = prerequisite_input_tree()

    def fake_git_stdout(_root: Path, *argv: str) -> tuple[bool, str]:
        if argv == ("rev-parse", "HEAD^{commit}"):
            return True, HEAD_COMMIT
        if len(argv) == 2 and argv[0] == "rev-parse" and argv[1].startswith("HEAD:"):
            rel = argv[1][len("HEAD:") :]
            return True, input_tree[rel]
        if len(argv) == 3 and argv[:2] == ("hash-object", "--"):
            rel = argv[2]
            if rel == dirty_primary_input:
                return True, f"dirty:{rel}"
            return True, input_tree[rel]
        raise AssertionError(f"unexpected git command: {argv}")

    with ExitStack() as stack:
        stack.enter_context(patch.object(readiness, "tracked_at_head", return_value=True))
        stack.enter_context(patch.object(readiness, "git_stdout", side_effect=fake_git_stdout))
        stack.enter_context(
            patch.object(
                readiness,
                "verify_slice_integration",
                return_value={"status": "ok", "classification": "integrated", "errors": []},
            )
        )
        stack.enter_context(patch.object(readiness, "compute_state_digest", return_value=CURRENT_DIGESTS))
        stack.enter_context(
            patch.object(
                readiness,
                "evaluate_tripwires",
                return_value=tripwire_report or {"status": "ok", "fired": []},
            )
        )
        stack.enter_context(
            patch.object(readiness, "verify_autokeel_invariants", return_value={"status": "ok", "errors": []})
        )
        stack.enter_context(
            patch.object(
                readiness,
                "verify_s12_continuity",
                return_value={
                    "status": "ok",
                    "errors": [],
                    "checks": {"authority_status": "ok", "sync_proof_status": "ok"},
                },
            )
        )
        stack.enter_context(patch.object(readiness.shutil, "which", return_value="/test/bin/reviewer"))
        stack.enter_context(patch.object(readiness, "check_no_tracked_data", return_value={"status": "ok", "errors": []}))
        stack.enter_context(patch.dict(os.environ, {"OPENAI_API_KEY": "test-key"}, clear=True))
        yield


class VerifyS06ReadinessTests(unittest.TestCase):
    def test_green_fully_mocked_contract(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_fixture(root)

            with mocked_readiness_dependencies():
                report = readiness.verify_s06_readiness(root)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["errors"], [])
        self.assertEqual(report["checks"]["dependency_closure"], ["S11", "S12"])
        self.assertEqual(report["checks"]["dependency_integration"]["S11"]["status"], "ok")
        self.assertEqual(report["checks"]["dependency_integration"]["S12"]["status"], "ok")
        self.assertEqual(report["checks"]["s12_continuity"]["authority_status"], "ok")
        self.assertEqual(report["checks"]["s12_continuity"]["sync_proof_status"], "ok")
        self.assertEqual(report["checks"]["state_digest_mismatches"], [])
        self.assertEqual(report["checks"]["lane_decision_policy"], {"status": "ok", "errors": []})
        self.assertEqual(report["checks"]["lane_decision_expected_input_tree"], prerequisite_input_tree())
        self.assertTrue(
            all(
                item["matches_head"]
                for item in report["checks"]["primary_input_worktree_bindings"].values()
            )
        )

    def test_readiness_does_not_mutate_ambient_environment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_fixture(root)

            ambient = {"OPENAI_API_KEY": "ambient-test-key", "AMBIENT_SENTINEL": "unchanged"}
            with mocked_readiness_dependencies(), patch.dict(os.environ, ambient, clear=True):
                before = dict(os.environ)
                report = readiness.verify_s06_readiness(root)
                after = dict(os.environ)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(after, before)
        self.assertEqual(report["checks"]["swr_required_env"], {"OPENAI_API_KEY": "[SET]"})
        self.assertFalse(report["checks"]["process_environment_mutated"])

    def test_readiness_does_not_open_or_accept_repo_local_auth(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_fixture(root)

            ambient = {"AMBIENT_SENTINEL": "unchanged"}
            with (
                mocked_readiness_dependencies(),
                patch.dict(os.environ, ambient, clear=True),
            ):
                before = dict(os.environ)
                report = readiness.verify_s06_readiness(root)
                after = dict(os.environ)

        self.assertEqual(report["status"], "error")
        self.assertEqual(after, before)
        self.assertEqual(report["checks"]["swr_required_env"], {"OPENAI_API_KEY": "[UNSET]"})
        self.assertTrue(any("repo-local env files were not opened" in error for error in report["errors"]))
        self.assertNotIn("PROVIDER_TEST_TOKEN", after)

    def test_readiness_accepts_non_secret_parent_presence_attestation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_fixture(root)

            with (
                mocked_readiness_dependencies(),
                patch.dict(
                    os.environ,
                    {"AUTOKEEL_READINESS_OPENAI_API_KEY_PRESENT": "1"},
                    clear=True,
                ),
            ):
                report = readiness.verify_s06_readiness(root)

        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["checks"]["swr_required_env"], {"OPENAI_API_KEY": "[SET]"})
        self.assertEqual(report["checks"]["swr_required_env_attestation"], "parent_presence_only")

    def test_incomplete_s11_dependency_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_fixture(root, s11_status="pending")

            with mocked_readiness_dependencies():
                report = readiness.verify_s06_readiness(root)

        self.assertEqual(report["status"], "error")
        self.assertIn("S11 must be complete before S06 launch: pending", report["errors"])
        self.assertEqual(report["checks"]["s11_status"], "pending")
        self.assertNotIn("S11", report["checks"]["dependency_integration"])

    def test_current_s12_authority_or_sync_failure_blocks_s06(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_fixture(root)

            with (
                mocked_readiness_dependencies(),
                patch.object(
                    readiness,
                    "verify_s12_continuity",
                    return_value={
                        "status": "blocked_external",
                        "errors": ["S12 current authority: authority has expired"],
                        "checks": {
                            "authority_status": "blocked_external",
                            "sync_proof_status": "not_run_authority_blocked",
                        },
                    },
                ),
            ):
                report = readiness.verify_s06_readiness(root)

        self.assertEqual(report["status"], "error")
        self.assertIn(
            "S06 provider continuity gate: S12 current authority: authority has expired",
            report["errors"],
        )
        self.assertEqual(report["checks"]["s12_continuity"]["authority_status"], "blocked_external")
        self.assertEqual(
            report["checks"]["s12_continuity"]["sync_proof_status"],
            "not_run_authority_blocked",
        )

    def test_fired_typed_tripwire_blocks(self) -> None:
        tripwire_report = {
            "status": "error",
            "fired": [
                {
                    "name": "mood_transport",
                    "evidence_type": "tripwire_mood_transport",
                    "reason": "typed transport evidence fired",
                }
            ],
        }
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_fixture(root)

            with mocked_readiness_dependencies(tripwire_report=tripwire_report):
                report = readiness.verify_s06_readiness(root)

        self.assertEqual(report["status"], "error")
        self.assertIn("tripwires are unresolved: mood_transport", report["errors"])
        self.assertEqual(report["checks"]["tripwires"], {"status": "error", "fired": ["mood_transport"]})

    def test_stale_state_digest_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_fixture(root)
            write_json(
                root / "ops/autonomy/state_digest.json",
                {"digests": {**CURRENT_DIGESTS, "ops/autonomy/slices.json": "stale"}},
            )

            with mocked_readiness_dependencies():
                report = readiness.verify_s06_readiness(root)

        self.assertEqual(report["status"], "error")
        self.assertEqual(report["checks"]["state_digest_mismatches"], ["ops/autonomy/slices.json"])
        self.assertIn("state digest is stale or missing for: ops/autonomy/slices.json", report["errors"])

    def test_stale_lane_decision_head_binding_blocks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_fixture(root, decision_head="b" * 40)

            with mocked_readiness_dependencies():
                report = readiness.verify_s06_readiness(root)

        self.assertEqual(report["status"], "error")
        self.assertTrue(
            any("lane_decision is not bound to current HEAD" in error for error in report["errors"]),
            report["errors"],
        )

    def test_dirty_primary_input_bytes_block_launch(self) -> None:
        dirty_rel = "docs/gstack/health-data-hub-office-hours.md"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_fixture(root)

            with mocked_readiness_dependencies(dirty_primary_input=dirty_rel):
                report = readiness.verify_s06_readiness(root)

        self.assertEqual(report["status"], "error")
        self.assertIn(
            f"S06 primary input worktree bytes do not match HEAD: {dirty_rel} "
            "(commit the final contract or restore the committed bytes before launch)",
            report["errors"],
        )
        self.assertFalse(report["checks"]["primary_input_worktree_bindings"][dirty_rel]["matches_head"])

    def test_incomplete_lane_decision_input_tree_binding_blocks(self) -> None:
        incomplete_tree = prerequisite_input_tree()
        missing_rel = readiness.REQUIRED_CONTROL_SURFACES[-1]
        incomplete_tree.pop(missing_rel)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_fixture(root, decision_input_tree=incomplete_tree)

            with mocked_readiness_dependencies():
                report = readiness.verify_s06_readiness(root)

        self.assertEqual(report["status"], "error")
        self.assertIn(
            "S06 lane_decision input_tree does not match the current committed prerequisites",
            report["errors"],
        )
        self.assertIn(missing_rel, report["checks"]["lane_decision_expected_input_tree"])

    def test_failed_lane_decision_verdict_blocks_before_commit_binding_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_fixture(root)
            decision_path = root / LANE_DECISION_PATH
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
            decision["verdict"] = "fail"
            write_json(decision_path, decision)

            with mocked_readiness_dependencies():
                report = readiness.verify_s06_readiness(root)

        self.assertEqual(report["status"], "error")
        self.assertIn("S06: lane_decision verdict is not pass: " + LANE_DECISION_PATH, report["errors"])
        self.assertEqual(report["checks"]["lane_decision_policy"]["status"], "error")
        self.assertNotIn("head_commit", report["checks"])
        self.assertNotIn("lane_decision_expected_input_tree", report["checks"])

    def test_structurally_invalid_lane_decision_blocks_before_commit_binding_checks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            make_fixture(root)
            decision_path = root / LANE_DECISION_PATH
            decision = json.loads(decision_path.read_text(encoding="utf-8"))
            decision.pop("commands")
            write_json(decision_path, decision)

            with mocked_readiness_dependencies():
                report = readiness.verify_s06_readiness(root)

        self.assertEqual(report["status"], "error")
        self.assertTrue(
            any("lane_decision missing required field commands" in error for error in report["errors"]),
            report["errors"],
        )
        self.assertEqual(report["checks"]["lane_decision_policy"]["status"], "error")
        self.assertNotIn("head_commit", report["checks"])
        self.assertNotIn("lane_decision_expected_input_tree", report["checks"])

    def test_s06_decision_inputs_cover_all_readiness_surfaces(self) -> None:
        target = {
            "autoplan": readiness.REQUIRED_INPUT_DOCS[0],
            "brief": readiness.REQUIRED_INPUT_DOCS[1],
        }
        expected = sorted(
            {
                *readiness.REQUIRED_DEPENDENCY_SURFACES,
                *readiness.REQUIRED_RECOVERY_DEPENDENCY_SURFACES,
                *readiness.REQUIRED_INPUT_DOCS,
                *readiness.REQUIRED_CONTROL_SURFACES,
            }
        )

        self.assertEqual(decision_input_paths("S06", target), expected)
        self.assertIn("docs/gstack/health-data-hub-office-hours.md", expected)


if __name__ == "__main__":
    unittest.main()

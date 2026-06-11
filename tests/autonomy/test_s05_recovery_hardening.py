"""Regression tests for the S05 recovery hardening (2026-06-10).

Covers the contracts introduced after the June-1 S05 incident:
- transport-vs-verdict separation in review decision handling
- stale-sidecar refusal and attempt-unique repair cycle ids
- stability-checkpoint freshness validation
- fresh-launch guard requiring a recorded abandonment decision
- state-digest tamper evidence
- SWR kernel pinning
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from ops.autonomy.autokeel import (  # noqa: E402
    AutoKeel,
    compute_state_digest,
    update_state_digest_sidecar,
    write_json_atomic,
)


def copy_autonomy_fixture(dst: Path) -> None:
    shutil.copytree(ROOT / "ops", dst / "ops")
    # The real repo's state-digest sidecar must never leak into fixtures:
    # fixture roots hand-edit state, and the first fixture tick re-baselines.
    (dst / "ops/autonomy/state_digest.json").unlink(missing_ok=True)
    shutil.copytree(ROOT / "scripts", dst / "scripts")
    (dst / ".gitignore").write_text(
        "data/\nprivate/\n.env\n.local/\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n",
        encoding="utf-8",
    )
    for rel in ("ops/autonomy/failures", "ops/autonomy/heartbeats"):
        shutil.rmtree(dst / rel, ignore_errors=True)
    slices_path = dst / "ops/autonomy/slices.json"
    slices = json.loads(slices_path.read_text(encoding="utf-8"))
    for item in slices:
        item["status"] = "pending"
        for stale_key in (
            "retry_count",
            "failure_path",
            "reason",
            "run_id",
            "swr_run_id",
            "swr_run_manifest",
            "swr_review_repair",
            "swr_validation_repair",
        ):
            item.pop(stale_key, None)
    write_json_atomic(slices_path, slices)
    write_json_atomic(
        dst / "ops/autonomy/autonomy_state.json",
        {"active_run": None, "completed_slices": [], "current_slice": None, "last_event_id": 0, "v1_complete": False},
    )
    (dst / "ops/autonomy/failure_ledger.jsonl").write_text("", encoding="utf-8")
    (dst / "ops/autonomy/events.jsonl").write_text("", encoding="utf-8")


class TransportVsVerdictTests(unittest.TestCase):
    def test_transport_statuses_are_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            for status in ("malformed_output", "timeout", "read_only_violation", "missing_cli", "interrupted"):
                decision_path = root / f"decision-{status}.json"
                write_json_atomic(
                    decision_path,
                    {"status": status, "approval_decision": "blocked", "validation_errors": [f"{status} happened"]},
                )
                error = op.swr_decision_transport_error(f"decision-{status}.json", "Codex reviewer decision")
                self.assertIsNotNone(error, status)
                self.assertIn(f"status={status}", error)

    def test_semantic_decisions_are_not_transport(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            decision_path = root / "decision.json"
            write_json_atomic(
                decision_path,
                {
                    "status": "succeeded",
                    "approval_decision": "do_not_approve",
                    "blocking_issues": [{"description": "real objection"}],
                },
            )
            self.assertIsNone(op.swr_decision_transport_error("decision.json", "Codex reviewer decision"))

    def test_transport_failure_class_maps_to_control_plane_budget(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            row = {"failure_class": "swr_review_transport_failure", "slice": "S05"}
            self.assertEqual(op.repair_budget_scope(row), "autokeel_control_plane")

    def test_explicit_repair_scope_field_wins(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            row = {
                "failure_class": "audit_failure",
                "slice": "S05",
                "repair_scope": "autokeel_control_plane",
                "description": "swr supervisor review did not satisfy the fail-closed review-bundle contract.",
            }
            self.assertEqual(op.repair_budget_scope(row), "autokeel_control_plane")


class SemanticRejectionEscalationTests(unittest.TestCase):
    def _manifest(self, root: Path) -> Path:
        import hashlib

        run_dir = root / ".local/autokeel/swr/runs/test-run"
        stage_dir = run_dir / "stages/01_source_authority_map"
        stage_dir.mkdir(parents=True)
        markdown = stage_dir / "response.final.md"
        response_json = stage_dir / "response.final.json"
        markdown.write_text("artifact\n", encoding="utf-8")
        response_json.write_text("{}", encoding="utf-8")
        manifest = run_dir / "run_manifest.json"
        write_json_atomic(
            manifest,
            {
                "run_id": "run_test",
                "run_dir": str(run_dir.relative_to(root)),
                "status": "waiting_for_review",
                "current_stage_id": "source_authority_map",
                "stages": [
                    {
                        "stage_id": "source_authority_map",
                        "stage_number": 1,
                        "status": "waiting_for_review",
                        "response_status": "completed",
                        "stage_dir": str(stage_dir.relative_to(root)),
                        "response_markdown_path": str(markdown.relative_to(root)),
                        "response_markdown_sha256": hashlib.sha256(markdown.read_bytes()).hexdigest(),
                        "response_json_path": str(response_json.relative_to(root)),
                        "response_json_sha256": hashlib.sha256(response_json.read_bytes()).hexdigest(),
                    },
                    {"stage_id": "repo_grounding", "stage_number": 2, "status": "prepared"},
                ],
            },
        )
        return manifest

    def test_semantic_rejection_detection(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            rejection = root / "rejection.json"
            write_json_atomic(
                rejection,
                {"status": "succeeded", "approval_decision": "do_not_approve", "blocking_issues": [{"description": "stale scope"}]},
            )
            approval = root / "approval.json"
            write_json_atomic(approval, {"status": "succeeded", "approval_decision": "approve", "blocking_issues": []})
            transport = root / "transport.json"
            write_json_atomic(transport, {"status": "malformed_output", "approval_decision": "blocked"})
            self.assertTrue(op.swr_decision_semantic_rejection("rejection.json"))
            self.assertFalse(op.swr_decision_semantic_rejection("approval.json"))
            self.assertFalse(op.swr_decision_semantic_rejection("transport.json"))

    def test_semantic_rejection_escalates_to_stage_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            manifest = self._manifest(root)
            slice_ = {"id": "S05"}
            plan = op.plan_swr_review_repair(slice_, manifest, "reviewers rejected", semantic_rejection=True)
            self.assertIsNotNone(plan)
            self.assertEqual(plan["repair_action"], "rerun_single_stage")
            self.assertIn("cannot converge", plan["rationale"])

    def test_shape_drift_still_plans_review_lane_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            manifest = self._manifest(root)
            slice_ = {"id": "S05"}
            plan = op.plan_swr_review_repair(slice_, manifest, "record shape drift", semantic_rejection=False)
            self.assertIsNotNone(plan)
            self.assertEqual(plan["repair_action"], "rerun_review_lane")


class StaleSidecarTests(unittest.TestCase):
    def test_marker_match_does_not_admit_dir_with_sidecars(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            review_dir = root / ".local/autokeel/swr/review_lane/S05-test-cycle"
            review_dir.mkdir(parents=True)
            (review_dir / ".autokeel_review_cycle_id").write_text("cycle_a\n", encoding="utf-8")
            (review_dir / "review_decision.json").write_text("{}", encoding="utf-8")
            result = op.assert_fresh_review_repair_dir(review_dir, "cycle_a")
            self.assertFalse(result.ok)
            self.assertIn("prior sidecars", result.stderr)

    def test_fresh_cycle_suffixes_past_contaminated_dirs(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            slice_ = {"id": "S05", "swr_review_repair": {"created_at": "2026-06-10T10:00:00-04:00"}}
            repair_plan = {"run_id": "run_test", "created_at": "2026-06-10T10:00:00-04:00"}
            base_id, base_dir = op.fresh_swr_repair_cycle(slice_, repair_plan, "source_authority_map")
            base_dir.mkdir(parents=True)
            (base_dir / "review_decision.json").write_text("{}", encoding="utf-8")
            next_id, next_dir = op.fresh_swr_repair_cycle(slice_, repair_plan, "source_authority_map")
            self.assertNotEqual(base_id, next_id)
            self.assertTrue(next_id.endswith("_r2"))
            self.assertNotEqual(base_dir, next_dir)


class CheckpointFreshnessTests(unittest.TestCase):
    def test_stale_checkpoint_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            checkpoint = root / "docs/evidence/s05-autokeel-stability-checkpoint.json"
            stale = (datetime.now().astimezone() - timedelta(hours=200)).isoformat(timespec="seconds")
            write_json_atomic(checkpoint, {"status": "ok", "slice": "S05", "created_at": stale})
            error = op.stability_checkpoint_error("S05")
            self.assertIsNotNone(error)
            self.assertIn("stale", error)

    def test_fresh_passing_checkpoint_is_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            checkpoint = root / "docs/evidence/s05-autokeel-stability-checkpoint.json"
            write_json_atomic(
                checkpoint,
                {"status": "ok", "slice": "S05", "created_at": datetime.now().astimezone().isoformat(timespec="seconds")},
            )
            self.assertIsNone(op.stability_checkpoint_error("S05"))

    def test_missing_and_failing_checkpoints_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            self.assertIn("missing", op.stability_checkpoint_error("S05"))
            checkpoint = root / "docs/evidence/s05-autokeel-stability-checkpoint.json"
            write_json_atomic(
                checkpoint,
                {"status": "error", "slice": "S05", "created_at": datetime.now().astimezone().isoformat(timespec="seconds")},
            )
            self.assertIn("not ok", op.stability_checkpoint_error("S05"))


class FreshLaunchGuardTests(unittest.TestCase):
    def test_fresh_launch_blocked_after_prior_swr_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=False)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S05")
            op.log_event("swr_review_repair_planned", {"repair_stage_id": "source_authority_map"}, slice_id="S05")
            result = op.assert_fresh_swr_launch_sanctioned(slice_)
            self.assertFalse(result.ok)
            self.assertEqual(result.exit_code, 39)
            self.assertIn("abandonment", result.stderr)

    def test_fresh_launch_allowed_with_recorded_abandonment(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=False)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S05")
            op.log_event("swr_review_repair_planned", {"repair_stage_id": "source_authority_map"}, slice_id="S05")
            write_json_atomic(
                root / "docs/evidence/s05-swr-run-abandonment-20260610t100000-0400.json",
                {
                    "schema_version": "autokeel.swr_run_abandonment.v1",
                    "slice": "S05",
                    "abandoned_run_id": "run_x",
                    "reason": "test",
                    "authorizes_fresh_launch": True,
                    "consumed_at": None,
                },
            )
            result = op.assert_fresh_swr_launch_sanctioned(slice_)
            self.assertTrue(result.ok, result.stderr)
            payload = json.loads(
                (root / "docs/evidence/s05-swr-run-abandonment-20260610t100000-0400.json").read_text(encoding="utf-8")
            )
            self.assertIsNotNone(payload.get("consumed_at"), "abandonment decision must be single-use")

    def test_no_prior_history_means_no_guard(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=False)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S05")
            result = op.assert_fresh_swr_launch_sanctioned(slice_)
            self.assertTrue(result.ok, result.stderr)


class StateDigestTests(unittest.TestCase):
    def test_out_of_band_slice_edit_is_detected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=False)
            update_state_digest_sidecar(root)
            self.assertTrue(op.verify_state_digest().ok)

            # Simulate the June-1 incident shape: hand-edit slices.json with no
            # sanctioned writer involved.
            slices_path = root / "ops/autonomy/slices.json"
            slices = json.loads(slices_path.read_text(encoding="utf-8"))
            slices[0]["status"] = "complete"
            slices_path.write_text(json.dumps(slices, indent=2, sort_keys=True) + "\n", encoding="utf-8")

            result = op.verify_state_digest()
            self.assertFalse(result.ok)
            self.assertEqual(result.exit_code, 39)
            self.assertIn("out-of-band", result.stderr)

    def test_sanctioned_writers_keep_digest_in_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=False)
            update_state_digest_sidecar(root)
            op.mark_slice_status("S01", "waiting_for_playbook")
            op.record_failure("S01", "test_failure", "low", "test", "test", None)
            self.assertTrue(op.verify_state_digest().ok)

    def test_digest_matches_computed_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            update_state_digest_sidecar(root)
            sidecar = json.loads((root / "ops/autonomy/state_digest.json").read_text(encoding="utf-8"))
            self.assertEqual(sidecar["digests"], compute_state_digest(root))


class KernelPinTests(unittest.TestCase):
    def _fixture_with_kernel(self, root: Path) -> Path:
        copy_autonomy_fixture(root)
        fake_keel = root / "fake-keel"
        kernel = fake_keel / "tools/staged-workflow-runner"
        pack = kernel / "automation/task_packs/gstack_design_to_po_playbook"
        (pack / "workflows").mkdir(parents=True)
        (pack / "workflows/gstack_design_to_po_playbook.workflow.json").write_text("{}", encoding="utf-8")
        policy_path = root / "ops/autonomy/policy.yaml"
        policy = policy_path.read_text(encoding="utf-8")
        policy = policy.replace("keel_root: /Users/aeziz-local/keel", f"keel_root: {fake_keel}")
        policy_path.write_text(policy, encoding="utf-8")
        return kernel

    def _git(self, cwd: Path, *args: str) -> None:
        subprocess.run(
            ["git", *args],
            cwd=cwd,
            check=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

    def test_dirty_kernel_refuses_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kernel = self._fixture_with_kernel(root)
            self._git(kernel, "init")
            self._git(kernel, "config", "user.email", "tests@example.com")
            self._git(kernel, "config", "user.name", "Tests")
            self._git(kernel, "add", "-A")
            self._git(kernel, "commit", "-m", "seed")
            (kernel / "untracked_change.txt").write_text("dirty\n", encoding="utf-8")
            op = AutoKeel(root=root, dry_run=True)
            result = op.materialize_swr_task_pack()
            self.assertFalse(result.ok)
            self.assertIn("dirty", result.stderr)

    def test_unpinned_kernel_refuses_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            self._fixture_with_kernel(root)
            op = AutoKeel(root=root, dry_run=True)
            result = op.materialize_swr_task_pack()
            self.assertFalse(result.ok)
            self.assertIn("unpinned", result.stderr)

    def test_clean_kernel_pins_and_materializes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            kernel = self._fixture_with_kernel(root)
            self._git(kernel, "init")
            self._git(kernel, "config", "user.email", "tests@example.com")
            self._git(kernel, "config", "user.name", "Tests")
            self._git(kernel, "add", "-A")
            self._git(kernel, "commit", "-m", "seed")
            op = AutoKeel(root=root, dry_run=False)
            result = op.materialize_swr_task_pack()
            self.assertTrue(result.ok, result.stderr)
            pin = json.loads(
                (root / "automation/task_packs/gstack_design_to_po_playbook/kernel_pin.json").read_text(encoding="utf-8")
            )
            self.assertTrue(pin.get("kernel_commit"))
            self.assertFalse(pin.get("kernel_dirty"))
            self.assertTrue(pin.get("task_pack_content_sha256"))


if __name__ == "__main__":
    unittest.main()


class V2ScopeNegationCalibrationTests(unittest.TestCase):
    def test_exclusion_list_bullets_inherit_negation_lead_in(self) -> None:
        import re

        from scripts.validate_playbook_autonomous import allowed_v2_scope_context

        text = (
            "s05 explicitly does not own:\n\n"
            "- counterfactual generation.\n"
            "- causal, medical, prospective, recommendation, or customer-facing claims.\n"
        )
        match = re.search(r"\bprospective\b", text)
        self.assertTrue(allowed_v2_scope_context(text, match))

    def test_bare_prose_usage_stays_flagged(self) -> None:
        import re

        from scripts.validate_playbook_autonomous import allowed_v2_scope_context

        text = "the model will support prospective analysis next quarter.\n"
        match = re.search(r"\bprospective\b", text)
        self.assertFalse(allowed_v2_scope_context(text, match))

    def test_positive_lead_in_bullets_stay_flagged(self) -> None:
        import re

        from scripts.validate_playbook_autonomous import allowed_v2_scope_context

        text = "s05 will deliver:\n\n- prospective dashboards.\n"
        match = re.search(r"\bprospective\b", text)
        self.assertFalse(allowed_v2_scope_context(text, match))


class ExternalDependencyScopeTests(unittest.TestCase):
    def test_external_provider_rows_do_not_consume_repair_budgets(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            row = {
                "failure_class": "provider_auth_failure",
                "failure_origin": "external_provider",
                "slice": "S05",
                "description": "Codex CLI usage limit exhausted",
            }
            self.assertEqual(op.repair_budget_scope(row), "external_dependency")

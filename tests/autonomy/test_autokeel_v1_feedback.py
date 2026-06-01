from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from ops.autonomy.autokeel import AutoKeel, CommandResult, CommandRunner, file_sha256, write_json_atomic
from scripts.check_no_tracked_data import check_no_tracked_data
from scripts.evaluate_tripwires import evaluate_tripwires
from scripts.evidence.pyeight_smoke import collect as collect_pyeight
from scripts.validate_playbook_autonomous import validate_playbook
from scripts.validate_swr_review_bundle import validate_swr_review_bundle
from scripts.verify_s02_readiness import verify_s02_readiness
from scripts.verify_slice import verify_slice


ROOT = Path(__file__).resolve().parents[2]


def copy_fixture(dst: Path) -> None:
    shutil.copytree(ROOT / "ops", dst / "ops")
    shutil.copytree(ROOT / "scripts", dst / "scripts")
    (dst / ".gitignore").write_text("data/\nprivate/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n", encoding="utf-8")
    for rel in ("ops/autonomy/failures/archived_playbooks", "ops/autonomy/failures/archived_autoplans", "ops/autonomy/heartbeats"):
        shutil.rmtree(dst / rel, ignore_errors=True)
    slices_path = dst / "ops/autonomy/slices.json"
    slices = json.loads(slices_path.read_text(encoding="utf-8"))
    for item in slices:
        item["status"] = "pending"
        item.pop("retry_count", None)
        item.pop("failure_path", None)
        item.pop("run_id", None)
        item.pop("reason", None)
        item.pop("stopped_run_id", None)
        item.pop("stopped_swr_run_id", None)
        item.pop("stopped_swr_response_id", None)
        item.pop("swr_run_id", None)
        item.pop("swr_run_manifest", None)
        item.pop("swr_review_repair", None)
        item.pop("swr_validation_repair", None)
    write_json_atomic(slices_path, slices)
    write_json_atomic(
        dst / "ops/autonomy/autonomy_state.json",
        {
            "active_run": None,
            "active_swr_run": None,
            "completed_slices": [],
            "current_slice": None,
            "last_event_id": 0,
            "v1_complete": False,
        },
    )
    (dst / "ops/autonomy/failure_ledger.jsonl").write_text("", encoding="utf-8")
    (dst / "ops/autonomy/events.jsonl").write_text("", encoding="utf-8")


def prepare_s02_swr_inputs(root: Path) -> Path:
    fake_keel = root / "fake-keel"
    task_pack_source = fake_keel / "tools/staged-workflow-runner/automation/task_packs/gstack_design_to_po_playbook"
    supervisor_pack_source = fake_keel / "tools/staged-workflow-runner/automation/task_packs/responses_runner_v2_supervisor_internal"
    (task_pack_source / "workflows").mkdir(parents=True)
    (task_pack_source / "workflows/gstack_design_to_po_playbook.workflow.json").write_text("{}", encoding="utf-8")
    (supervisor_pack_source / "commands").mkdir(parents=True)
    (supervisor_pack_source / "prompts").mkdir(parents=True)
    (supervisor_pack_source / "commands/operator_codex.command.json").write_text("{}", encoding="utf-8")
    (supervisor_pack_source / "commands/codex_review_agent.command.json").write_text("{}", encoding="utf-8")
    (supervisor_pack_source / "commands/claude_review_agent.command.json").write_text("{}", encoding="utf-8")
    (supervisor_pack_source / "prompts/operator_codex.md").write_text("operator\n", encoding="utf-8")
    (supervisor_pack_source / "prompts/codex_review.md").write_text("codex\n", encoding="utf-8")
    (supervisor_pack_source / "prompts/claude_review.md").write_text("claude\n", encoding="utf-8")
    (fake_keel / "bin").mkdir(parents=True)

    policy = (root / "ops/autonomy/policy.yaml").read_text(encoding="utf-8")
    policy = policy.replace("keel_root: /Users/aeziz-local/keel", f"keel_root: {fake_keel}")
    (root / "ops/autonomy/policy.yaml").write_text(policy, encoding="utf-8")

    (root / "docs/gstack").mkdir(parents=True, exist_ok=True)
    (root / "docs/briefs").mkdir(parents=True, exist_ok=True)
    (root / "docs/playbooks").mkdir(parents=True, exist_ok=True)
    (root / "docs/evidence").mkdir(parents=True, exist_ok=True)
    (root / "docs/gstack/health-data-hub-office-hours.md").write_text("S02 design", encoding="utf-8")
    (root / "docs/briefs/s02-mood-api.autonomous-brief.md").write_text("S02 brief", encoding="utf-8")
    (root / "docs/gstack/s02-mood-api-autoplan.md").write_text(
        "# S02 autoplan\n\n"
        "Deliverables and verification are listed below.\n\n"
        "Manual gates are forbidden; use autonomous_gate_review evidence instead.\n\n"
        "## Implementation Tasks\n\n"
        "- [ ] Implement the Mood API loop.\n"
        "  Files: `src/api/mood.py`; `tests/test_api_security.py`\n"
        "  Verify: `python -m pytest tests/test_api_security.py -q`\n",
        encoding="utf-8",
    )
    return fake_keel


def swr_review_decision(
    *,
    actor_role: str,
    review_kind: str,
    approval_decision: str,
    run_id: str = "run_20260527_test_waiting",
    stage_id: str = "source_authority_map",
    status: str = "succeeded",
    next_action: str | None = None,
    blocking_issues: list[dict[str, object]] | None = None,
    validation_errors: list[str] | None = None,
) -> dict[str, object]:
    if next_action is None:
        if review_kind == "consolidation":
            next_action = "proceed_to_operator_acceptance"
        elif review_kind == "operator_acceptance" and approval_decision == "approve":
            next_action = "create_review_bundle"
        elif approval_decision in {"blocked", "do_not_approve"}:
            next_action = "blocked"
        else:
            next_action = "proceed_to_consolidation"
    return {
        "schema_version": "responses_runner_v2.review_decision.v1",
        "decision_id": f"{actor_role}_{review_kind}_{stage_id}",
        "created_at": "2026-05-27T00:00:00-04:00",
        "supervisor_session_id": "autokeel-s02-run_20260527_test_waiting",
        "workflow_id": "gstack_design_to_po_playbook",
        "run_id": run_id,
        "stage_id": stage_id,
        "review_cycle_id": f"{stage_id}_stage_review",
        "review_kind": review_kind,
        "actor_role": actor_role,
        "agent_command_id": None,
        "status": status,
        "approval_decision": approval_decision,
        "summary": "test decision",
        "reviewed_artifacts": [],
        "missing_artifacts": [],
        "blocking_issues": blocking_issues or [],
        "non_blocking_improvements": [],
        "recommendations": [],
        "unsupported_claims": [],
        "evidence": [],
        "command": None,
        "read_only_check": None,
        "validation_errors": validation_errors or [],
        "next_action": next_action,
    }


def write_completed_swr_manifest(root: Path, text: str) -> Path:
    run_dir = root / ".local/autokeel/swr/runs/test-run"
    stage_dir = run_dir / "stages/05_final_markdown_playbook"
    stage_dir.mkdir(parents=True, exist_ok=True)
    response_path = stage_dir / "response.final.json"
    response_path.write_text(
        json.dumps(
            {
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": text}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "status": "completed",
        "stages": [
            {
                "stage_id": "final_markdown_playbook",
                "status": "completed",
                "response_json_path": str(response_path.relative_to(root)),
            }
        ],
    }
    manifest_path = run_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def write_active_swr_manifest(root: Path) -> Path:
    run_dir = root / ".local/autokeel/swr/runs/2026-05-27_193037_autokeel-s02-test_gstack_design_to_po_playbook"
    stage_dir = run_dir / "stages/01_source_authority_map"
    stage_dir.mkdir(parents=True, exist_ok=True)
    checkpoint = stage_dir / "stage_checkpoint.json"
    checkpoint.write_text(
        json.dumps(
            {
                "status": "in_progress",
                "response_id": "resp_test_s02_active",
                "terminal": False,
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "run_id": "run_20260527_test_active",
        "run_name": "autokeel-s02-test",
        "run_dir": str(run_dir.relative_to(root)),
        "workflow_id": "gstack_design_to_po_playbook",
        "status": "running",
        "current_stage_id": "source_authority_map",
        "operator_overrides": {
            "primary_job_inputs": [
                str(root / "docs/gstack/health-data-hub-office-hours.md"),
                str(root / "docs/gstack/s02-mood-api-autoplan.md"),
                str(root / "docs/briefs/s02-mood-api.autonomous-brief.md"),
            ],
            "reference_context": [],
            "review_bundles": [],
            "skip_token_count": True,
        },
        "stages": [
            {
                "stage_id": "source_authority_map",
                "status": "in_progress",
                "response_id": "resp_test_s02_active",
                "stage_dir": str(stage_dir.relative_to(root)),
                "checkpoint_path": str(checkpoint.relative_to(root)),
            }
        ],
    }
    manifest_path = run_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def write_created_swr_manifest(root: Path) -> Path:
    run_dir = root / ".local/autokeel/swr/runs/2026-05-27_192819_autokeel-s02-test_gstack_design_to_po_playbook"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": "run_20260527_test_created",
        "run_name": "autokeel-s02-test-created",
        "run_dir": str(run_dir.relative_to(root)),
        "workflow_id": "gstack_design_to_po_playbook",
        "status": "created",
        "current_stage_id": None,
        "operator_overrides": {
            "primary_job_inputs": [
                str(root / "docs/gstack/health-data-hub-office-hours.md"),
                str(root / "docs/gstack/s02-mood-api-autoplan.md"),
                str(root / "docs/briefs/s02-mood-api.autonomous-brief.md"),
            ],
            "reference_context": [],
            "review_bundles": [],
            "skip_token_count": True,
        },
        "stages": [
            {"stage_id": "source_authority_map", "stage_number": 1, "status": "prepared"},
            {"stage_id": "repo_grounding", "stage_number": 2, "status": "prepared"},
            {"stage_id": "execution_row_draft", "stage_number": 3, "status": "prepared"},
            {"stage_id": "gate_and_contract_review", "stage_number": 4, "status": "prepared"},
            {"stage_id": "final_markdown_playbook", "stage_number": 5, "status": "prepared"},
        ],
    }
    manifest_path = run_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def write_completed_swr_manifest_with_review_history(root: Path) -> Path:
    run_dir = root / ".local/autokeel/swr/runs/2026-05-27_193037_autokeel-s02-test_gstack_design_to_po_playbook"
    run_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "run_id": "run_20260527_test_completed",
        "run_name": "autokeel-s02-test-completed",
        "run_dir": str(run_dir.relative_to(root)),
        "workflow_id": "gstack_design_to_po_playbook",
        "status": "completed",
        "current_stage_id": "final_markdown_playbook",
        "stage_order": [
            "source_authority_map",
            "repo_grounding",
            "execution_row_draft",
            "gate_and_contract_review",
            "final_markdown_playbook",
        ],
        "stages": [
            {"stage_id": "source_authority_map", "stage_number": 1, "status": "waiting_for_review"},
            {"stage_id": "repo_grounding", "stage_number": 2, "status": "waiting_for_review"},
            {"stage_id": "execution_row_draft", "stage_number": 3, "status": "waiting_for_review"},
            {"stage_id": "gate_and_contract_review", "stage_number": 4, "status": "waiting_for_review"},
            {"stage_id": "final_markdown_playbook", "stage_number": 5, "status": "completed"},
        ],
    }
    manifest_path = run_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def write_completed_swr_manifest_with_stage_contract_drift(root: Path) -> Path:
    run_dir = root / ".local/autokeel/swr/runs/2026-05-27_193037_autokeel-s02-test_gstack_design_to_po_playbook"
    old_table = (
        "| step_id | phase | action | why_now | owner_type | prerequisites | repo_surfaces | deliverable | "
        "exit_criteria | allowed_write_roots | requires_red_green |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|\n"
        "| 01 | mood api | implement | now | ai | none | src/api/mood.py | src/api/mood.py | tests pass | src/api | true |\n"
    )
    stage_order = [
        "source_authority_map",
        "repo_grounding",
        "execution_row_draft",
        "gate_and_contract_review",
        "final_markdown_playbook",
    ]

    stages = []
    for number, stage_id in enumerate(stage_order, start=1):
        stage_dir = run_dir / f"stages/{number:02d}_{stage_id}"
        stage_dir.mkdir(parents=True, exist_ok=True)
        md = stage_dir / "response.final.md"
        js = stage_dir / "response.final.json"
        text = f"# {stage_id}\n\n{old_table}"
        md.write_text(text, encoding="utf-8")
        js.write_text(
            json.dumps({"output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}]}),
            encoding="utf-8",
        )
        stage = {
            "stage_id": stage_id,
            "stage_number": number,
            "gate": "terminal" if stage_id == "final_markdown_playbook" else "review_required",
            "stage_dir": str(stage_dir.relative_to(root)),
            "status": "completed" if stage_id == "final_markdown_playbook" else "waiting_for_review",
            "checkpoint_path": str((stage_dir / "stage_checkpoint.json").relative_to(root)),
            "response_json_path": str(js.relative_to(root)),
            "response_markdown_path": str(md.relative_to(root)),
        }
        if stage_id in {"execution_row_draft", "gate_and_contract_review"}:
            review_dir = root / ".local/autokeel/swr/review_lane" / f"S02-run_20260527_test_completed-{stage_id}"
            review_dir.mkdir(parents=True, exist_ok=True)
            bundle = review_dir / f"{stage_id}.review_bundle.json"
            bundle.write_text(json.dumps({"review_status": "approved"}), encoding="utf-8")
            stage["review_approved"] = True
            stage["review_bundle_path"] = str(bundle.relative_to(root))
        stages.append(stage)

    manifest = {
        "run_id": "run_20260527_test_completed",
        "run_name": "autokeel-s02-test-completed",
        "run_dir": str(run_dir.relative_to(root)),
        "workflow_id": "gstack_design_to_po_playbook",
        "status": "completed",
        "current_stage_id": "final_markdown_playbook",
        "stage_order": stage_order,
        "operator_overrides": {
            "primary_job_inputs": [
                str(root / "docs/gstack/health-data-hub-office-hours.md"),
                str(root / "docs/gstack/s02-mood-api-autoplan.md"),
                str(root / "docs/briefs/s02-mood-api.autonomous-brief.md"),
            ],
            "reference_context": [],
            "review_bundles": [],
            "skip_token_count": True,
        },
        "stages": stages,
    }
    manifest_path = run_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def write_waiting_swr_manifest(root: Path) -> Path:
    run_dir = root / ".local/autokeel/swr/runs/2026-05-27_193037_autokeel-s02-test_gstack_design_to_po_playbook"
    stage_dir = run_dir / "stages/01_source_authority_map"
    stage_dir.mkdir(parents=True, exist_ok=True)
    response_md = stage_dir / "response.final.md"
    response_json = stage_dir / "response.final.json"
    checkpoint = stage_dir / "stage_checkpoint.json"
    response_md.write_text("# Source Authority Map\n\nComplete artifact.\n", encoding="utf-8")
    response_json.write_text(json.dumps({"output": [{"type": "message", "content": []}]}), encoding="utf-8")
    input_manifest = stage_dir / "input_manifest.json"
    input_manifest.write_text(json.dumps({"stage_id": "source_authority_map", "input": "test"}), encoding="utf-8")
    checkpoint.write_text(
        json.dumps(
            {
                "status": "waiting_for_review",
                "stage_id": "source_authority_map",
                "run_id": "run_20260527_test_waiting",
                "terminal": True,
                "review_checkpoint_required": True,
                "artifacts": {
                    "response_final_markdown_path": str(response_md.relative_to(root)),
                    "response_final_json_path": str(response_json.relative_to(root)),
                },
            }
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": "responses_runner_v2.run_manifest.v1",
        "run_id": "run_20260527_test_waiting",
        "run_name": "autokeel-s02-test",
        "run_dir": str(run_dir.relative_to(root)),
        "workflow_id": "gstack_design_to_po_playbook",
        "status": "waiting_for_review",
        "operator_overrides": {
            "primary_job_inputs": [
                str(root / "docs/gstack/health-data-hub-office-hours.md"),
                str(root / "docs/gstack/s02-mood-api-autoplan.md"),
                str(root / "docs/briefs/s02-mood-api.autonomous-brief.md"),
            ],
            "reference_context": [],
            "review_bundles": [],
            "skip_token_count": True,
        },
        "stage_order": [
            "source_authority_map",
            "repo_grounding",
            "execution_row_draft",
            "gate_and_contract_review",
            "final_markdown_playbook",
        ],
        "current_stage_id": "source_authority_map",
        "stages": [
            {
                "stage_id": "source_authority_map",
                "stage_number": 1,
                "gate": "review_required",
                "status": "waiting_for_review",
                "response_status": "completed",
                "response_id": "resp_source_authority_map",
                "stage_dir": str(stage_dir.relative_to(root)),
                "checkpoint_path": str(checkpoint.relative_to(root)),
                "input_manifest_json_path": str(input_manifest.relative_to(root)),
                "response_markdown_path": str(response_md.relative_to(root)),
                "response_json_path": str(response_json.relative_to(root)),
                "response_markdown_sha256": file_sha256(response_md),
                "response_json_sha256": file_sha256(response_json),
            },
            {"stage_id": "repo_grounding", "stage_number": 2, "gate": "review_required", "status": "prepared"},
        ],
    }
    manifest_path = run_dir / "run_manifest.json"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    return manifest_path


def write_existing_swr_review_bundle(root: Path, manifest_path: Path) -> Path:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    stage = payload["stages"][0]
    review_dir = root / ".local/autokeel/swr/review_lane/S02-run_20260527_test_waiting-source_authority_map"
    review_dir.mkdir(parents=True, exist_ok=True)
    operator_review = review_dir / "operator/operator.json"
    codex_review = review_dir / "agents/codex.json"
    claude_review = review_dir / "agents/claude.json"
    consolidated_review = review_dir / "consolidated_review.json"
    operator_acceptance = review_dir / "operator_acceptance.json"
    operator_review.parent.mkdir(parents=True, exist_ok=True)
    codex_review.parent.mkdir(parents=True, exist_ok=True)
    operator_review.write_text(
        json.dumps(swr_review_decision(actor_role="operator_codex", review_kind="stage_output", approval_decision="approve")),
        encoding="utf-8",
    )
    codex_review.write_text(
        json.dumps(swr_review_decision(actor_role="codex_review_agent", review_kind="stage_output", approval_decision="approve")),
        encoding="utf-8",
    )
    claude_review.write_text(
        json.dumps(swr_review_decision(actor_role="claude_review_agent", review_kind="stage_output", approval_decision="approve")),
        encoding="utf-8",
    )
    consolidated_review.write_text(
        json.dumps(swr_review_decision(actor_role="consolidation_pass", review_kind="consolidation", approval_decision="approve_with_conditions")),
        encoding="utf-8",
    )
    operator_acceptance.write_text(
        json.dumps(swr_review_decision(actor_role="operator_codex", review_kind="operator_acceptance", approval_decision="approve")),
        encoding="utf-8",
    )
    reviewer_notes = review_dir / "reviewer_notes.md"
    records = {
        "operator_review": str(operator_review.relative_to(root)),
        "codex_review": str(codex_review.relative_to(root)),
        "claude_review": str(claude_review.relative_to(root)),
        "consolidated_review": str(consolidated_review.relative_to(root)),
        "operator_acceptance": str(operator_acceptance.relative_to(root)),
    }
    reviewer_notes.write_text(
        "# Reviewer Notes\n\n"
        + "\n".join(f"- {key}: `{value}`" for key, value in records.items())
        + "\n",
        encoding="utf-8",
    )
    bundle_path = review_dir / "source_authority_map.review_bundle.json"
    bundle = {
        "schema_version": "responses_runner_v2.review_bundle.v1",
        "slice": "S02",
        "run_id": payload["run_id"],
        "stage_id": "source_authority_map",
        "response_id": stage["response_id"],
        "input_artifact": stage["input_manifest_json_path"],
        "input_sha256": file_sha256(root / stage["input_manifest_json_path"]),
        "output_artifact": stage["response_json_path"],
        "output_sha256": file_sha256(root / stage["response_json_path"]),
        "reviewer_results": [
            {"reviewer": "operator_codex", "verdict": "pass"},
            {"reviewer": "codex_review_agent", "verdict": "pass"},
            {"reviewer": "claude_review_agent", "verdict": "pass"},
        ],
        "consolidated_verdict": "pass",
        "accepted_at": "2026-05-27T00:00:00-04:00",
        "workflow_id": payload["workflow_id"],
        "source_stage_id": "source_authority_map",
        "source_run_id": payload["run_id"],
        "review_status": "approved",
        "primary_artifact_markdown": stage["response_markdown_path"],
        "response_artifact_json": stage["response_json_path"],
        "reviewer_notes": str(reviewer_notes.relative_to(root)),
        "acceptance_record": str(operator_acceptance.relative_to(root)),
        "review_decision_records": records,
        "artifact_hashes": {
            "primary_artifact_markdown_sha256": file_sha256(root / stage["response_markdown_path"]),
            "response_artifact_json_sha256": file_sha256(root / stage["response_json_path"]),
            "reviewer_notes_sha256": file_sha256(reviewer_notes),
        },
    }
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")
    return bundle_path


class CompletedSwrRunner:
    def __init__(self, root: Path, text: str = "# S02 Mood API Playbook\n\nmarkdown_playbook_v1\n"):
        self.root = root
        self.text = text
        self.calls: list[list[str]] = []

    def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
        self.calls.append(list(argv))
        manifest_path = write_completed_swr_manifest(self.root, self.text)
        return CommandResult(list(argv), 0, str(manifest_path.relative_to(self.root)) + "\n", "")


class AutoKeelV1FeedbackTests(unittest.TestCase):
    def setUp(self) -> None:
        self._openai_env = mock.patch.dict(os.environ, {"OPENAI_API_KEY": "test-openai-key"}, clear=False)
        self._openai_env.start()

    def tearDown(self) -> None:
        self._openai_env.stop()

    def test_command_runner_timeout_returns_124(self) -> None:
        runner = CommandRunner(ROOT, {"manual_gates": {"forbidden_commands": []}}, timeout=0.01)
        result = runner.run(["python", "-c", "import time; time.sleep(1)"])
        self.assertEqual(result.exit_code, 124)
        self.assertIn("command timed out", result.stderr)

    def test_replan_archives_playbook_and_clears_active_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            playbook = root / "docs/playbooks/s01-warehouse.playbook.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text("old playbook", encoding="utf-8")
            autoplan = root / "docs/gstack/s01-warehouse-autoplan.md"
            autoplan.parent.mkdir(parents=True)
            autoplan.write_text(
                "# S01 autoplan\n\n"
                "Deliverables and verification are listed below.\n\n"
                "Manual gates are forbidden.\n\n"
                "## Implementation Tasks\n\n"
                "- [ ] Add schema.\n"
                "  Files: `src/db/schema.sql`; `tests/warehouse/test_schema.py`\n"
                "  Verify: `python -m pytest tests/warehouse/test_schema.py -q`\n",
                encoding="utf-8",
            )
            (root / "docs/gstack/health-data-hub-office-hours.md").write_text("design", encoding="utf-8")
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            state["active_run"] = {"slice": "S01", "run_id": "run_old", "started_at": "2026-05-23T00:00:00-04:00"}
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", state)

            class CompileRunner:
                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    return CommandResult(list(argv), 0, "compiled", "")

            op = AutoKeel(root=root, dry_run=False)
            op.runner = CompileRunner()
            op.mark_slice_status("S01", "replan_required")
            slices = op.load_slices()
            result = op.ensure_playbook(next(item for item in slices if item["id"] == "S01"))

            self.assertTrue(result.ok)
            self.assertIsNone(op.load_state().get("active_run"))
            archived = list((root / "ops/autonomy/failures/archived_playbooks").glob("S01-*.md"))
            self.assertEqual(len(archived), 1)

    def test_evidence_ready_resumes_po_with_evidence_dir(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            evidence = root / "private/evidence/S03/request"
            evidence.mkdir(parents=True)
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            state["active_run"] = {"slice": "S03", "run_id": "run_ext", "evidence_request": "private/evidence/S03/request"}
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", state)
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S03":
                    item["status"] = "evidence_ready"
                    item["evidence_request"] = "private/evidence/S03/request"
            write_json_atomic(root / "ops/autonomy/slices.json", slices)

            op = AutoKeel(root=root, dry_run=True)
            result = op.start_or_resume_po(next(item for item in slices if item["id"] == "S03"))
            self.assertTrue(result.ok)
            self.assertIn("dry_run", result.stdout)

    def test_high_risk_swr_missing_lane_decision_blocks_not_downgrades(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S02":
                    item.pop("lane_decision", None)
            write_json_atomic(root / "ops/autonomy/slices.json", slices)
            op = AutoKeel(root=root, dry_run=True)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_lane_decision(slice_)

            self.assertFalse(result.ok)
            self.assertIn("missing lane_decision artifact", result.stderr)
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertEqual(updated["status"], "blocked_compile_inputs")
            self.assertNotIn("lane_decision", updated)
            self.assertIn("missing lane_decision artifact", updated["reason"])
            ledger = (root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn("lane_decision_missing", ledger)
            self.assertNotIn("compile_failure", ledger)

    def test_high_risk_swr_invalid_lane_decision_blocks_with_dedicated_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            invalid = root / "ops/autonomy/decisions/S02-invalid.json"
            invalid.write_text(
                json.dumps(
                    {
                        "created_at": "2026-05-26T00:00:00-04:00",
                        "slice": "S02",
                        "lane": "swr_preferred",
                        "decision": "block",
                        "risk": "high",
                        "review_artifacts": ["docs/reviews/s02-autonomous-security-review.md"],
                        "commands": [{"command": "python scripts/verify_autonomy_preflight.py --json", "exit_code": 1}],
                        "verdict": "fail",
                    }
                ),
                encoding="utf-8",
            )
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S02":
                    item["lane_decision"] = "ops/autonomy/decisions/S02-invalid.json"
            write_json_atomic(root / "ops/autonomy/slices.json", slices)
            op = AutoKeel(root=root, dry_run=True)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_lane_decision(slice_)

            self.assertFalse(result.ok)
            self.assertIn("lane_decision verdict blocks execution", result.stderr)
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertEqual(updated["status"], "blocked_compile_inputs")
            ledger = (root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn("lane_decision_invalid", ledger)

    def test_high_risk_swr_compile_decision_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            decision = root / "ops/autonomy/decisions/S02-compile.json"
            decision.write_text(
                json.dumps(
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
                        "commands": [{"command": "python scripts/verify_autonomy_preflight.py --json", "exit_code": 0}],
                        "verdict": "pass",
                    }
                ),
                encoding="utf-8",
            )
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S02":
                    item["lane_decision"] = "ops/autonomy/decisions/S02-compile.json"
            write_json_atomic(root / "ops/autonomy/slices.json", slices)
            op = AutoKeel(root=root, dry_run=True)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_lane_decision(slice_)

            self.assertFalse(result.ok)
            self.assertIn("must be use_swr", result.stderr)
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertEqual(updated["status"], "blocked_compile_inputs")

    def test_high_risk_swr_valid_lane_decision_allows_swr_precheck(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_lane_decision(slice_)

            self.assertTrue(result.ok, result.stderr)
            self.assertIn("lane decision exists", result.stdout)

    def test_swr_preferred_playbook_generation_routes_through_keel_swr(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            fake_keel = root / "fake-keel"
            task_pack_source = fake_keel / "tools/staged-workflow-runner/automation/task_packs/gstack_design_to_po_playbook"
            task_pack_source.mkdir(parents=True)
            (fake_keel / "bin").mkdir(parents=True)

            policy = (root / "ops/autonomy/policy.yaml").read_text(encoding="utf-8")
            policy = policy.replace("keel_root: /Users/aeziz-local/keel", f"keel_root: {fake_keel}")
            (root / "ops/autonomy/policy.yaml").write_text(policy, encoding="utf-8")

            (root / "docs/gstack").mkdir(parents=True)
            (root / "docs/briefs").mkdir(parents=True)
            (root / "docs/playbooks").mkdir(parents=True)
            (root / "docs/gstack/health-data-hub-office-hours.md").write_text("S02 design", encoding="utf-8")
            (root / "docs/briefs/s02-mood-api.autonomous-brief.md").write_text("S02 brief", encoding="utf-8")
            (root / "docs/gstack/s02-mood-api-autoplan.md").write_text(
                "# S02 autoplan\n\n"
                "Deliverables and verification are listed below.\n\n"
                "Manual gates are forbidden; use autonomous_gate_review evidence instead.\n\n"
                "## Implementation Tasks\n\n"
                "- [ ] Implement the Mood API loop.\n"
                "  Files: `src/api/mood.py`; `tests/test_api_security.py`\n"
                "  Verify: `python -m pytest tests/test_api_security.py -q`\n",
                encoding="utf-8",
            )
            stale_playbook = root / "docs/playbooks/s02-mood-api.playbook.md"
            stale_playbook.write_text("compiler generated stale playbook\n", encoding="utf-8")

            op = AutoKeel(root=root, dry_run=True)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")
            result = op.ensure_playbook(slice_)

            self.assertTrue(result.ok, result.stderr)
            self.assertIn("keel-swr", result.argv[0])
            self.assertIn("run", result.argv)
            self.assertIn("automation/task_packs/gstack_design_to_po_playbook/workflows/gstack_design_to_po_playbook.workflow.json", result.argv)
            self.assertIn("--skip-token-count", result.argv)
            self.assertNotIn("keel-compile", " ".join(result.argv))
            events = (root / "ops/autonomy/events.jsonl").read_text(encoding="utf-8")
            self.assertIn("dry_run_non_swr_playbook_archive_skipped", events)
            self.assertIn("swr_playbook_generation_planned", events)

    def test_swr_task_pack_materializes_under_manifest_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            fake_keel = prepare_s02_swr_inputs(root)
            task_pack_source = fake_keel / "tools/staged-workflow-runner/automation/task_packs/gstack_design_to_po_playbook"
            source_corpus = task_pack_source / "corpus"
            source_corpus.mkdir(parents=True)
            (source_corpus / "markdown_playbook_v1_contract.md").write_text("contract\n", encoding="utf-8")
            source_prompts = fake_keel / "tools/staged-workflow-runner/automation/task_packs/gstack_design_to_po_playbook/prompts"
            source_prompts.mkdir(parents=True)
            (task_pack_source / "shared_instructions.md").write_text("shared\n", encoding="utf-8")
            (source_prompts / "stage3_execution_row_draft.md").write_text("stage3\n", encoding="utf-8")
            (source_prompts / "stage4_gate_and_contract_review.md").write_text("stage4\n", encoding="utf-8")
            (source_prompts / "stage5_final_markdown_playbook.md").write_text("stage5\n", encoding="utf-8")

            op = AutoKeel(root=root, dry_run=False)
            result = op.materialize_swr_task_pack()

            self.assertTrue(result.ok, result.stderr)
            self.assertEqual(result.stdout, "automation/task_packs/gstack_design_to_po_playbook")
            self.assertTrue(
                (
                    root
                    / "automation/task_packs/gstack_design_to_po_playbook/corpus/markdown_playbook_v1_contract.md"
                ).exists()
            )
            contract = (
                root
                / "automation/task_packs/gstack_design_to_po_playbook/corpus/markdown_playbook_v1_contract.md"
            ).read_text(encoding="utf-8")
            stage5_prompt = (
                root
                / "automation/task_packs/gstack_design_to_po_playbook/prompts/stage5_final_markdown_playbook.md"
            ).read_text(encoding="utf-8")
            stage3_prompt = (
                root
                / "automation/task_packs/gstack_design_to_po_playbook/prompts/stage3_execution_row_draft.md"
            ).read_text(encoding="utf-8")
            stage4_prompt = (
                root
                / "automation/task_packs/gstack_design_to_po_playbook/prompts/stage4_gate_and_contract_review.md"
            ).read_text(encoding="utf-8")
            shared = (root / "automation/task_packs/gstack_design_to_po_playbook/shared_instructions.md").read_text(
                encoding="utf-8"
            )
            self.assertIn("required_verification_commands", contract)
            self.assertIn("autonomous_gate_review", contract)
            for text in (stage3_prompt, stage4_prompt, stage5_prompt, shared):
                self.assertIn("required_verification_commands", text)
                self.assertIn("autonomous_gate_review", text)

    def test_swr_missing_openai_key_blocks_as_provider_auth_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.dict(os.environ, {}, clear=True):
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)

            class MissingOpenAIKeyRunner:
                def __init__(self):
                    self.calls: list[list[str]] = []

                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    self.calls.append(list(argv))
                    joined = " ".join(str(part) for part in argv)
                    if "scripts.verify_v1" in joined:
                        return CommandResult(list(argv), 1, '{"status":"error","errors":["incomplete"]}', "")
                    if "scripts.evaluate_tripwires" in joined:
                        return CommandResult(list(argv), 0, '{"status":"ok","errors":[],"warnings":[]}', "")
                    if "keel-swr" in str(argv[0]):
                        return CommandResult(list(argv), 1, "", "OPENAI_API_KEY is not set in the environment and was not found in .env.\n")
                    return CommandResult(list(argv), 0, "", "")

            op = AutoKeel(root=root, dry_run=False)
            runner = MissingOpenAIKeyRunner()
            op.runner = runner

            code = op._run_once_impl(requested_slice="S02", force_slice=True)

            self.assertEqual(code, 26)
            self.assertFalse(any("keel-swr" in call[0] and "run" in call for call in runner.calls))
            self.assertFalse((root / "docs/playbooks/s02-mood-api.playbook.md").exists())
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertEqual(updated["status"], "blocked_external")
            self.assertEqual(updated["reason"], "missing required SWR provider environment")
            ledger = (root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn("provider_auth_failure", ledger)
            self.assertNotIn("compile_failure", ledger)
            evidence_files = list((root / "docs/evidence").glob("s02-mood-api-swr-provider-preflight-*.json"))
            self.assertEqual(len(evidence_files), 1)
            evidence = json.loads(evidence_files[0].read_text(encoding="utf-8"))
            self.assertEqual(evidence["status"], "blocked_external")
            self.assertEqual(evidence["provider"], "openai")
            self.assertEqual(evidence["missing_env"], ["OPENAI_API_KEY"])
            self.assertFalse(evidence["secret_values_logged"])
            events = (root / "ops/autonomy/events.jsonl").read_text(encoding="utf-8")
            self.assertIn("swr_provider_preflight_evidence_recorded", events)

    def test_active_swr_manifest_blocks_relaunch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_active_swr_manifest(root)
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S02":
                    item["failure_path"] = "ops/autonomy/failures/old.md"
                    item["retry_count"] = 2
                    item["stopped_run_id"] = "old-run"
            write_json_atomic(root / "ops/autonomy/slices.json", slices)
            write_json_atomic(
                root / "ops/autonomy/autonomy_state.json",
                {
                    "active_run": None,
                    "active_swr_run": {
                        "slice": "S02",
                        "run_manifest": str(manifest.relative_to(root)),
                        "last_remote_check_at": "2999-01-01T00:00:00+00:00",
                    },
                    "completed_slices": [],
                    "current_slice": "S02",
                    "last_event_id": 0,
                    "v1_complete": False,
                },
            )

            class NoSwrRelaunchRunner:
                def __init__(self):
                    self.calls: list[list[str]] = []

                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    self.calls.append(list(argv))
                    if "keel-swr" in str(argv[0]):
                        return CommandResult(list(argv), 99, "", "SWR must not relaunch")
                    return CommandResult(list(argv), 0, "", "")

            op = AutoKeel(root=root, dry_run=False)
            runner = NoSwrRelaunchRunner()
            op.runner = runner
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_playbook(slice_)

            self.assertEqual(result.exit_code, 31)
            self.assertEqual(result.stdout, str(manifest.relative_to(root)))
            self.assertFalse(any("keel-swr" in call[0] for call in runner.calls))
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["active_swr_run"]["slice"], "S02")
            self.assertEqual(state["active_swr_run"]["run_manifest"], str(manifest.relative_to(root)))
            self.assertEqual(state["active_swr_run"]["response_id"], "resp_test_s02_active")
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertEqual(updated["status"], "waiting_for_playbook")
            self.assertEqual(updated["swr_run_manifest"], str(manifest.relative_to(root)))
            self.assertEqual(updated["retry_count"], 0)
            self.assertNotIn("failure_path", updated)
            self.assertNotIn("stopped_run_id", updated)
            ledger = (root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("compile_failure", ledger)

    def test_swr_active_lease_blocks_duplicate_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_active_swr_manifest(root)
            op = AutoKeel(root=root, dry_run=False)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")
            op.record_active_swr_run(slice_, manifest, "test active lease")
            lease = root / ".local/autokeel/swr/leases/S02.json"

            result = op.ensure_playbook(slice_)

            self.assertEqual(result.exit_code, 31)
            self.assertTrue(lease.exists())
            payload = json.loads(lease.read_text(encoding="utf-8"))
            self.assertEqual(payload["run_manifest"], str(manifest.relative_to(root)))

    def test_swr_missing_manifest_with_active_lease_blocks_and_records_state_divergence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            lease = root / ".local/autokeel/swr/leases/S02.json"
            lease.parent.mkdir(parents=True)
            lease.write_text(
                json.dumps(
                    {
                        "slice": "S02",
                        "run_id": "run_missing",
                        "run_dir": ".local/autokeel/swr/runs/missing",
                        "run_manifest": ".local/autokeel/swr/runs/missing/run_manifest.json",
                        "stage_id": "source_authority_map",
                        "response_id": "resp_missing",
                        "status": "in_progress",
                        "created_at": "2026-05-27T00:00:00-04:00",
                        "last_checked_at": "2026-05-27T00:00:00-04:00",
                        "next_check_not_before": "2026-05-27T00:05:00-04:00",
                        "lease_owner_pid": None,
                    }
                ),
                encoding="utf-8",
            )
            op = AutoKeel(root=root, dry_run=False)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_playbook(slice_)

            self.assertEqual(result.exit_code, 35)
            self.assertIn("manifest missing", result.stderr)
            ledger = (root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn("state_divergence", ledger)

    def test_swr_stale_lease_requires_remote_response_check_not_new_run(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_active_swr_manifest(root)
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            state["active_swr_run"] = {
                "slice": "S02",
                "run_manifest": str(manifest.relative_to(root)),
                "last_remote_check_at": "2026-01-01T00:00:00+00:00",
            }
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", state)
            lease = root / ".local/autokeel/swr/leases/S02.json"
            lease.parent.mkdir(parents=True)
            lease.write_text(
                json.dumps(
                    {
                        "slice": "S02",
                        "run_id": "run_20260527_test_active",
                        "run_dir": str(manifest.parent.relative_to(root)),
                        "run_manifest": str(manifest.relative_to(root)),
                        "stage_id": "source_authority_map",
                        "response_id": "resp_test_s02_active",
                        "status": "in_progress",
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "last_checked_at": "2026-01-01T00:00:00+00:00",
                        "next_check_not_before": "2026-01-01T00:05:00+00:00",
                        "lease_owner_pid": None,
                    }
                ),
                encoding="utf-8",
            )

            class ResumeOnlyRunner:
                def __init__(self):
                    self.calls: list[list[str]] = []

                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    argv = list(argv)
                    self.calls.append(argv)
                    if "keel-swr" in str(argv[0]) and "--run-name" in argv:
                        raise AssertionError("stale lease must not start a new SWR run")
                    if "keel-swr" in str(argv[0]) and "resume" in argv:
                        return CommandResult(
                            argv,
                            1,
                            str(manifest.relative_to(root)) + "\n",
                            "ApiError: Response resp_test_s02_active did not reach a terminal state within 5.0s (last_status=in_progress).",
                        )
                    return CommandResult(argv, 0, "", "")

            op = AutoKeel(root=root, dry_run=False)
            op.runner = ResumeOnlyRunner()
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_playbook(slice_)

            self.assertEqual(result.exit_code, 31)
            self.assertTrue(any("resume" in call for call in op.runner.calls))
            self.assertFalse(any("--run-name" in call for call in op.runner.calls))

    def test_swr_wait_timeout_in_progress_records_active_run_not_compile_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            stale_manifest = write_created_swr_manifest(root)
            stale_state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            stale_state["active_swr_run"] = {
                "slice": "S02",
                "run_id": "run_stale_created",
                "run_manifest": str(stale_manifest.relative_to(root)),
                "status": "created",
                "supervisor_session_id": "old-supervisor-session",
                "last_remote_check_at": "2999-01-01T00:00:00+00:00",
                "last_remote_check_exit_code": 0,
                "last_remote_check_stderr": "",
            }
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", stale_state)

            class InProgressSwrRunner:
                def __init__(self):
                    self.calls: list[list[str]] = []

                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    self.calls.append(list(argv))
                    joined = " ".join(str(part) for part in argv)
                    if "scripts.verify_v1" in joined:
                        return CommandResult(list(argv), 1, '{"status":"error","errors":["incomplete"]}', "")
                    if "scripts.evaluate_tripwires" in joined:
                        return CommandResult(list(argv), 0, '{"status":"ok","errors":[],"warnings":[]}', "")
                    if "keel-swr" in str(argv[0]):
                        manifest = write_active_swr_manifest(root)
                        return CommandResult(
                            list(argv),
                            1,
                            str(manifest.relative_to(root)) + "\n",
                            "ApiError: Response resp_test_s02_active did not reach a terminal state within 5.0s (last_status=in_progress).",
                        )
                    return CommandResult(list(argv), 0, "", "")

            op = AutoKeel(root=root, dry_run=False)
            op.runner = InProgressSwrRunner()

            code = op._run_once_impl(requested_slice="S02", force_slice=True)

            self.assertEqual(code, 0)
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["active_swr_run"]["slice"], "S02")
            self.assertEqual(state["active_swr_run"]["run_id"], "run_20260527_test_active")
            self.assertEqual(state["active_swr_run"]["last_remote_check_at"], state["active_swr_run"]["recorded_at"])
            self.assertEqual(state["active_swr_run"]["last_remote_check_exit_code"], 1)
            self.assertIsNone(state["active_swr_run"]["supervisor_session_id"])
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertEqual(updated["status"], "waiting_for_playbook")
            ledger = (root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("compile_failure", ledger)
            events = (root / "ops/autonomy/events.jsonl").read_text(encoding="utf-8")
            self.assertIn("swr_run_active", events)
            self.assertIn("waiting_for_playbook_or_compile_inputs", events)

    def test_swr_waiting_for_review_runs_supervisor_review_lane_before_next_stage(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_waiting_swr_manifest(root)

            class ReviewLaneRunner:
                def __init__(self):
                    self.calls: list[list[str]] = []

                def _arg(self, argv: list[str], flag: str) -> str:
                    return argv[argv.index(flag) + 1]

                def _write_decision(
                    self,
                    rel: str,
                    *,
                    actor_role: str,
                    review_kind: str = "stage_output",
                    approval: str = "approve",
                ) -> None:
                    path = root / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(
                        json.dumps(
                            swr_review_decision(
                                actor_role=actor_role,
                                review_kind=review_kind,
                                approval_decision=approval,
                            )
                        ),
                        encoding="utf-8",
                    )

                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    argv = list(argv)
                    self.calls.append(argv)
                    joined = " ".join(str(part) for part in argv)
                    if "supervisor init-session" in joined:
                        return CommandResult(argv, 0, '{"session":"autokeel-s02-run_20260527_test_waiting"}', "")
                    if "supervisor classify" in joined:
                        output = self._arg(argv, "--output")
                        (root / output).parent.mkdir(parents=True, exist_ok=True)
                        (root / output).write_text(
                            json.dumps(
                                {
                                    "run_id": "run_20260527_test_waiting",
                                    "stage_id": "source_authority_map",
                                    "classification": "completed_complete_artifact",
                                    "reviewable": True,
                                    "review_bundle_allowed": True,
                                }
                            ),
                            encoding="utf-8",
                        )
                        return CommandResult(argv, 0, '{"classification":"completed_complete_artifact"}', "")
                    if "supervisor invoke-operator" in joined:
                        rel = self._arg(argv, "--output-dir") + "/operator.json"
                        self._write_decision(rel, actor_role="operator_codex")
                        return CommandResult(argv, 0, json.dumps({"operator_review": rel}), "")
                    if "supervisor invoke-reviewers" in joined:
                        out_dir = self._arg(argv, "--output-dir")
                        codex = out_dir + "/codex.json"
                        claude = out_dir + "/claude.json"
                        self._write_decision(codex, actor_role="codex_review_agent")
                        self._write_decision(claude, actor_role="claude_review_agent")
                        return CommandResult(argv, 0, json.dumps({"codex_review": codex, "claude_review": claude}), "")
                    if "supervisor consolidate" in joined:
                        output = self._arg(argv, "--output")
                        self._write_decision(output, actor_role="consolidation_pass", review_kind="consolidation", approval="approve_with_conditions")
                        return CommandResult(argv, 0, json.dumps({"json_report_path": output}), "")
                    if "supervisor accept" in joined:
                        output = self._arg(argv, "--output")
                        self._write_decision(output, actor_role="operator_codex", review_kind="operator_acceptance")
                        return CommandResult(argv, 0, json.dumps({"json_report_path": output, "approval_decision": "approve"}), "")
                    if "supervisor create-bundle" in joined:
                        output = self._arg(argv, "--output")
                        (root / output).parent.mkdir(parents=True, exist_ok=True)
                        (root / output).write_text(json.dumps({"bundle_path": output}), encoding="utf-8")
                        return CommandResult(argv, 0, json.dumps({"bundle_path": output}), "")
                    if "keel-swr" in str(argv[0]) and "run" in argv:
                        if "--review-bundle" not in argv:
                            raise AssertionError("review bundle required before continuing SWR")
                        if "--primary-job-input" not in argv:
                            raise AssertionError("primary job inputs must be carried into SWR continuation")
                        payload = json.loads(manifest.read_text(encoding="utf-8"))
                        payload["status"] = "running"
                        payload["current_stage_id"] = "repo_grounding"
                        payload["stages"][0]["status"] = "waiting_for_review"
                        payload["stages"][1]["status"] = "in_progress"
                        payload["stages"][1]["response_id"] = "resp_stage2"
                        payload["stages"][1]["stage_dir"] = str((manifest.parent / "stages/02_repo_grounding").relative_to(root))
                        manifest.write_text(json.dumps(payload), encoding="utf-8")
                        return CommandResult(
                            argv,
                            1,
                            str(manifest.relative_to(root)) + "\n",
                            "ApiError: Response resp_stage2 did not reach a terminal state within 5.0s (last_status=in_progress).",
                        )
                    return CommandResult(argv, 0, "", "")

            op = AutoKeel(root=root, dry_run=False)
            op.runner = ReviewLaneRunner()
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_playbook(slice_)

            self.assertEqual(result.exit_code, 31)
            calls = [" ".join(call) for call in op.runner.calls]
            self.assertTrue(any("supervisor classify" in call for call in calls))
            self.assertTrue(any("supervisor invoke-operator" in call for call in calls))
            self.assertTrue(any("supervisor invoke-reviewers" in call for call in calls))
            self.assertTrue(any("supervisor create-bundle" in call for call in calls))
            self.assertTrue(any("keel-swr run" in call and "--review-bundle" in call for call in calls))
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["active_swr_run"]["current_stage_id"], "repo_grounding")
            self.assertEqual(state["active_swr_run"]["response_id"], "resp_stage2")

    def test_swr_review_lane_blocks_malformed_operator_before_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_waiting_swr_manifest(root)

            class MalformedOperatorRunner:
                def __init__(self):
                    self.calls: list[list[str]] = []

                def _arg(self, argv: list[str], flag: str) -> str:
                    return argv[argv.index(flag) + 1]

                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    argv = list(argv)
                    self.calls.append(argv)
                    joined = " ".join(str(part) for part in argv)
                    if "supervisor init-session" in joined:
                        return CommandResult(argv, 0, '{"session":"autokeel-s02-run_20260527_test_waiting"}', "")
                    if "supervisor classify" in joined:
                        output = self._arg(argv, "--output")
                        (root / output).parent.mkdir(parents=True, exist_ok=True)
                        (root / output).write_text(
                            json.dumps(
                                {
                                    "run_id": "run_20260527_test_waiting",
                                    "stage_id": "source_authority_map",
                                    "classification": "completed_complete_artifact",
                                    "reviewable": True,
                                    "review_bundle_allowed": True,
                                }
                            ),
                            encoding="utf-8",
                        )
                        return CommandResult(argv, 0, '{"classification":"completed_complete_artifact"}', "")
                    if "supervisor invoke-operator" in joined:
                        rel = self._arg(argv, "--output-dir") + "/operator.json"
                        path = root / rel
                        path.parent.mkdir(parents=True, exist_ok=True)
                        path.write_text(
                            json.dumps(
                                swr_review_decision(
                                    actor_role="operator_codex",
                                    review_kind="stage_output",
                                    approval_decision="blocked",
                                    status="malformed_output",
                                    next_action="blocked",
                                    blocking_issues=[
                                        {
                                            "issue_id": "operator_output_failure",
                                            "severity": "blocking",
                                            "description": "Agent stdout did not contain JSON.",
                                            "evidence": ["Agent stdout did not contain JSON."],
                                            "affected_artifacts": [],
                                        }
                                    ],
                                    validation_errors=["Agent stdout did not contain JSON."],
                                )
                            ),
                            encoding="utf-8",
                        )
                        return CommandResult(argv, 0, json.dumps({"operator_review": rel}), "")
                    if "supervisor invoke-reviewers" in joined or "supervisor create-bundle" in joined or "keel-swr run" in joined:
                        raise AssertionError("malformed operator review must block before reviewer, bundle, or continuation")
                    return CommandResult(argv, 0, "", "")

            op = AutoKeel(root=root, dry_run=False)
            op.runner = MalformedOperatorRunner()
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_playbook(slice_)

            self.assertEqual(result.exit_code, 32)
            self.assertIn("operator review failed closed", result.stderr)
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertEqual(updated["status"], "blocked_compile_inputs")
            calls = [" ".join(call) for call in op.runner.calls]
            self.assertFalse(any("supervisor invoke-reviewers" in call for call in calls))
            self.assertFalse(any("supervisor create-bundle" in call for call in calls))
            self.assertEqual(json.loads(manifest.read_text(encoding="utf-8"))["current_stage_id"], "source_authority_map")

    def test_swr_waiting_for_review_reuses_existing_approved_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_waiting_swr_manifest(root)
            bundle = write_existing_swr_review_bundle(root, manifest)

            class ReuseBundleRunner:
                def __init__(self):
                    self.calls: list[list[str]] = []

                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    argv = list(argv)
                    self.calls.append(argv)
                    joined = " ".join(str(part) for part in argv)
                    if "supervisor invoke-operator" in joined or "supervisor invoke-reviewers" in joined:
                        raise AssertionError("existing approved review bundle should be reused")
                    if "keel-swr" in str(argv[0]) and "run" in argv:
                        if "--review-bundle" not in argv:
                            raise AssertionError("review bundle required before continuing SWR")
                        if argv[argv.index("--review-bundle") + 1] != str(bundle.relative_to(root)):
                            raise AssertionError("existing approved review bundle was not reused")
                        if "--primary-job-input" not in argv:
                            raise AssertionError("primary job inputs must be carried into SWR continuation")
                        payload = json.loads(manifest.read_text(encoding="utf-8"))
                        payload["status"] = "running"
                        payload["current_stage_id"] = "repo_grounding"
                        payload["stages"][0]["review_approved"] = True
                        payload["stages"][0]["review_bundle_path"] = str(bundle.relative_to(root))
                        payload["stages"][1]["status"] = "in_progress"
                        payload["stages"][1]["response_id"] = "resp_stage2_reused"
                        manifest.write_text(json.dumps(payload), encoding="utf-8")
                        return CommandResult(
                            argv,
                            1,
                            "",
                            "ApiError: Response resp_stage2_reused did not reach a terminal state within 5.0s (last_status=in_progress).",
                        )
                    return CommandResult(argv, 0, "", "")

            op = AutoKeel(root=root, dry_run=False)
            op.runner = ReuseBundleRunner()
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_playbook(slice_)

            self.assertEqual(result.exit_code, 31)
            calls = [" ".join(call) for call in op.runner.calls]
            self.assertFalse(any("supervisor invoke-operator" in call for call in calls))
            self.assertFalse(any("supervisor invoke-reviewers" in call for call in calls))
            self.assertTrue(any("keel-swr run" in call and "--review-bundle" in call for call in calls))
            events = (root / "ops/autonomy/events.jsonl").read_text(encoding="utf-8")
            self.assertIn("swr_stage_review_bundle_reused", events)
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["active_swr_run"]["current_stage_id"], "repo_grounding")
            self.assertEqual(state["active_swr_run"]["response_id"], "resp_stage2_reused")

    def test_swr_review_bundle_requires_matching_response_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_waiting_swr_manifest(root)
            bundle = write_existing_swr_review_bundle(root, manifest)
            report = validate_swr_review_bundle(bundle, root=root)
            self.assertEqual(report["status"], "ok", report)

            payload = json.loads(bundle.read_text(encoding="utf-8"))
            payload["output_sha256"] = "0" * 64
            bundle.write_text(json.dumps(payload), encoding="utf-8")

            report = validate_swr_review_bundle(bundle, root=root)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("output_sha256" in error for error in report["errors"]))

    def test_swr_review_bundle_rejects_malformed_operator_record(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_waiting_swr_manifest(root)
            bundle = write_existing_swr_review_bundle(root, manifest)
            payload = json.loads(bundle.read_text(encoding="utf-8"))
            operator_record = root / payload["review_decision_records"]["operator_review"]
            operator_payload = json.loads(operator_record.read_text(encoding="utf-8"))
            operator_payload["status"] = "malformed_output"
            operator_payload["approval_decision"] = "blocked"
            operator_payload["next_action"] = "blocked"
            operator_payload["validation_errors"] = ["Agent stdout did not contain JSON."]
            operator_record.write_text(json.dumps(operator_payload), encoding="utf-8")

            report = validate_swr_review_bundle(bundle, root=root)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("operator provisional review status must be succeeded" in error for error in report["errors"]))

            op = AutoKeel(root=root, dry_run=False)
            manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertIsNone(op.existing_swr_review_bundle(slice_, manifest_payload))

    def test_swr_review_bundle_rejects_blocking_consolidation_despite_acceptance(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_waiting_swr_manifest(root)
            bundle = write_existing_swr_review_bundle(root, manifest)
            payload = json.loads(bundle.read_text(encoding="utf-8"))
            consolidated_record = root / payload["review_decision_records"]["consolidated_review"]
            consolidated_payload = json.loads(consolidated_record.read_text(encoding="utf-8"))
            consolidated_payload["approval_decision"] = "do_not_approve"
            consolidated_payload["blocking_issues"] = [
                {
                    "issue_id": "reviewer_output_failure",
                    "severity": "blocking",
                    "description": "Reviewer output failed schema validation.",
                    "evidence": ["review decision failed schema validation"],
                    "affected_artifacts": [],
                }
            ]
            consolidated_record.write_text(json.dumps(consolidated_payload), encoding="utf-8")

            report = validate_swr_review_bundle(bundle, root=root)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("consolidated review approval_decision" in error for error in report["errors"]))
            self.assertTrue(any("consolidated review contains blocking_issues" in error for error in report["errors"]))

            op = AutoKeel(root=root, dry_run=False)
            manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertIsNone(op.existing_swr_review_bundle(slice_, manifest_payload))

    def test_active_swr_run_plans_review_repair_when_prior_review_bundle_is_invalid(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_waiting_swr_manifest(root)
            bundle = write_existing_swr_review_bundle(root, manifest)
            bundle_payload = json.loads(bundle.read_text(encoding="utf-8"))
            operator_record = root / bundle_payload["review_decision_records"]["operator_review"]
            operator_payload = json.loads(operator_record.read_text(encoding="utf-8"))
            operator_payload["status"] = "malformed_output"
            operator_payload["approval_decision"] = "blocked"
            operator_payload["next_action"] = "blocked"
            operator_record.write_text(json.dumps(operator_payload), encoding="utf-8")

            manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
            manifest_payload["status"] = "running"
            manifest_payload["current_stage_id"] = "repo_grounding"
            manifest_payload["stages"][0]["review_bundle_path"] = str(bundle.relative_to(root))
            manifest_payload["stages"][0]["review_approved"] = True
            manifest_payload["stages"][1]["status"] = "in_progress"
            manifest_payload["stages"][1]["response_id"] = "resp_repo_grounding"
            manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")

            class NoRemoteRunner:
                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    if "keel-swr" in str(argv[0]) and "run" in argv:
                        raise AssertionError("invalid prior review bundle must block before remote resume")
                    return CommandResult(list(argv), 0, "", "")

            op = AutoKeel(root=root, dry_run=False)
            op.runner = NoRemoteRunner()
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_playbook(slice_)

            self.assertEqual(result.exit_code, 32)
            self.assertIn("SWR review history failed closed", result.stderr)
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertEqual(updated["status"], "blocked_compile_inputs")
            repair = updated["swr_review_repair"]
            self.assertEqual(repair["repair_action"], "rerun_review_lane")
            self.assertEqual(repair["repair_stage_id"], "source_authority_map")
            self.assertIn("repo_grounding", repair["stale_downstream_stage_ids"])
            self.assertEqual(repair["stage_artifact_errors"], [])
            quarantined = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertNotIn("autokeel_quarantined", quarantined)
            self.assertEqual(quarantined["status"], "running")
            events = (root / "ops/autonomy/events.jsonl").read_text(encoding="utf-8")
            self.assertIn("swr_review_repair_planned", events)

    def test_swr_review_history_ignores_consumed_prior_stage_bundle_path(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_waiting_swr_manifest(root)
            bundle = write_existing_swr_review_bundle(root, manifest)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            payload["status"] = "running"
            payload["current_stage_id"] = "repo_grounding"
            payload["stages"][0]["review_bundle_path"] = str(bundle.relative_to(root))
            payload["stages"][0]["review_approved"] = True
            payload["stages"][1]["status"] = "in_progress"
            payload["stages"][1]["review_bundle_path"] = str(bundle.relative_to(root))
            payload["stages"][1].pop("review_approved", None)

            op = AutoKeel(root=root, dry_run=False)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            self.assertEqual(op.swr_review_history_findings(slice_, payload), [])

    def test_swr_review_repair_reruns_review_lane_without_fresh_full_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_waiting_swr_manifest(root)
            repair = {
                "created_at": "2026-05-27T00:00:00-04:00",
                "status": "planned",
                "repair_action": "rerun_review_lane",
                "repair_stage_id": "source_authority_map",
                "run_id": "run_20260527_test_waiting",
                "run_dir": str(manifest.parent.relative_to(root)),
                "run_manifest": str(manifest.relative_to(root)),
                "stale_downstream_stage_ids": ["repo_grounding"],
            }
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S02":
                    item["status"] = "blocked_compile_inputs"
                    item["swr_review_repair"] = repair
            write_json_atomic(root / "ops/autonomy/slices.json", slices)

            class ReviewRepairRunner:
                def __init__(self):
                    self.calls: list[list[str]] = []

                def _arg(self, argv: list[str], flag: str) -> str:
                    return argv[argv.index(flag) + 1]

                def _write_decision(
                    self,
                    rel: str,
                    *,
                    actor_role: str,
                    review_kind: str = "stage_output",
                    approval: str = "approve",
                ) -> None:
                    path = root / rel
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_text(
                        json.dumps(
                            swr_review_decision(
                                actor_role=actor_role,
                                review_kind=review_kind,
                                approval_decision=approval,
                            )
                        ),
                        encoding="utf-8",
                    )

                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    argv = list(argv)
                    self.calls.append(argv)
                    joined = " ".join(str(part) for part in argv)
                    if "keel-swr" in str(argv[0]) and "--run-name" in argv:
                        raise AssertionError("fresh full SWR workflow rerun is forbidden")
                    if "supervisor init-session" in joined:
                        return CommandResult(argv, 0, '{"session":"autokeel-s02-run_20260527_test_waiting"}', "")
                    if "supervisor classify" in joined:
                        output = self._arg(argv, "--output")
                        (root / output).parent.mkdir(parents=True, exist_ok=True)
                        (root / output).write_text(
                            json.dumps(
                                {
                                    "run_id": "run_20260527_test_waiting",
                                    "stage_id": "source_authority_map",
                                    "classification": "completed_complete_artifact",
                                    "reviewable": True,
                                    "review_bundle_allowed": True,
                                }
                            ),
                            encoding="utf-8",
                        )
                        return CommandResult(argv, 0, '{"classification":"completed_complete_artifact"}', "")
                    if "supervisor invoke-operator" in joined:
                        rel = self._arg(argv, "--output-dir") + "/operator.json"
                        self._write_decision(rel, actor_role="operator_codex")
                        return CommandResult(argv, 0, json.dumps({"operator_review": rel}), "")
                    if "supervisor invoke-reviewers" in joined:
                        out_dir = self._arg(argv, "--output-dir")
                        codex = out_dir + "/codex.json"
                        claude = out_dir + "/claude.json"
                        self._write_decision(codex, actor_role="codex_review_agent")
                        self._write_decision(claude, actor_role="claude_review_agent")
                        return CommandResult(argv, 0, json.dumps({"codex_review": codex, "claude_review": claude}), "")
                    if "supervisor consolidate" in joined:
                        output = self._arg(argv, "--output")
                        self._write_decision(output, actor_role="consolidation_pass", review_kind="consolidation", approval="approve_with_conditions")
                        return CommandResult(argv, 0, json.dumps({"json_report_path": output}), "")
                    if "supervisor accept" in joined:
                        output = self._arg(argv, "--output")
                        self._write_decision(output, actor_role="operator_codex", review_kind="operator_acceptance")
                        return CommandResult(argv, 0, json.dumps({"json_report_path": output, "approval_decision": "approve"}), "")
                    if "supervisor create-bundle" in joined:
                        output = self._arg(argv, "--output")
                        (root / output).parent.mkdir(parents=True, exist_ok=True)
                        (root / output).write_text(json.dumps({"bundle_path": output}), encoding="utf-8")
                        return CommandResult(argv, 0, json.dumps({"bundle_path": output}), "")
                    if "keel-swr" in str(argv[0]) and "run" in argv:
                        if "--run-name" in argv:
                            raise AssertionError("fresh full SWR workflow rerun is forbidden")
                        if "--run-dir" not in argv:
                            raise AssertionError("review repair must target the existing run directory")
                        if "--review-bundle" not in argv:
                            raise AssertionError("review repair must continue with the repaired bundle")
                        payload = json.loads(manifest.read_text(encoding="utf-8"))
                        payload["status"] = "running"
                        payload["current_stage_id"] = "repo_grounding"
                        payload["stages"][0]["review_approved"] = True
                        payload["stages"][0]["review_bundle_path"] = argv[argv.index("--review-bundle") + 1]
                        payload["stages"][1]["status"] = "in_progress"
                        payload["stages"][1]["response_id"] = "resp_stage2_after_review_repair"
                        manifest.write_text(json.dumps(payload), encoding="utf-8")
                        return CommandResult(
                            argv,
                            1,
                            str(manifest.relative_to(root)) + "\n",
                            "ApiError: Response resp_stage2_after_review_repair did not reach a terminal state within 5.0s (last_status=in_progress).",
                        )
                    return CommandResult(argv, 0, "", "")

            op = AutoKeel(root=root, dry_run=False)
            runner = ReviewRepairRunner()
            op.runner = runner
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_playbook(slice_)

            self.assertEqual(result.exit_code, 31)
            calls = [" ".join(call) for call in runner.calls]
            self.assertTrue(any("supervisor invoke-operator" in call for call in calls))
            self.assertTrue(any("keel-swr run" in call and "--review-bundle" in call for call in calls))
            self.assertFalse(any("keel-swr run" in call and "--run-name" in call for call in calls))
            repaired = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertNotIn("autokeel_quarantined", repaired)
            self.assertEqual(repaired["current_stage_id"], "repo_grounding")
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertNotIn("swr_review_repair", updated)
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["active_swr_run"]["run_id"], "run_20260527_test_waiting")

    def test_quarantined_review_failure_manifest_plans_repair_before_fresh_launch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_waiting_swr_manifest(root)
            manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
            manifest_payload["status"] = "quarantined"
            manifest_payload["autokeel_quarantined"] = True
            manifest.write_text(json.dumps(manifest_payload), encoding="utf-8")
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S02":
                    item["status"] = "blocked_compile_inputs"
                    item["swr_run_manifest"] = str(manifest.relative_to(root))
                    item["reason"] = "SWR independent review failed closed: reviewer decision contains blocking_issues"
            write_json_atomic(root / "ops/autonomy/slices.json", slices)

            class NoFreshLaunchRunner:
                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    if "keel-swr" in str(argv[0]) and "run" in argv:
                        raise AssertionError("stored repairable SWR run must be planned before any fresh launch")
                    return CommandResult(list(argv), 0, "", "")

            op = AutoKeel(root=root, dry_run=False)
            op.runner = NoFreshLaunchRunner()
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_playbook(slice_)

            self.assertEqual(result.exit_code, 32)
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertEqual(updated["status"], "blocked_compile_inputs")
            self.assertEqual(updated["swr_review_repair"]["repair_action"], "rerun_review_lane")
            self.assertEqual(updated["swr_review_repair"]["repair_stage_id"], "source_authority_map")
            self.assertEqual(updated["swr_review_repair"]["stage_artifact_errors"], [])
            events = (root / "ops/autonomy/events.jsonl").read_text(encoding="utf-8")
            self.assertIn("swr_review_repair_planned_from_stored_manifest", events)

    def test_swr_review_repair_stage_rerun_uses_same_run_and_clears_plan_while_active(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_waiting_swr_manifest(root)
            bundle = root / ".local/autokeel/swr/review_lane/S02-run_20260527_test_waiting-source_authority_map/source_authority_map.review_bundle.json"
            bundle.parent.mkdir(parents=True, exist_ok=True)
            bundle.write_text(json.dumps({"review_status": "approved"}), encoding="utf-8")
            repair = {
                "created_at": "2026-05-27T00:00:00-04:00",
                "status": "planned",
                "repair_action": "rerun_single_stage",
                "repair_stage_id": "repo_grounding",
                "source_review_stage_id": "source_authority_map",
                "source_review_bundle": str(bundle.relative_to(root)),
                "run_id": "run_20260527_test_waiting",
                "run_dir": str(manifest.parent.relative_to(root)),
                "run_manifest": str(manifest.relative_to(root)),
            }
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S02":
                    item["status"] = "blocked_compile_inputs"
                    item["swr_review_repair"] = repair
            write_json_atomic(root / "ops/autonomy/slices.json", slices)

            class StageRepairRunner:
                def __init__(self):
                    self.calls: list[list[str]] = []

                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    argv = list(argv)
                    self.calls.append(argv)
                    if "keel-swr" in str(argv[0]) and "run" in argv:
                        if "--run-name" in argv:
                            raise AssertionError("fresh full SWR workflow rerun is forbidden")
                        if argv[argv.index("--stage") + 1] != "repo_grounding":
                            raise AssertionError("review repair must rerun only the requested stage")
                        if argv[argv.index("--review-bundle") + 1] != str(bundle.relative_to(root)):
                            raise AssertionError("review repair must pass the approved source bundle")
                        payload = json.loads(manifest.read_text(encoding="utf-8"))
                        payload["status"] = "running"
                        payload["current_stage_id"] = "repo_grounding"
                        payload["stages"][1]["status"] = "in_progress"
                        payload["stages"][1]["response_id"] = "resp_repo_grounding_review_repair"
                        manifest.write_text(json.dumps(payload), encoding="utf-8")
                        return CommandResult(
                            argv,
                            1,
                            str(manifest.relative_to(root)) + "\n",
                            "ApiError: Response resp_repo_grounding_review_repair did not reach a terminal state within 5.0s (last_status=in_progress).",
                        )
                    return CommandResult(argv, 0, "", "")

            op = AutoKeel(root=root, dry_run=False)
            runner = StageRepairRunner()
            op.runner = runner
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_playbook(slice_)

            self.assertEqual(result.exit_code, 31)
            self.assertTrue(any("keel-swr" in call[0] and "--stage" in call for call in runner.calls))
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertNotIn("swr_review_repair", updated)
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["active_swr_run"]["response_id"], "resp_repo_grounding_review_repair")

    def test_swr_review_bundle_reuse_rejects_different_response_id(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_waiting_swr_manifest(root)
            bundle = write_existing_swr_review_bundle(root, manifest)
            payload = json.loads(bundle.read_text(encoding="utf-8"))
            payload["response_id"] = "resp_other"
            bundle.write_text(json.dumps(payload), encoding="utf-8")
            op = AutoKeel(root=root, dry_run=False)

            manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertIsNone(op.existing_swr_review_bundle(next(item for item in op.load_slices() if item["id"] == "S02"), manifest_payload))

    def test_s02_readiness_blocks_when_active_swr_manifest_exists_without_state_adoption(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_active_swr_manifest(root)
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S01":
                    item["status"] = "complete"
                if item["id"] == "S02":
                    item["status"] = "pending"
            write_json_atomic(root / "ops/autonomy/slices.json", slices)

            report = verify_s02_readiness(root)

            self.assertEqual(report["status"], "error")
            self.assertEqual(report["checks"]["active_swr_manifest"], str(manifest.relative_to(root)))
            self.assertIn("do not launch a new run", "\n".join(report["errors"]))

    def test_created_swr_manifest_without_response_does_not_block_relaunch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_created_swr_manifest(root)
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S01":
                    item["status"] = "complete"
                if item["id"] == "S02":
                    item["status"] = "pending"
            write_json_atomic(root / "ops/autonomy/slices.json", slices)
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            state["active_swr_run"] = {
                "slice": "S02",
                "run_manifest": str(manifest.relative_to(root)),
                "status": "created",
            }
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", state)

            op = AutoKeel(root=root, dry_run=False)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            report = verify_s02_readiness(root)

            self.assertFalse(op.swr_run_is_active(payload))
            self.assertEqual(report["checks"]["active_swr_manifest"], None)
            self.assertNotIn("do not launch a new run", "\n".join(report["errors"]))
            self.assertIn("stale or non-active SWR manifest", "\n".join(report["warnings"]))

    def test_completed_swr_manifest_review_history_does_not_block_relaunch(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_completed_swr_manifest_with_review_history(root)
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S01":
                    item["status"] = "complete"
                if item["id"] == "S02":
                    item["status"] = "pending"
            write_json_atomic(root / "ops/autonomy/slices.json", slices)

            op = AutoKeel(root=root, dry_run=False)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            report = verify_s02_readiness(root)

            self.assertFalse(op.swr_run_is_active(payload))
            self.assertEqual(report["checks"]["active_swr_manifest"], None)
            self.assertNotIn("do not launch a new run", "\n".join(report["errors"]))

    def test_completed_swr_manifest_materializes_canonical_playbook(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            fake_keel = root / "fake-keel"
            task_pack_source = fake_keel / "tools/staged-workflow-runner/automation/task_packs/gstack_design_to_po_playbook"
            (task_pack_source / "workflows").mkdir(parents=True)
            (task_pack_source / "workflows/gstack_design_to_po_playbook.workflow.json").write_text("{}", encoding="utf-8")
            (fake_keel / "bin").mkdir(parents=True)

            policy = (root / "ops/autonomy/policy.yaml").read_text(encoding="utf-8")
            policy = policy.replace("keel_root: /Users/aeziz-local/keel", f"keel_root: {fake_keel}")
            (root / "ops/autonomy/policy.yaml").write_text(policy, encoding="utf-8")

            (root / "docs/gstack").mkdir(parents=True)
            (root / "docs/briefs").mkdir(parents=True)
            (root / "docs/gstack/health-data-hub-office-hours.md").write_text("S02 design", encoding="utf-8")
            (root / "docs/briefs/s02-mood-api.autonomous-brief.md").write_text("S02 brief", encoding="utf-8")
            (root / "docs/gstack/s02-mood-api-autoplan.md").write_text(
                "# S02 autoplan\n\n"
                "Deliverables and verification are listed below.\n\n"
                "Manual gates are forbidden; use autonomous_gate_review evidence instead.\n\n"
                "## Implementation Tasks\n\n"
                "- [ ] Implement the Mood API loop.\n"
                "  Files: `src/api/mood.py`; `tests/test_api_security.py`\n"
                "  Verify: `python -m pytest tests/test_api_security.py -q`\n",
                encoding="utf-8",
            )

            class SwrRunner:
                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    run_dir = root / ".local/autokeel/swr/runs/test-run"
                    stage_dir = run_dir / "stages/05_final_markdown_playbook"
                    stage_dir.mkdir(parents=True)
                    response_path = stage_dir / "response.final.json"
                    response_path.write_text(
                        json.dumps(
                            {
                                "output": [
                                    {
                                        "type": "message",
                                        "content": [
                                            {
                                                "type": "output_text",
                                                "text": "# S02 Mood API Playbook\n\nmarkdown_playbook_v1\n",
                                            }
                                        ],
                                    }
                                ]
                            }
                        ),
                        encoding="utf-8",
                    )
                    manifest = {
                        "status": "completed",
                        "stages": [
                            {
                                "stage_id": "final_markdown_playbook",
                                "status": "completed",
                                "response_json_path": str(response_path.relative_to(root)),
                            }
                        ],
                    }
                    manifest_path = run_dir / "run_manifest.json"
                    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
                    return CommandResult(list(argv), 0, str(manifest_path.relative_to(root)) + "\n", "")

            op = AutoKeel(root=root, dry_run=False)
            op.runner = SwrRunner()
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")
            result = op.ensure_playbook(slice_)

            self.assertTrue(result.ok, result.stderr)
            playbook = root / "docs/playbooks/s02-mood-api.playbook.md"
            self.assertTrue(playbook.exists())
            self.assertEqual(playbook.read_text(encoding="utf-8"), "# S02 Mood API Playbook\n\nmarkdown_playbook_v1\n")
            self.assertFalse((root / "docs/evidence/s02-mood-api-swr-playbook-evidence.json").exists())
            self.assertEqual(
                op._swr_materializations["S02"]["swr_source"]["manifest"],
                ".local/autokeel/swr/runs/test-run/run_manifest.json",
            )
            events = (root / "ops/autonomy/events.jsonl").read_text(encoding="utf-8")
            self.assertIn("swr_playbook_materialized", events)

    def test_s02_po_start_blocks_without_matching_swr_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            playbook = root / "docs/playbooks/s02-mood-api.playbook.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text("# stale S02 playbook\n", encoding="utf-8")

            op = AutoKeel(root=root, dry_run=False)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")
            result = op.start_or_resume_po(slice_)

            self.assertEqual(result.exit_code, 29)
            self.assertIn("SWR-required slice cannot enter PO", result.stderr)
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertEqual(updated["status"], "blocked_compile_inputs")
            ledger = (root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn("state_divergence", ledger)

    def test_high_risk_swr_slice_cannot_ship_without_matching_swr_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            playbook = root / "docs/playbooks/s02-mood-api.playbook.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text("# stale S02 playbook\n", encoding="utf-8")

            op = AutoKeel(root=root, dry_run=False)
            result = op.ship_slice("S02", "RUN_TEST")

            self.assertEqual(result.exit_code, 29)
            self.assertIn("SWR-required slice", result.stderr)

    def test_s02_stale_compiler_playbook_is_archived_before_swr(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            stale = root / "docs/playbooks/s02-mood-api.playbook.md"
            stale.write_text("compiler stale playbook\n", encoding="utf-8")
            runner = CompletedSwrRunner(root)
            op = AutoKeel(root=root, dry_run=False)
            op.runner = runner
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_playbook(slice_)

            self.assertTrue(result.ok, result.stderr)
            self.assertTrue(any("keel-swr" in call[0] for call in runner.calls))
            archived = list((root / "ops/autonomy/failures/archived_playbooks").glob("S02-*-s02-mood-api.playbook.md"))
            self.assertEqual(len(archived), 1)
            self.assertEqual(stale.read_text(encoding="utf-8"), "# S02 Mood API Playbook\n\nmarkdown_playbook_v1\n")

    def test_s02_swr_evidence_hash_mismatch_forces_regeneration(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            playbook = root / "docs/playbooks/s02-mood-api.playbook.md"
            playbook.write_text("old swr playbook\n", encoding="utf-8")
            evidence = root / "docs/evidence/s02-mood-api-swr-playbook-evidence.json"
            write_json_atomic(
                evidence,
                {
                    "status": "ok",
                    "tool": "keel-swr",
                    "slice": "S02",
                    "playbook": "docs/playbooks/s02-mood-api.playbook.md",
                    "playbook_sha256": "not-the-current-hash",
                },
            )
            runner = CompletedSwrRunner(root, text="# regenerated\n\nmarkdown_playbook_v1\n")
            op = AutoKeel(root=root, dry_run=False)
            op.runner = runner
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_playbook(slice_)

            self.assertTrue(result.ok, result.stderr)
            self.assertTrue(any("keel-swr" in call[0] for call in runner.calls))
            self.assertEqual(playbook.read_text(encoding="utf-8"), "# regenerated\n\nmarkdown_playbook_v1\n")
            self.assertFalse(evidence.exists())
            archived_evidence = list((root / "ops/autonomy/failures/archived_playbooks").glob("S02-*-s02-mood-api-swr-playbook-evidence.json"))
            self.assertEqual(len(archived_evidence), 1)

    def test_s02_swr_output_must_validate_before_po_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)

            class InvalidValidationRunner:
                def __init__(self):
                    self.calls: list[list[str]] = []

                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    self.calls.append(list(argv))
                    joined = " ".join(str(part) for part in argv)
                    if "scripts.verify_v1" in joined:
                        return CommandResult(list(argv), 1, '{"status":"error","errors":["incomplete"]}', "")
                    if "scripts.evaluate_tripwires" in joined:
                        return CommandResult(list(argv), 0, '{"status":"ok","errors":[],"warnings":[]}', "")
                    if "keel-swr" in str(argv[0]):
                        manifest_path = write_completed_swr_manifest_with_stage_contract_drift(root)
                        return CommandResult(list(argv), 0, str(manifest_path.relative_to(root)) + "\n", "")
                    if "scripts.validate_playbook_autonomous" in joined:
                        return CommandResult(
                            list(argv),
                            1,
                            '{"status":"error","errors":["missing required column: required_verification_commands","missing required gate term: autonomous_gate_review"]}',
                            "",
                        )
                    if "supervise" in joined and "run" in joined:
                        return CommandResult(list(argv), 99, "", "PO should not start")
                    return CommandResult(list(argv), 0, "", "")

            op = AutoKeel(root=root, dry_run=False)
            op.runner = InvalidValidationRunner()
            code = op._run_once_impl(requested_slice="S02", force_slice=True)

            self.assertEqual(code, 3)
            self.assertFalse((root / "docs/evidence/s02-mood-api-swr-playbook-evidence.json").exists())
            self.assertFalse((root / "docs/playbooks/s02-mood-api.playbook.md").exists())
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertEqual(updated["status"], "blocked_compile_inputs")
            self.assertEqual(updated["reason"], "SWR playbook validation failed; minimal stage repair required")
            self.assertNotEqual(updated["status"], "replan_required")
            repair = updated["swr_validation_repair"]
            self.assertEqual(repair["repair_action"], "rerun_single_stage")
            self.assertEqual(repair["repair_stage_id"], "gate_and_contract_review")
            self.assertEqual(repair["source_review_stage_id"], "execution_row_draft")
            self.assertIn("run_manifest", repair)
            self.assertIn("run_dir", repair)
            self.assertTrue(repair["source_review_bundle"].endswith("execution_row_draft.review_bundle.json"))
            self.assertIn("required_verification_commands", repair["stage4_missing_terms"])
            self.assertIn("autonomous_gate_review", repair["stage4_missing_terms"])
            failure_artifact = root / updated["failure_path"]
            self.assertTrue(failure_artifact.exists())
            evidence_files = list((root / "docs/evidence").glob("s02-mood-api-swr-validation-repair-*.md"))
            self.assertEqual(len(evidence_files), 1)
            events = (root / "ops/autonomy/events.jsonl").read_text(encoding="utf-8")
            self.assertIn("playbook_rejected", events)
            self.assertIn("swr_validation_repair_planned", events)
            self.assertNotIn("po_started", events)

    def test_swr_validation_failure_does_not_fresh_rerun_full_workflow(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_completed_swr_manifest_with_stage_contract_drift(root)
            repair = {
                "created_at": "2026-05-27T00:00:00-04:00",
                "status": "planned",
                "repair_action": "rerun_single_stage",
                "repair_stage_id": "gate_and_contract_review",
                "source_review_stage_id": "execution_row_draft",
                "source_review_bundle": ".local/autokeel/swr/review_lane/S02-run_20260527_test_completed-execution_row_draft/execution_row_draft.review_bundle.json",
                "run_id": "run_20260527_test_completed",
                "run_dir": str(manifest.parent.relative_to(root)),
                "run_manifest": str(manifest.relative_to(root)),
            }
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S02":
                    item["status"] = "blocked_compile_inputs"
                    item["swr_validation_repair"] = repair
            write_json_atomic(root / "ops/autonomy/slices.json", slices)

            class NoFreshSwrRunner:
                def __init__(self):
                    self.calls: list[list[str]] = []

                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    argv = list(argv)
                    self.calls.append(argv)
                    joined = " ".join(str(part) for part in argv)
                    if "scripts.verify_v1" in joined:
                        return CommandResult(argv, 1, '{"status":"error","errors":["incomplete"]}', "")
                    if "scripts.evaluate_tripwires" in joined:
                        return CommandResult(argv, 0, '{"status":"ok","errors":[],"warnings":[]}', "")
                    if "keel-swr" in str(argv[0]) and "--run-name" in argv:
                        raise AssertionError("fresh full SWR workflow rerun is forbidden")
                    return CommandResult(argv, 0, "", "")

            op = AutoKeel(root=root, dry_run=False)
            runner = NoFreshSwrRunner()
            op.runner = runner
            code = op._run_once_impl(requested_slice="S02", force_slice=True)

            self.assertEqual(code, 0)
            self.assertFalse(any("keel-swr" in call[0] and "--run-name" in call for call in runner.calls))
            self.assertTrue(any("keel-swr" in call[0] and "--run-dir" in call and "--stage" in call for call in runner.calls))
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertEqual(updated["status"], "waiting_for_playbook")
            self.assertEqual(updated["swr_validation_repair"]["repair_stage_id"], "gate_and_contract_review")
            cmd = op.build_swr_stage_rerun_command(repair)
            self.assertIn("--run-dir", cmd)
            self.assertIn(str(manifest.parent.relative_to(root)), cmd)
            self.assertIn("--stage", cmd)
            self.assertIn("gate_and_contract_review", cmd)
            self.assertIn("--review-bundle", cmd)
            self.assertIn("execution_row_draft.review_bundle.json", " ".join(cmd))
            self.assertNotIn("--run-name", cmd)
            self.assertNotIn("--output-root", cmd)
            readiness = verify_s02_readiness(root)
            self.assertEqual(readiness["status"], "error")
            self.assertTrue(any("pending SWR validation repair" in error for error in readiness["errors"]))

    def test_swr_validation_repair_recovers_revalidated_archive_without_rerun(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            archive = root / "ops/autonomy/failures/archived_playbooks/S02-test-s02-mood-api.playbook.md"
            archive.parent.mkdir(parents=True, exist_ok=True)
            archive.write_text(
                "# S02 Mood API Playbook\n\n"
                "autonomous_gate_review evidence is required before PO.\n"
                "Active human approval gates are not emitted, and review artifacts are used in lieu of human approval.\n\n"
                "| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |\n"
                "|---|---|---|---|---|---|---|---|---|\n"
                "| 01 | Preserve the local-first mood API boundary with no prospective output. | src/api/app.py; tests/test_api_security.py | src/api/app.py; tests/test_api_security.py | true | python -m pytest tests/test_api_security.py tests/test_mood_date.py tests/test_mood_correction.py -q | tests pass | none | none |\n",
                encoding="utf-8",
            )
            repair = {
                "created_at": "2026-05-27T00:00:00-04:00",
                "status": "planned",
                "repair_action": "rerun_single_stage",
                "repair_stage_id": "final_markdown_playbook",
                "run_id": "run_20260527_test_completed",
                "run_dir": ".local/autokeel/swr/runs/test-run",
                "run_manifest": ".local/autokeel/swr/runs/test-run/run_manifest.json",
                "rejected_playbook_archive": str(archive.relative_to(root)),
                "swr_source": {
                    "run_id": "run_20260527_test_completed",
                    "stage_id": "final_markdown_playbook",
                    "response_json": ".local/autokeel/swr/runs/test-run/stages/05_final_markdown_playbook/response.final.json",
                },
            }
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S02":
                    item["status"] = "blocked_compile_inputs"
                    item["failure_path"] = "ops/autonomy/failures/S02-compile_failure-test.md"
                    item["reason"] = "SWR playbook validation failed; minimal stage repair required"
                    item["swr_validation_repair"] = repair
            write_json_atomic(root / "ops/autonomy/slices.json", slices)

            class RevalidationRunner:
                def __init__(self):
                    self.calls: list[list[str]] = []

                def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                    argv = list(argv)
                    self.calls.append(argv)
                    joined = " ".join(str(part) for part in argv)
                    if "scripts.validate_playbook_autonomous" in joined:
                        playbook_path = Path(argv[3])
                        report = validate_playbook(
                            playbook_path,
                            policy_path=root / "ops/autonomy/policy.yaml",
                            risk=argv[argv.index("--risk") + 1],
                        )
                        return CommandResult(argv, 0 if report["status"] == "ok" else 1, json.dumps(report), "")
                    if "keel-swr" in str(argv[0]):
                        raise AssertionError("SWR should not run when the archived playbook revalidates")
                    return CommandResult(argv, 0, "", "")

            op = AutoKeel(root=root, dry_run=False)
            runner = RevalidationRunner()
            op.runner = runner
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.recover_revalidated_swr_playbook(slice_, repair)

            self.assertIsNotNone(result)
            self.assertTrue(result.ok)
            self.assertEqual((root / "docs/playbooks/s02-mood-api.playbook.md").read_text(encoding="utf-8"), archive.read_text(encoding="utf-8"))
            self.assertFalse(any("keel-swr" in call[0] for call in runner.calls))
            updated = next(item for item in op.load_slices() if item["id"] == "S02")
            self.assertEqual(updated["status"], "pending")
            self.assertNotIn("swr_validation_repair", updated)
            self.assertNotIn("failure_path", updated)
            self.assertEqual(updated["reason"], "SWR playbook revalidated after validator false-positive fix")
            events = (root / "ops/autonomy/events.jsonl").read_text(encoding="utf-8")
            self.assertIn("swr_validation_repair_archived_playbook_revalidated", events)
            self.assertIn("swr_validation_repair_recovered_without_rerun", events)

    def test_reset_swr_manifest_for_stage_rerun_invalidates_target_and_downstream_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_completed_swr_manifest_with_stage_contract_drift(root)
            op = AutoKeel(root=root, dry_run=False)

            op.reset_swr_manifest_for_stage_rerun(manifest, "gate_and_contract_review")

            payload = json.loads(manifest.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "created")
            self.assertEqual(payload["current_stage_id"], "gate_and_contract_review")
            stages = {stage["stage_id"]: stage for stage in payload["stages"]}
            self.assertEqual(stages["execution_row_draft"]["status"], "waiting_for_review")
            self.assertIn("response_json_path", stages["execution_row_draft"])
            self.assertEqual(stages["gate_and_contract_review"]["status"], "prepared")
            self.assertEqual(stages["final_markdown_playbook"]["status"], "prepared")
            self.assertNotIn("response_json_path", stages["gate_and_contract_review"])
            self.assertNotIn("response_json_path", stages["final_markdown_playbook"])
            self.assertNotIn("review_bundle_path", stages["gate_and_contract_review"])

    def test_swr_stage5_writer_drops_valid_stage4_repairs_stage5_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_completed_swr_manifest_with_stage_contract_drift(root)
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            stage4 = next(stage for stage in payload["stages"] if stage["stage_id"] == "gate_and_contract_review")
            stage4_text = "required_verification_commands and autonomous_gate_review are present in Stage 4."
            (root / stage4["response_markdown_path"]).write_text(stage4_text, encoding="utf-8")
            (root / stage4["response_json_path"]).write_text(
                json.dumps({"output": [{"type": "message", "content": [{"type": "output_text", "text": stage4_text}]}]}),
                encoding="utf-8",
            )
            op = AutoKeel(root=root, dry_run=False)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            diagnosis = op.diagnose_swr_validation_failure(
                slice_,
                manifest,
                ["missing required column: required_verification_commands", "missing required gate term: autonomous_gate_review"],
            )

            self.assertEqual(diagnosis["repair_stage_id"], "final_markdown_playbook")
            self.assertEqual(diagnosis["source_review_stage_id"], "gate_and_contract_review")

    def test_swr_repair_missing_model_gate_review_repairs_stage4_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_completed_swr_manifest_with_stage_contract_drift(root)
            op = AutoKeel(root=root, dry_run=False)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            diagnosis = op.diagnose_swr_validation_failure(slice_, manifest, ["missing model_gate_review"])

            self.assertEqual(diagnosis["repair_stage_id"], "gate_and_contract_review")
            self.assertIn("model_gate_review", diagnosis["stage4_missing_terms"])

    def test_swr_repair_missing_counterfactual_safety_review_repairs_stage4_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_completed_swr_manifest_with_stage_contract_drift(root)
            op = AutoKeel(root=root, dry_run=False)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            diagnosis = op.diagnose_swr_validation_failure(slice_, manifest, ["missing counterfactual_safety_review"])

            self.assertEqual(diagnosis["repair_stage_id"], "gate_and_contract_review")
            self.assertIn("counterfactual_safety_review", diagnosis["stage4_missing_terms"])

    def test_swr_repair_missing_restore_secret_logging_review_repairs_stage4_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            prepare_s02_swr_inputs(root)
            manifest = write_completed_swr_manifest_with_stage_contract_drift(root)
            op = AutoKeel(root=root, dry_run=False)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            diagnosis = op.diagnose_swr_validation_failure(slice_, manifest, ["missing restore_secret_logging_review"])

            self.assertEqual(diagnosis["repair_stage_id"], "gate_and_contract_review")
            self.assertIn("restore_secret_logging_review", diagnosis["stage4_missing_terms"])

    def test_lane_decision_review_artifacts_must_match_slice_review_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            decision = root / "ops/autonomy/decisions/S02-mismatched-reviews.json"
            decision.write_text(
                json.dumps(
                    {
                        "created_at": "2026-05-27T00:00:00-04:00",
                        "status": "accepted",
                        "slice": "S02",
                        "lane": "swr_preferred",
                        "decision": "use_swr",
                        "risk": "high",
                        "review_artifacts": [
                            "docs/reviews/s02-autonomous-security-review.md",
                            "docs/reviews/unrelated-review.md",
                        ],
                        "commands": [{"command": "python scripts/verify_autonomy_preflight.py --json", "exit_code": 0}],
                        "verdict": "pass",
                    }
                ),
                encoding="utf-8",
            )
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                if item["id"] == "S02":
                    item["lane_decision"] = "ops/autonomy/decisions/S02-mismatched-reviews.json"
            write_json_atomic(root / "ops/autonomy/slices.json", slices)
            op = AutoKeel(root=root, dry_run=True)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S02")

            result = op.ensure_lane_decision(slice_)

            self.assertFalse(result.ok)
            self.assertIn("review_artifacts must match slice review_artifacts", result.stderr)

    def test_policy_swr_preferred_is_pinned_to_use_swr(self) -> None:
        op = AutoKeel(root=ROOT, dry_run=True)
        self.assertEqual(op.policy.get("lanes", {}).get("swr_preferred"), "use_swr")

    def test_complete_status_clears_stale_failure_fields_and_records_history(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            slices[0]["failure_path"] = "ops/autonomy/failures/old.md"
            slices[0]["reason"] = "old failure"
            slices[0]["retry_count"] = 2
            write_json_atomic(root / "ops/autonomy/slices.json", slices)
            op = AutoKeel(root=root, dry_run=True)

            op.mark_slice_status(
                "S01",
                "complete",
                run_id="RUN_DONE",
                ship_branch="ship/s01",
                ship_commit="abc123",
            )

            updated = next(item for item in op.load_slices() if item["id"] == "S01")
            self.assertEqual(updated["retry_count"], 0)
            self.assertNotIn("failure_path", updated)
            self.assertNotIn("reason", updated)
            history = op.load_state()["run_history"]
            self.assertTrue(any(item["slice"] == "S01" and item["run_id"] == "RUN_DONE" for item in history))

    def test_tripwire_rejects_latest_blocked_external_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            policy = (root / "ops/autonomy/policy.yaml").read_text(encoding="utf-8")
            policy = policy.replace("date: 2026-05-30", "date: 2026-01-01")
            (root / "ops/autonomy/policy.yaml").write_text(policy, encoding="utf-8")
            report_dir = root / "private/evidence/S03/oura_smoke"
            report_dir.mkdir(parents=True)
            (report_dir / "report.json").write_text(json.dumps({"status": "blocked_external"}), encoding="utf-8")

            report = evaluate_tripwires(root)
            self.assertEqual(report["status"], "error")
            self.assertEqual(report["fired"][0]["evidence_status"]["status"], "blocked_external")

    def test_s03_required_oura_preflight_blocks_before_po_without_token(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            op = AutoKeel(root=root, dry_run=False)
            slice_ = next(item for item in op.load_slices() if item["id"] == "S03")

            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop("OURA_ACCESS_TOKEN", None)
                result = op.required_external_evidence_ready(slice_)

            self.assertFalse(result.ok)
            updated = next(item for item in op.load_slices() if item["id"] == "S03")
            self.assertEqual(updated["status"], "blocked_external")
            self.assertEqual(updated["reason"], "required Oura evidence unavailable")
            ledger = (root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn("blocked_external_missing_evidence", ledger)

    def test_pyeight_can_use_fallback_decision(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            decision = root / "ops/autonomy/decisions/pyeight_smoke_failure-test.json"
            decision.write_text(json.dumps({"status": "fallback_accepted", "action": "oura_only_v1"}), encoding="utf-8")
            report = collect_pyeight(root)
            self.assertEqual(report["status"], "fallback_accepted")

    def test_tracked_lock_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / ".gitignore").write_text("data/\nprivate/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n", encoding="utf-8")
            lock = root / "ops/autonomy/.autokeel.lock"
            lock.parent.mkdir(parents=True)
            lock.write_text("123\n", encoding="utf-8")
            subprocess.run(["git", "add", "-f", "ops/autonomy/.autokeel.lock"], cwd=root, check=True)
            report = check_no_tracked_data(root)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any(".autokeel.lock" in error for error in report["errors"]))

    def test_manual_approval_language_is_global_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "playbook.md"
            path.write_text(
                """# Playbook

This requires manual approval.

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|
| 01 | Update code | src/app.py | src/app.py | true | python -m pytest tests -q | tests pass | none | none |
""",
                encoding="utf-8",
            )
            policy = Path(temp) / "policy.yaml"
            policy.write_text("playbook_validation:\n  banned_language:\n    - manual approval\n", encoding="utf-8")
            report = validate_playbook(path, policy_path=policy)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("manual approval" in error for error in report["errors"]))

    def test_verify_slice_rejects_non_allowlisted_acceptance_command(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_fixture(root)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            slices = [
                {
                    "id": "S01",
                    "required": True,
                    "status": "pending",
                    "playbook": "docs/playbooks/s01.md",
                    "acceptance": ["bash scripts/unsafe.sh"],
                    "review_artifacts": [],
                    "deliverables": [],
                }
            ]
            write_json_atomic(root / "ops/autonomy/slices.json", slices)
            playbook = root / "docs/playbooks/s01.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text(
                """| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|
| 01 | Check | scripts/check.py | scripts/check.py | true | python scripts/check.py | check passes | none | none |
""",
                encoding="utf-8",
            )
            report = verify_slice(root, "S01", dry_run=True)
            self.assertEqual(report["status"], "error")
            self.assertTrue(any("not allowlisted" in error for error in report["errors"]))


if __name__ == "__main__":
    unittest.main()

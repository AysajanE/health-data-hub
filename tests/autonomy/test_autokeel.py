from __future__ import annotations

import json
import stat
import shutil
import subprocess
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from ops.autonomy.autokeel import AutoKeel, CommandResult, CommandRunner, PolicyError, write_json_atomic
from scripts.autokeel_row_author import row_for_card


ROOT = Path(__file__).resolve().parents[2]


def copy_autonomy_fixture(dst: Path) -> None:
    shutil.copytree(ROOT / "ops", dst / "ops")
    shutil.copytree(ROOT / "scripts", dst / "scripts")
    (dst / ".gitignore").write_text("data/\nprivate/\n.env\n.local/\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n", encoding="utf-8")
    for rel in ("ops/autonomy/failures/archived_playbooks", "ops/autonomy/failures/archived_autoplans", "ops/autonomy/heartbeats"):
        shutil.rmtree(dst / rel, ignore_errors=True)
    slices_path = dst / "ops/autonomy/slices.json"
    slices = json.loads(slices_path.read_text(encoding="utf-8"))
    for item in slices:
        item["status"] = "pending"
        item.pop("retry_count", None)
        item.pop("failure_path", None)
        item.pop("run_id", None)
    write_json_atomic(slices_path, slices)
    write_json_atomic(
        dst / "ops/autonomy/autonomy_state.json",
        {"active_run": None, "completed_slices": [], "current_slice": None, "last_event_id": 0, "v1_complete": False},
    )
    (dst / "ops/autonomy/failure_ledger.jsonl").write_text("", encoding="utf-8")


def configure_fake_po_root(root: Path) -> Path:
    po_root = root / "keel" / "tools" / "plan-orchestrator"
    (po_root / "automation" / "plan_orchestrator").mkdir(parents=True)
    (po_root / "automation" / "run_plan_orchestrator.py").write_text("# runner\n", encoding="utf-8")
    policy = root / "ops/autonomy/policy.yaml"
    policy.write_text(
        "mode: autonomous_zero_human\n"
        f"keel_root: {root / 'keel'}\n"
        f"plan_orchestrator_root: {po_root}\n"
        "manual_gates:\n"
        "  forbidden_commands:\n"
        "    - mark-manual-gate\n",
        encoding="utf-8",
    )
    return po_root


def init_git_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)


class AutoKeelTests(unittest.TestCase):
    def test_command_runner_blocks_manual_gate_command(self) -> None:
        policy = {"manual_gates": {"forbidden_commands": ["keel-run mark-manual-gate", "mark-manual-gate"]}}
        runner = CommandRunner(ROOT, policy, dry_run=True)
        with self.assertRaises(PolicyError):
            runner.run(["keel-run", "mark-manual-gate", "--run-id", "run_1"])

    def test_log_event_redacts_secret_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            token_key = "MOOD_" + "TOKEN"
            token_value = "supersecret" + "value123456"
            access_key = "access_" + "token"
            access_value = "abc123" + "45678901234567890"
            op.log_event("secret_test", {"message": f"{token_key}={token_value}", access_key: access_value}, slice_id="S01")
            content = (root / "ops/autonomy/events.jsonl").read_text(encoding="utf-8")
            self.assertNotIn(token_value, content)
            self.assertNotIn(access_value, content)
            self.assertIn("[REDACTED]", content)

    def test_log_event_uses_event_log_high_water_mark(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            state_path = root / "ops/autonomy/autonomy_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["last_event_id"] = 3
            write_json_atomic(state_path, state)
            events_path = root / "ops/autonomy/events.jsonl"
            events_path.write_text(json.dumps({"event_id": 9, "event": "prior"}) + "\n", encoding="utf-8")

            op = AutoKeel(root=root, dry_run=True)
            event = op.log_event("after_prior", {"ok": True}, slice_id="S01")

            self.assertEqual(event["event_id"], 10)
            self.assertEqual(op.load_state()["last_event_id"], 10)

    def test_choose_next_slice_skips_complete_required_slices(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            slices_path = root / "ops/autonomy/slices.json"
            slices = json.loads(slices_path.read_text(encoding="utf-8"))
            slices[0]["status"] = "complete"
            slices[0]["run_id"] = "run_s01"
            write_json_atomic(slices_path, slices)
            op = AutoKeel(root=root, dry_run=True)
            self.assertEqual(op.choose_next_slice()["id"], "S02")

    def test_autoplan_validation_rejects_assistant_wrapper_without_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            slice_ = op.load_slices()[0]
            text = """The write wasn't approved. Here is the autoplan:

```markdown
# S01 Warehouse Foundation

Deliverables: schema.
Verification: tests.
Manual gates are forbidden.

Let me know if you want changes.
```
"""
            errors = op.validate_autoplan_text(slice_, text)
            self.assertIn("autoplan contains assistant wrapper/refusal text", errors)
            self.assertIn("autoplan missing Implementation Tasks section", errors)
            self.assertIn("autoplan missing compiler-parseable Files fields", errors)
            self.assertIn("autoplan missing compiler-parseable Verify fields", errors)

    def test_autoplan_validation_accepts_compiler_parseable_tasks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            slice_ = op.load_slices()[0]
            text = """# S01 Autoplan

Deliverables and verification are listed below.
Manual gates are forbidden.

## Implementation Tasks

- [ ] Add warehouse schema.
  Files: `src/db/schema.sql`; `tests/warehouse/test_schema.py`
  Verify: `python -m pytest tests/warehouse/test_schema.py -q`
"""
            self.assertEqual(op.validate_autoplan_text(slice_, text), [])

    def test_plan_orchestrator_root_matches_keel_tool_layout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            policy = root / "ops/autonomy/policy.yaml"
            policy.write_text(
                "mode: autonomous_zero_human\n"
                "keel_root: /tmp/keel\n"
                "manual_gates:\n"
                "  forbidden_commands:\n"
                "    - mark-manual-gate\n",
                encoding="utf-8",
            )

            op = AutoKeel(root=root, dry_run=True)

            self.assertEqual(op.plan_orchestrator_root(), "/tmp/keel/tools/plan-orchestrator")

    def test_po_product_shim_points_to_configured_tool_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            po_root = configure_fake_po_root(root)

            op = AutoKeel(root=root, dry_run=True)
            runner = op.ensure_plan_orchestrator_product_shim()

            self.assertEqual(runner.resolve(), (po_root / "automation" / "run_plan_orchestrator.py").resolve())
            self.assertEqual(
                (root / "automation" / "plan_orchestrator").resolve(),
                (po_root / "automation" / "plan_orchestrator").resolve(),
            )

    def test_po_start_uses_product_local_runner_not_keel_run_wrapper(self) -> None:
        seen: dict[str, object] = {}

        class CapturingRunner:
            def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                seen["argv"] = list(argv)
                seen["cwd"] = cwd
                seen["env"] = dict(env or {})
                seen["timeout"] = timeout
                return CommandResult(list(argv), 0, '{"run_id": "RUN_TEST"}', "")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            configure_fake_po_root(root)
            playbook = root / "docs/playbooks/s01-warehouse.playbook.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text("playbook", encoding="utf-8")
            op = AutoKeel(root=root, dry_run=False)
            op.runner = CapturingRunner()

            result = op.start_or_resume_po(op.load_slices()[0])

            self.assertTrue(result.ok)
            argv = seen["argv"]
            self.assertEqual(Path(argv[1]).resolve(), (root / "automation" / "run_plan_orchestrator.py").resolve())
            self.assertNotIn("keel-run", " ".join(argv))
            self.assertIn("--max-wait-seconds", argv)
            self.assertEqual(argv[argv.index("--max-wait-seconds") + 1], "5")
            self.assertIn("--max-auto-resume-attempts", argv)
            self.assertEqual(argv[argv.index("--max-auto-resume-attempts") + 1], "0")
            self.assertEqual(Path(seen["cwd"]).resolve(), root.resolve())
            self.assertEqual(seen["env"], {"PLAN_ORCHESTRATOR_CLEAN_ENV_CONFIRMED": "1"})
            self.assertEqual(seen["timeout"], 7200)
            self.assertEqual(op.load_state()["active_run"]["run_id"], "RUN_TEST")

    def test_pre_po_contract_runs_real_po_list_items_and_doctor(self) -> None:
        calls: list[tuple[list[str], dict[str, str]]] = []

        class ContractRunner:
            def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                calls.append((list(argv), dict(env or {})))
                joined = " ".join(str(part) for part in argv)
                if "list-items" in joined or "doctor" in joined:
                    return CommandResult(list(argv), 0, '{"status":"ok"}', "")
                return CommandResult(list(argv), 0, '{"run_id": "RUN_TEST"}', "")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            configure_fake_po_root(root)
            playbook = root / "docs/playbooks/s01-warehouse.playbook.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text("playbook", encoding="utf-8")
            op = AutoKeel(root=root, dry_run=False)
            op.runner = ContractRunner()

            result = op.start_or_resume_po(op.load_slices()[0])

            self.assertTrue(result.ok)
            joined_calls = [" ".join(call) for call, _env in calls]
            self.assertTrue(any("list-items" in call for call in joined_calls))
            self.assertTrue(any("doctor" in call for call in joined_calls))
            self.assertTrue(any("supervise run" in call for call in joined_calls))
            contract_env = {"PLAN_ORCHESTRATOR_CLEAN_ENV_CONFIRMED": "1"}
            contract_envs = [
                env
                for call, env in calls
                if "list-items" in " ".join(call) or "doctor" in " ".join(call)
            ]
            self.assertEqual(contract_envs, [contract_env, contract_env])

    def test_po_start_checkpoints_allowed_changes_before_contract_doctor(self) -> None:
        statuses: dict[str, str] = {}
        envs: dict[str, dict[str, str]] = {}

        class CleanCheckoutRunner:
            def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                joined = " ".join(str(part) for part in argv)
                stage = None
                if "list-items" in joined:
                    stage = "list-items"
                elif "doctor" in joined:
                    stage = "doctor"
                elif "supervise run" in joined:
                    stage = "supervise"

                if stage:
                    status = subprocess.run(
                        ["git", "status", "--short"],
                        cwd=root,
                        text=True,
                        capture_output=True,
                        check=True,
                    ).stdout
                    statuses[stage] = status
                    envs[stage] = dict(env or {})
                    if status:
                        return CommandResult(list(argv), 99, "", f"{stage} saw dirty checkout:\n{status}")

                if stage in {"list-items", "doctor"}:
                    return CommandResult(list(argv), 0, '{"status":"ok"}', "")
                return CommandResult(list(argv), 0, '{"run_id": "RUN_TEST"}', "")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            configure_fake_po_root(root)
            bootstrap = AutoKeel(root=root, dry_run=False)
            bootstrap.ensure_plan_orchestrator_product_shim()
            init_git_repo(root)
            playbook = root / "docs/playbooks/s01-warehouse.playbook.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text("playbook", encoding="utf-8")
            op = AutoKeel(root=root, dry_run=False)
            op.log_event("dirty_before_contract", {"ok": True}, slice_id="S01")
            op.runner = CleanCheckoutRunner()

            result = op.start_or_resume_po(op.load_slices()[0])

            self.assertTrue(result.ok, result.stderr)
            self.assertEqual(statuses["list-items"], "")
            self.assertEqual(statuses["doctor"], "")
            self.assertEqual(statuses["supervise"], "")
            expected_env = {"PLAN_ORCHESTRATOR_CLEAN_ENV_CONFIRMED": "1"}
            self.assertEqual(envs["list-items"], expected_env)
            self.assertEqual(envs["doctor"], expected_env)
            self.assertEqual(envs["supervise"], expected_env)

    def test_pre_po_contract_rejects_po_doctor_failure(self) -> None:
        class ContractRejectRunner:
            def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                joined = " ".join(str(part) for part in argv)
                if "list-items" in joined:
                    return CommandResult(list(argv), 0, '{"status":"ok"}', "")
                if "doctor" in joined:
                    return CommandResult(list(argv), 2, "", "normalization failed")
                raise AssertionError("PO supervise must not start after doctor failure")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            configure_fake_po_root(root)
            playbook = root / "docs/playbooks/s01-warehouse.playbook.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text("playbook", encoding="utf-8")
            op = AutoKeel(root=root, dry_run=False)
            op.runner = ContractRejectRunner()

            result = op.start_or_resume_po(op.load_slices()[0])

            self.assertEqual(result.exit_code, 2)
            self.assertIn("normalization failed", result.stderr)
            updated = op.load_slices()[0]
            self.assertEqual(updated["status"], "blocked_compile_inputs")

    def test_active_same_slice_run_invokes_supervise_resume(self) -> None:
        seen: dict[str, object] = {}

        class CapturingRunner:
            def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                seen["argv"] = list(argv)
                seen["cwd"] = cwd
                seen["env"] = dict(env or {})
                seen["timeout"] = timeout
                return CommandResult(list(argv), 0, '{"run_id": "RUN_TEST"}', "")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            configure_fake_po_root(root)
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            state["active_run"] = {
                "slice": "S01",
                "run_id": "RUN_TEST",
                "started_at": now,
                "last_seen_at": now,
            }
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", state)
            op = AutoKeel(root=root, dry_run=False)
            op.runner = CapturingRunner()

            result = op.start_or_resume_po(op.load_slices()[0])

            self.assertTrue(result.ok)
            argv = seen["argv"]
            self.assertEqual(argv[2:4], ["supervise", "resume"])
            self.assertIn("--run-id", argv)
            self.assertEqual(argv[argv.index("--run-id") + 1], "RUN_TEST")
            self.assertIn("--max-wait-seconds", argv)
            self.assertIn("--max-auto-resume-attempts", argv)
            self.assertEqual(argv[argv.index("--max-auto-resume-attempts") + 1], "0")
            self.assertEqual(seen["env"], {"PLAN_ORCHESTRATOR_CLEAN_ENV_CONFIRMED": "1"})
            self.assertEqual(seen["timeout"], 7200)
            active = op.load_state()["active_run"]
            self.assertEqual(active["run_id"], "RUN_TEST")
            self.assertIn("last_seen_at", active)

    def test_escalated_active_run_requires_closed_audit_failure(self) -> None:
        calls: list[list[str]] = []

        class CapturingRunner:
            def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                calls.append(list(argv))
                if list(argv)[:3] == ["python", "-m", "scripts.keel_status_digest"]:
                    return CommandResult(list(argv), 0, '{"terminal_state": "escalated"}', "")
                raise AssertionError("resume command should not run while audit_failure is open")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            configure_fake_po_root(root)
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            state["active_run"] = {
                "slice": "S01",
                "run_id": "RUN_TEST",
                "started_at": now,
                "last_seen_at": now,
            }
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", state)
            failure = {
                "ts": now,
                "slice": "S01",
                "run_id": "RUN_TEST",
                "failure_class": "audit_failure",
                "severity": "high",
                "description": "PO escalated the slice.",
                "action_taken": "Recorded escalation for root-cause diagnosis.",
                "evidence_path": "ops/autonomy/failures/S01-audit_failure.md",
                "open": True,
            }
            (root / "ops/autonomy/failure_ledger.jsonl").write_text(json.dumps(failure) + "\n", encoding="utf-8")
            op = AutoKeel(root=root, dry_run=False)
            op.runner = CapturingRunner()

            result = op.start_or_resume_po(op.load_slices()[0])

            self.assertEqual(result.exit_code, 54)
            self.assertIn("audit_failure remains open", result.stderr)
            self.assertEqual(len(calls), 1)
            events = (root / "ops/autonomy/events.jsonl").read_text(encoding="utf-8")
            self.assertIn("po_escalated_resume_blocked_open_failure", events)

    def test_closed_escalated_audit_failure_allows_one_repaired_resume(self) -> None:
        seen: dict[str, object] = {}

        class CapturingRunner:
            def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                if list(argv)[:3] == ["python", "-m", "scripts.keel_status_digest"]:
                    return CommandResult(list(argv), 0, '{"terminal_state": "escalated"}', "")
                seen["argv"] = list(argv)
                seen["cwd"] = cwd
                seen["env"] = dict(env or {})
                seen["timeout"] = timeout
                return CommandResult(list(argv), 0, '{"run_id": "RUN_TEST"}', "")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            configure_fake_po_root(root)
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            state["active_run"] = {
                "slice": "S01",
                "run_id": "RUN_TEST",
                "started_at": now,
                "last_seen_at": now,
            }
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", state)
            failure = {
                "ts": now,
                "slice": "S01",
                "run_id": "RUN_TEST",
                "failure_class": "audit_failure",
                "severity": "high",
                "description": "PO escalated the slice.",
                "action_taken": "Recorded escalation for root-cause diagnosis.",
                "evidence_path": "ops/autonomy/failures/S01-audit_failure.md",
                "open": False,
                "closure_evidence": "docs/evidence/root-cause.md",
                "closure_note": "Root cause fixed.",
            }
            (root / "ops/autonomy/failure_ledger.jsonl").write_text(json.dumps(failure) + "\n", encoding="utf-8")
            op = AutoKeel(root=root, dry_run=False)
            op.runner = CapturingRunner()

            result = op.start_or_resume_po(op.load_slices()[0])

            self.assertTrue(result.ok)
            argv = seen["argv"]
            self.assertEqual(argv[2:4], ["supervise", "resume"])
            self.assertIn("--max-auto-resume-attempts", argv)
            self.assertEqual(argv[argv.index("--max-auto-resume-attempts") + 1], "1")
            events = (root / "ops/autonomy/events.jsonl").read_text(encoding="utf-8")
            self.assertIn("po_escalated_resume_after_repair_authorized", events)

    def test_superseded_active_run_snapshot_starts_new_po_run(self) -> None:
        seen: dict[str, object] = {}

        class CapturingRunner:
            def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                seen["argv"] = list(argv)
                return CommandResult(list(argv), 0, '{"run_id": "RUN_NEW"}', "")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            configure_fake_po_root(root)
            playbook = root / "docs/playbooks/s01-warehouse.playbook.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text("current playbook", encoding="utf-8")
            run_root = root / ".local/automation/plan_orchestrator/runs/RUN_OLD"
            run_root.mkdir(parents=True)
            write_json_atomic(run_root / "run_state.json", {"playbook_source_sha256": "old-sha"})
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            state["active_run"] = {
                "slice": "S01",
                "run_id": "RUN_OLD",
                "started_at": now,
                "last_seen_at": now,
            }
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", state)
            op = AutoKeel(root=root, dry_run=False)
            op.runner = CapturingRunner()

            result = op.start_or_resume_po(op.load_slices()[0])

            self.assertTrue(result.ok)
            self.assertEqual(seen["argv"][2:4], ["supervise", "run"])
            self.assertEqual(op.load_state()["active_run"]["run_id"], "RUN_NEW")
            ledger = (root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn("state_divergence", ledger)
            self.assertIn("superseded playbook snapshot", ledger)

    def test_active_same_slice_resume_rejects_dirty_product_changes(self) -> None:
        class ShouldNotRun:
            def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                raise AssertionError("resume command should not run after dirty precheck")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            configure_fake_po_root(root)
            init_git_repo(root)
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            now = datetime.now().astimezone().isoformat(timespec="seconds")
            state["active_run"] = {
                "slice": "S01",
                "run_id": "RUN_TEST",
                "started_at": now,
                "last_seen_at": now,
            }
            write_json_atomic(root / "ops/autonomy/autonomy_state.json", state)
            (root / "src").mkdir(exist_ok=True)
            (root / "src" / "unexpected.py").write_text("print('dirty')\n", encoding="utf-8")
            op = AutoKeel(root=root, dry_run=False)
            op.runner = ShouldNotRun()

            result = op.start_or_resume_po(op.load_slices()[0])

            self.assertFalse(result.ok)
            self.assertIn("non-AutoKeel dirty paths", result.stderr)
            self.assertIn("src/unexpected.py", result.stderr)

    def test_heartbeat_writes_ignored_runtime_file_without_tracked_state_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            state_before = (root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8")
            events_before = (root / "ops/autonomy/events.jsonl").read_text(encoding="utf-8")

            op.log_heartbeat()

            self.assertEqual((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"), state_before)
            self.assertEqual((root / "ops/autonomy/events.jsonl").read_text(encoding="utf-8"), events_before)
            self.assertTrue((root / "ops/autonomy/heartbeats/latest.json").exists())

    def test_pre_po_checkpoint_commits_only_autokeel_runtime_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            init_git_repo(root)
            op = AutoKeel(root=root, dry_run=False)
            op.log_event("checkpoint_test", {"ok": True}, slice_id="S01")

            result = op.checkpoint_allowed_pre_po_changes("S01")

            self.assertTrue(result.ok, result.stderr)
            status = subprocess.run(
                ["git", "status", "--short"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            self.assertEqual(status, "")
            log = subprocess.run(
                ["git", "log", "--oneline", "-1"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout
            self.assertIn("Record AutoKeel S01 pre-PO state", log)

    def test_pre_po_checkpoint_rejects_product_code_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            init_git_repo(root)
            (root / "src").mkdir()
            (root / "src" / "unexpected.py").write_text("print('no')\n", encoding="utf-8")
            op = AutoKeel(root=root, dry_run=False)

            result = op.checkpoint_allowed_pre_po_changes("S01")

            self.assertFalse(result.ok)
            self.assertIn("non-AutoKeel dirty paths", result.stderr)
            self.assertIn("src/unexpected.py", result.stderr)

    def test_ship_slice_rejects_dirty_product_changes_before_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            init_git_repo(root)
            base_branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            subprocess.run(["git", "checkout", "-b", "orchestrator/run/RUN_TEST"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / "run.txt").write_text("run branch\n", encoding="utf-8")
            subprocess.run(["git", "add", "run.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "run branch"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            subprocess.run(["git", "checkout", base_branch], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / "src").mkdir()
            (root / "src" / "unexpected.py").write_text("print('no')\n", encoding="utf-8")
            op = AutoKeel(root=root, dry_run=False)

            result = op.ship_slice("S01", "RUN_TEST")

            self.assertFalse(result.ok)
            self.assertIn("non-AutoKeel dirty paths", result.stderr)
            branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            self.assertEqual(branch, base_branch)

    def test_ship_slice_uses_run_state_branch_without_switching_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            init_git_repo(root)
            base_branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            subprocess.run(["git", "checkout", "-b", "orchestrator/run/RUN_TEST"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / "run.txt").write_text("stale run branch\n", encoding="utf-8")
            subprocess.run(["git", "add", "run.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "stale run branch"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            stale_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()

            subprocess.run(["git", "checkout", base_branch], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            subprocess.run(["git", "checkout", "-b", "orchestrator/run-refresh/RUN_TEST/1"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / "run.txt").write_text("refreshed run branch\n", encoding="utf-8")
            subprocess.run(["git", "add", "run.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "refreshed run branch"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            refreshed_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()

            run_state_dir = root / ".local/automation/plan_orchestrator/runs/RUN_TEST"
            run_state_dir.mkdir(parents=True)
            write_json_atomic(run_state_dir / "run_state.json", {"run_branch_name": "orchestrator/run-refresh/RUN_TEST/1"})
            subprocess.run(["git", "checkout", base_branch], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            op = AutoKeel(root=root, dry_run=False)

            result = op.ship_slice("S01", "RUN_TEST")

            self.assertTrue(result.ok, result.stderr)
            ship_head = subprocess.run(["git", "rev-parse", "ship/s01"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            branch = subprocess.run(["git", "branch", "--show-current"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            self.assertEqual(ship_head, refreshed_head)
            self.assertNotEqual(ship_head, stale_head)
            self.assertEqual(branch, base_branch)

    def test_passed_po_validates_shipped_branch_not_operator_checkout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            (root / "scripts" / "check_autonomous_review_exists.py").write_text(
                "from pathlib import Path\nimport sys\nsys.exit(0 if Path('ship_only_review').exists() else 9)\n",
                encoding="utf-8",
            )
            (root / "scripts" / "verify_slice.py").write_text(
                "from pathlib import Path\nimport sys\nsys.exit(0 if Path('ship_only_verify').exists() else 8)\n",
                encoding="utf-8",
            )
            init_git_repo(root)
            base_branch = subprocess.run(
                ["git", "branch", "--show-current"],
                cwd=root,
                text=True,
                capture_output=True,
                check=True,
            ).stdout.strip()
            subprocess.run(["git", "checkout", "-b", "orchestrator/run/RUN_TEST"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            (root / "ship_only_review").write_text("review exists only on ship branch\n", encoding="utf-8")
            (root / "ship_only_verify").write_text("acceptance exists only on ship branch\n", encoding="utf-8")
            subprocess.run(["git", "add", "ship_only_review", "ship_only_verify"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "ship-only gates"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            run_head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            subprocess.run(["git", "checkout", base_branch], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            op = AutoKeel(root=root, dry_run=False)

            result = op.handle_po_status("S01", "RUN_TEST", {"terminal_state": "passed"})

            self.assertEqual(result, "complete")
            self.assertFalse((root / "ship_only_review").exists())
            self.assertFalse((root / "ship_only_verify").exists())
            state = op.load_state()
            self.assertIn("S01", state["completed_slices"])
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            self.assertEqual(slices[0]["ship_commit"], run_head)
            branch = subprocess.run(["git", "branch", "--show-current"], cwd=root, text=True, capture_output=True, check=True).stdout.strip()
            self.assertEqual(branch, base_branch)

    def test_run_once_recovers_passed_run_before_recompile(self) -> None:
        class PreflightRunner:
            def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                if list(argv)[:3] == ["python", "-m", "scripts.verify_autokeel_invariants"]:
                    return CommandResult(list(argv), 0, '{"status": "ok"}', "")
                if list(argv)[:3] == ["python", "-m", "scripts.verify_v1"]:
                    return CommandResult(list(argv), 1, '{"status": "error"}', "")
                if list(argv)[:3] == ["python", "-m", "scripts.evaluate_tripwires"]:
                    return CommandResult(list(argv), 0, '{"status": "ok"}', "")
                raise AssertionError(f"unexpected command before terminal recovery: {argv}")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            slices_path = root / "ops/autonomy/slices.json"
            slices = json.loads(slices_path.read_text(encoding="utf-8"))
            slices[0]["status"] = "replan_required"
            slices[0]["run_id"] = "RUN_TEST"
            write_json_atomic(slices_path, slices)
            op = AutoKeel(root=root, dry_run=False)
            op.runner = PreflightRunner()
            handled: list[tuple[str, str, dict[str, str]]] = []

            def recover(slice_):
                return CommandResult([], 0, '{"run_id": "RUN_TEST", "terminal_state": "passed"}', "")

            def inspect(run_id):
                return {"terminal_state": "passed"}

            def handle(slice_id, run_id, status):
                handled.append((slice_id, run_id, status))
                return "complete"

            def should_not_compile(slice_):
                raise AssertionError("terminal recovery must run before lane/playbook compilation")

            op.recover_passed_slice_run = recover
            op.inspect_po_status = inspect
            op.handle_po_status = handle
            op.ensure_lane_decision = should_not_compile
            op.ensure_playbook = should_not_compile

            result = op.run_once(requested_slice="S01")

            self.assertEqual(result, 0)
            self.assertEqual(handled, [("S01", "RUN_TEST", {"terminal_state": "passed"})])

    def test_run_once_runs_end_invariants_after_early_return(self) -> None:
        class TripwireFailureRunner:
            def __init__(self):
                self.invariant_calls = 0

            def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                if list(argv)[:3] == ["python", "-m", "scripts.verify_autokeel_invariants"]:
                    self.invariant_calls += 1
                    return CommandResult(list(argv), 0, '{"status": "ok"}', "")
                if list(argv)[:3] == ["python", "-m", "scripts.verify_v1"]:
                    return CommandResult(list(argv), 1, '{"status": "error"}', "")
                if list(argv)[:3] == ["python", "-m", "scripts.evaluate_tripwires"]:
                    return CommandResult(list(argv), 1, '{"status": "error", "fired": ["tripwire"]}', "")
                raise AssertionError(f"unexpected command: {argv}")

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=False)
            runner = TripwireFailureRunner()
            op.runner = runner

            result = op.run_once(requested_slice="S01")

            self.assertEqual(result, 6)
            self.assertEqual(runner.invariant_calls, 2)

    def test_row_author_keeps_verification_for_artifact_tasks(self) -> None:
        row = row_for_card(
            {
                "task_id": "task_006",
                "phase": "Autonomous schema review",
                "task": "Generate the autonomous review artifact.",
                "declared_deliverables": ["docs/reviews/s01-autonomous-schema-review.md"],
                "clamped_allowed_write_roots": ["docs/reviews"],
                "verification_candidates": ["python scripts/check_autonomous_review_exists.py S01"],
                "behavioral": False,
            },
            6,
        )

        self.assertTrue(row["requires_red_green"])
        self.assertEqual(row["required_verification_commands"], ["python scripts/check_autonomous_review_exists.py S01"])
        self.assertEqual(row["required_verification_artifacts"], [])

    def test_row_author_preserves_task_notes_in_exit_criteria(self) -> None:
        row = row_for_card(
            {
                "task_id": "task_001",
                "phase": "Warehouse schema",
                "task": "Author the canonical DuckDB schema.",
                "declared_deliverables": ["src/db/schema.sql", "tests/warehouse/test_schema.py"],
                "clamped_allowed_write_roots": ["src/db", "tests/warehouse"],
                "verification_candidates": ["python -m pytest tests/warehouse/test_schema.py -q"],
                "notes": [
                    "Include a bounded test that opens an in-memory DuckDB database, executes `src/db/schema.sql`, and asserts the five expected tables exist."
                ],
                "behavioral": True,
            },
            1,
        )

        self.assertIn("in-memory DuckDB", row["exit_criteria"])
        self.assertTrue(any("in-memory DuckDB" in note for note in row["notes"]))

    def test_row_author_preserves_string_task_note(self) -> None:
        row = row_for_card(
            {
                "task_id": "task_001",
                "phase": "Warehouse schema",
                "task": "Author the canonical DuckDB schema.",
                "declared_deliverables": ["src/db/schema.sql"],
                "clamped_allowed_write_roots": ["src/db"],
                "verification_candidates": ["python scripts/check_schema_contract.py"],
                "notes": "Include a bounded test that opens an in-memory DuckDB database.",
                "behavioral": True,
            },
            1,
        )

        self.assertIn("in-memory DuckDB", row["exit_criteria"])
        self.assertNotIn("I; n; c; l; u; d; e", row["exit_criteria"])

    def test_extract_run_id_ignores_run_state_path(self) -> None:
        text = "missing runs/RUN_20260524T182654Z_599d47fbb3784447bbb2386ea88ad935/run_state.json"
        self.assertEqual(
            AutoKeel._extract_run_id(text),
            "RUN_20260524T182654Z_599d47fbb3784447bbb2386ea88ad935",
        )
        self.assertIsNone(AutoKeel._extract_run_id("missing run_state.json"))

    def test_failed_po_start_does_not_persist_active_run(self) -> None:
        class FailingRunner:
            def run(self, argv, cwd=None, env=None, execute_in_dry_run=False, timeout=None):
                return CommandResult(
                    list(argv),
                    1,
                    "",
                    "ERROR: missing runs/RUN_20260524T182654Z_599d47fbb3784447bbb2386ea88ad935/run_state.json",
                )

        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            playbook = root / "docs/playbooks/s01-warehouse.playbook.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text("playbook", encoding="utf-8")
            configure_fake_po_root(root)
            op = AutoKeel(root=root, dry_run=False)
            op.runner = FailingRunner()
            result = op.start_or_resume_po(op.load_slices()[0])

            self.assertFalse(result.ok)
            self.assertIsNone(op.load_state().get("active_run"))

    def test_dry_run_once_restores_tracked_state_and_skips_po_start(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            playbook = root / "docs/playbooks/s01-warehouse.playbook.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text(
                """# Playbook

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 01 | Add schema test | `tests/warehouse/test_schema.py` | tests/warehouse | true | python -m pytest tests/warehouse/test_schema.py -q | tests pass | none | none |
""",
                encoding="utf-8",
            )
            tracked = [
                root / "ops/autonomy/autonomy_state.json",
                root / "ops/autonomy/events.jsonl",
                root / "ops/autonomy/failure_ledger.jsonl",
                root / "ops/autonomy/progress.md",
                root / "ops/autonomy/slices.json",
            ]
            before = {path: path.read_bytes() for path in tracked}

            op = AutoKeel(root=root, dry_run=True)
            result = op.run_once(requested_slice="S01")

            self.assertEqual(result, 0)
            self.assertEqual({path: path.read_bytes() for path in tracked}, before)

    def test_dry_run_replan_does_not_archive_playbook(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            playbook = root / "docs/playbooks/s01-warehouse.playbook.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text(
                """# Playbook

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 01 | Add schema test | `tests/warehouse/test_schema.py` | tests/warehouse | true | python -m pytest tests/warehouse/test_schema.py -q | tests pass | none | none |
""",
                encoding="utf-8",
            )
            slices_path = root / "ops/autonomy/slices.json"
            slices = json.loads(slices_path.read_text(encoding="utf-8"))
            slices[0]["status"] = "replan_required"
            write_json_atomic(slices_path, slices)

            op = AutoKeel(root=root, dry_run=True)
            result = op.run_once(requested_slice="S01")

            self.assertEqual(result, 0)
            self.assertTrue(playbook.exists())
            self.assertFalse((root / "ops/autonomy/failures/archived_playbooks").exists())

    def test_awaiting_human_gate_records_manual_gate_leak(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            result = op.handle_po_status("S01", "run_test", {"terminal_state": "awaiting_human_gate"})
            self.assertEqual(result, "manual_gate_leak")
            ledger = (root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn("manual_gate_leak", ledger)
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            self.assertEqual(slices[0]["status"], "replan_required")

    def test_escalated_po_keeps_active_run_for_supervised_resume(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            state_path = root / "ops/autonomy/autonomy_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["active_run"] = {"slice": "S01", "run_id": "run_escalated", "started_at": "2026-05-27T00:00:00-04:00"}
            write_json_atomic(state_path, state)

            op = AutoKeel(root=root, dry_run=True)
            result = op.handle_po_status("S01", "run_escalated", {"terminal_state": "escalated"})

            self.assertEqual(result, "escalated")
            ledger = (root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8")
            self.assertIn("audit_failure", ledger)
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            self.assertEqual(slices[0]["status"], "pending")
            updated_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(updated_state["active_run"]["run_id"], "run_escalated")

    def test_blocked_external_creates_local_evidence_request(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            op = AutoKeel(root=root, dry_run=True)
            result = op.handle_po_status("S03", "run_ext", {"terminal_state": "blocked_external"})
            self.assertEqual(result, "blocked_external")
            evidence_roots = list((root / "private/evidence/S03").glob("*"))
            self.assertEqual(len(evidence_roots), 1)
            readme = evidence_roots[0] / "README.md"
            self.assertTrue(readme.exists())
            self.assertEqual(stat.S_IMODE(evidence_roots[0].stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(readme.stat().st_mode), 0o600)

    def test_verify_v1_does_not_pass_without_real_deliverables(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
            for item in slices:
                item["status"] = "complete"
                item["acceptance"] = []
                item["review_artifacts"] = []
            write_json_atomic(root / "ops/autonomy/slices.json", slices)
            op = AutoKeel(root=root, dry_run=False)
            result = op.run_verify_v1()
            self.assertFalse(result.ok)
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            self.assertFalse(state.get("v1_complete", False))


if __name__ == "__main__":
    unittest.main()

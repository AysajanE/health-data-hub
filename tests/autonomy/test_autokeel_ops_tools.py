from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from types import SimpleNamespace
from datetime import date, datetime, timedelta
from pathlib import Path

import yaml

from ops.autonomy.autokeel import write_json_atomic
from scripts.close_failure import close_failure
from scripts.evaluate_tripwires import evaluate_tripwires
from scripts.record_intervention import cmd_append_ledger
from scripts.verify_autokeel_invariants import partition_open_high_failures
from scripts.verify_failure_ledger import verify_failure_ledger


ROOT = Path(__file__).resolve().parents[2]


def rewrite_tripwire_deadlines(
    root: Path,
    offsets_by_name: dict[str, int] | None = None,
    default_offset_days: int = 30,
) -> None:
    """Pin the fixture's tripwire deadlines relative to today.

    The fixture copies the real ops/autonomy/policy.yaml, whose deadlines are
    fixed calendar dates that eventually pass. Tests must encode intent
    (future deadlines do not fire; newest report wins) without ever expiring,
    so they rewrite every deadline relative to today. Positive offsets are
    future deadlines; negative offsets are already-past deadlines.
    """
    policy_path = root / "ops/autonomy/policy.yaml"
    policy = yaml.safe_load(policy_path.read_text(encoding="utf-8"))
    deadlines = policy.get("tripwire_deadlines") or {}
    for name, config in deadlines.items():
        if not isinstance(config, dict):
            continue
        offset = (offsets_by_name or {}).get(name, default_offset_days)
        config["date"] = (date.today() + timedelta(days=offset)).isoformat()
    policy_path.write_text(yaml.safe_dump(policy, sort_keys=False), encoding="utf-8")


def iso_days_ago(days: int) -> str:
    return (datetime.now().astimezone() - timedelta(days=days)).isoformat(timespec="seconds")


def copy_autonomy_fixture(dst: Path) -> None:
    shutil.copytree(ROOT / "ops", dst / "ops")
    # The real repo's state-digest sidecar must never leak into fixtures:
    # fixture roots hand-edit state, and the first fixture tick re-baselines.
    (dst / "ops/autonomy/state_digest.json").unlink(missing_ok=True)
    shutil.copytree(ROOT / "scripts", dst / "scripts")
    (dst / ".gitignore").write_text("data/\nprivate/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n", encoding="utf-8")
    for rel in ("ops/autonomy/failures/archived_playbooks", "ops/autonomy/failures/archived_autoplans", "ops/autonomy/heartbeats"):
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


class AutoKeelOpsToolTests(unittest.TestCase):
    def test_append_ledger_builds_valid_scoped_s12_authority_blocker(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            evidence_rel = "ops/autonomy/failures/S12-provider-terms-test.md"
            evidence = root / evidence_rel
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text("# S12 provider authority blocker\n", encoding="utf-8")
            slices_path = root / "ops/autonomy/slices.json"
            slices = json.loads(slices_path.read_text(encoding="utf-8"))
            s12 = next(item for item in slices if item.get("id") == "S12")
            s12["status"] = "blocked_external"
            s12["failure_path"] = evidence_rel
            write_json_atomic(slices_path, slices)
            source = root / "candidate.json"
            source.write_text(
                json.dumps(
                    {
                        "slice": "S12",
                        "failure_class": "provider_terms_conflict",
                        "severity": "high",
                        "description": "Current provider authority is unavailable.",
                        "action_taken": "Stopped before compiler or provider use.",
                        "evidence_path": evidence_rel,
                    }
                ),
                encoding="utf-8",
            )

            report = cmd_append_ledger(SimpleNamespace(root=str(root), source=str(source)))

            self.assertEqual(report["status"], "ok", report)
            rows = [
                json.loads(line)
                for line in (root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["schema_version"], "autokeel.failure_ledger.v2")
            self.assertEqual(rows[0]["failure_origin"], "external_provider")
            self.assertTrue(rows[0]["failure_id"].startswith("failure_"))
            self.assertEqual(
                rows[0]["evidence_sha256"],
                hashlib.sha256(evidence.read_bytes()).hexdigest(),
            )
            verified = verify_failure_ledger(root)
            self.assertEqual(verified["status"], "ok", verified)
            self.assertEqual(verified["scoped_external_blockers"], 1)

    def test_append_ledger_rejects_semantically_incomplete_v2_row(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            source = root / "candidate.json"
            source.write_text(
                json.dumps(
                    {
                        "slice": "S12",
                        "failure_class": "provider_terms_conflict",
                        "severity": "high",
                        "description": "Missing action and evidence.",
                    }
                ),
                encoding="utf-8",
            )

            report = cmd_append_ledger(SimpleNamespace(root=str(root), source=str(source)))

            self.assertEqual(report["status"], "error")
            self.assertIn("action_taken", report["errors"][0])
            self.assertEqual((root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8"), "")

    def test_append_ledger_rejects_forged_evidence_hash(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            evidence_rel = "ops/autonomy/failures/S12-provider-terms-test.md"
            evidence = root / evidence_rel
            evidence.parent.mkdir(parents=True, exist_ok=True)
            evidence.write_text("# S12 provider authority blocker\n", encoding="utf-8")
            source = root / "candidate.json"
            source.write_text(
                json.dumps(
                    {
                        "slice": "S12",
                        "failure_class": "provider_terms_conflict",
                        "severity": "high",
                        "description": "Current provider authority is unavailable.",
                        "action_taken": "Stopped before compiler or provider use.",
                        "evidence_path": evidence_rel,
                        "evidence_sha256": "0" * 64,
                    }
                ),
                encoding="utf-8",
            )

            report = cmd_append_ledger(SimpleNamespace(root=str(root), source=str(source)))

            self.assertEqual(report["status"], "error")
            self.assertTrue(any("evidence_sha256 mismatch" in error for error in report["errors"]))
            self.assertEqual((root / "ops/autonomy/failure_ledger.jsonl").read_text(encoding="utf-8"), "")

    def test_open_global_high_failure_is_never_scoped_external(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            (root / "ops/autonomy/failure_ledger.jsonl").write_text(
                json.dumps(
                    {
                        "schema_version": "autokeel.failure_ledger.v2",
                        "failure_id": "failure_global_test",
                        "ts": datetime.now().astimezone().isoformat(timespec="seconds"),
                        "slice": "GLOBAL",
                        "run_id": None,
                        "failure_class": "state_divergence",
                        "severity": "high",
                        "description": "Global state diverged.",
                        "action_taken": "Stopped.",
                        "evidence_path": "ops/autonomy/events.jsonl",
                        "evidence_sha256": None,
                        "root_cause_id": "GLOBAL-STATE-DIVERGENCE",
                        "failure_origin": "autokeel_wrapper",
                        "supersedes": [],
                        "superseded_by": None,
                        "false_positive": False,
                        "closure_validation_command": "",
                        "open": True,
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            report = verify_failure_ledger(root)

            self.assertEqual(report["status"], "error")
            self.assertEqual(report["scoped_external_blockers"], 0)
            self.assertTrue(any("open high/critical" in error for error in report["errors"]))

    def test_scoped_s12_authority_failure_does_not_become_global_s11_stop(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            evidence_rel = "ops/autonomy/failures/S12-provider-terms-test.md"
            evidence = root / evidence_rel
            evidence.parent.mkdir(parents=True)
            evidence.write_text("# real tracked authority blocker\n", encoding="utf-8")
            slices = [
                {"id": "S11", "status": "pending", "depends_on": ["S01", "S02"]},
                {
                    "id": "S12",
                    "status": "blocked_external",
                    "depends_on": ["S11"],
                    "failure_path": evidence_rel,
                },
            ]
            row = {
                "schema_version": "autokeel.failure_ledger.v2",
                "failure_id": "failure_s12_authority_test",
                "root_cause_id": "S12-OURA-PROVIDER-TERMS-AUTHORITY",
                "failure_origin": "external_provider",
                "slice": "S12",
                "failure_class": "provider_terms_conflict",
                "severity": "high",
                "open": True,
                "evidence_path": evidence_rel,
                "evidence_sha256": hashlib.sha256(evidence.read_bytes()).hexdigest(),
            }

            scoped, global_blockers = partition_open_high_failures(root, [row], slices)

            self.assertEqual(scoped, [row])
            self.assertEqual(global_blockers, [])

            forged = {**row, "evidence_sha256": "0" * 64}
            scoped, global_blockers = partition_open_high_failures(root, [forged], slices)
            self.assertEqual(scoped, [])
            self.assertEqual(global_blockers, [forged])

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

    def test_close_failure_uses_event_log_high_water_mark(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            evidence = root / "docs/reviews/closure.md"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("Verdict: pass\n", encoding="utf-8")
            state_path = root / "ops/autonomy/autonomy_state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state["last_event_id"] = 4
            write_json_atomic(state_path, state)
            events_path = root / "ops/autonomy/events.jsonl"
            events_path.write_text(json.dumps({"event_id": 11, "event": "prior"}) + "\n", encoding="utf-8")
            ledger = root / "ops/autonomy/failure_ledger.jsonl"
            ledger.write_text(
                json.dumps({"slice": "S01", "failure_class": "manual_gate_leak", "severity": "high", "open": True}) + "\n",
                encoding="utf-8",
            )

            report = close_failure(root, "S01", "manual_gate_leak", "docs/reviews/closure.md", "closed in test")

            self.assertEqual(report["status"], "ok")
            events = [json.loads(line) for line in events_path.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(events[-1]["event_id"], 12)
            self.assertEqual(json.loads(state_path.read_text(encoding="utf-8"))["last_event_id"], 12)

    def test_close_failure_refuses_ambiguous_slice_and_class(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            evidence = root / "docs/reviews/closure.md"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("Verdict: pass\n", encoding="utf-8")
            ledger = root / "ops/autonomy/failure_ledger.jsonl"
            ledger.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "slice": "GLOBAL",
                            "failure_class": "tripwire_triggered",
                            "root_cause_id": root_cause,
                            "severity": "high",
                            "open": True,
                        }
                    )
                    for root_cause in (
                        "GLOBAL-TRIPWIRE-ON-MOOD-TRANSPORT-FAILURE-WEEK-4",
                        "GLOBAL-TRIPWIRE-ON-MOOD-COMPLIANCE-FAILURE-WEEK-8",
                    )
                )
                + "\n",
                encoding="utf-8",
            )

            report = close_failure(
                root,
                "GLOBAL",
                "tripwire_triggered",
                "docs/reviews/closure.md",
                "closed in test",
            )

            self.assertEqual(report["status"], "error")
            self.assertIn("multiple open failures", report["errors"][0])
            rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(all(row["open"] for row in rows))

    def test_close_failure_targets_one_root_cause(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            evidence = root / "docs/reviews/closure.md"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("Verdict: pass\n", encoding="utf-8")
            target = "GLOBAL-TRIPWIRE-ON-MOOD-TRANSPORT-FAILURE-WEEK-4"
            other = "GLOBAL-TRIPWIRE-ON-MOOD-COMPLIANCE-FAILURE-WEEK-8"
            ledger = root / "ops/autonomy/failure_ledger.jsonl"
            ledger.write_text(
                "\n".join(
                    json.dumps(
                        {
                            "slice": "GLOBAL",
                            "failure_class": "tripwire_triggered",
                            "root_cause_id": root_cause,
                            "severity": "high",
                            "open": True,
                        }
                    )
                    for root_cause in (target, other)
                )
                + "\n",
                encoding="utf-8",
            )

            report = close_failure(
                root,
                "GLOBAL",
                "tripwire_triggered",
                "docs/reviews/closure.md",
                "closed in test",
                root_cause_id=target,
            )

            self.assertEqual(report["status"], "ok")
            rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines()]
            self.assertFalse(rows[0]["open"])
            self.assertTrue(rows[1]["open"])

    def test_close_failure_requeues_blocked_slice_when_failures_are_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            evidence = root / "docs/reviews/closure.md"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("Verdict: pass\n", encoding="utf-8")
            slices_path = root / "ops/autonomy/slices.json"
            slices = json.loads(slices_path.read_text(encoding="utf-8"))
            slices[0]["status"] = "blocked"
            slices[0]["retry_count"] = 5
            slices[0]["reason"] = "retry cap exceeded"
            write_json_atomic(slices_path, slices)
            ledger = root / "ops/autonomy/failure_ledger.jsonl"
            ledger.write_text(
                json.dumps({"slice": "S01", "failure_class": "test_failure", "severity": "medium", "open": True}) + "\n",
                encoding="utf-8",
            )

            report = close_failure(root, "S01", "test_failure", "docs/reviews/closure.md", "closed in test")

            self.assertEqual(report["status"], "ok")
            updated = json.loads(slices_path.read_text(encoding="utf-8"))
            self.assertEqual(updated[0]["status"], "replan_required")
            self.assertEqual(updated[0]["retry_count"], 0)
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["current_slice"], "S01")

    def test_close_failure_preserves_swr_review_repair_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            evidence = root / "docs/reviews/closure.md"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("Verdict: pass\n", encoding="utf-8")
            slices_path = root / "ops/autonomy/slices.json"
            slices = json.loads(slices_path.read_text(encoding="utf-8"))
            slices[0]["status"] = "blocked"
            slices[0]["retry_count"] = 5
            slices[0]["reason"] = "slice readiness failed"
            slices[0]["swr_review_repair"] = {
                "repair_action": "rerun_review_lane",
                "repair_stage_id": "source_authority_map",
            }
            write_json_atomic(slices_path, slices)
            ledger = root / "ops/autonomy/failure_ledger.jsonl"
            ledger.write_text(
                json.dumps({"slice": "S01", "failure_class": "audit_failure", "severity": "high", "open": True}) + "\n",
                encoding="utf-8",
            )

            report = close_failure(root, "S01", "audit_failure", "docs/reviews/closure.md", "closed in test")

            self.assertEqual(report["status"], "ok")
            updated = json.loads(slices_path.read_text(encoding="utf-8"))
            self.assertEqual(updated[0]["status"], "blocked_compile_inputs")
            self.assertEqual(updated[0]["retry_count"], 0)
            self.assertEqual(updated[0]["reason"], "slice readiness failed")
            self.assertEqual(updated[0]["swr_review_repair"]["repair_action"], "rerun_review_lane")
            state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["current_slice"], "S01")

    def test_close_failure_redacts_secret_notes(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            evidence = root / "docs/reviews/closure.md"
            evidence.parent.mkdir(parents=True)
            evidence.write_text("Verdict: pass\n", encoding="utf-8")
            ledger = root / "ops/autonomy/failure_ledger.jsonl"
            ledger.write_text(
                json.dumps({"slice": "S01", "failure_class": "provider_auth_failure", "severity": "high", "open": True}) + "\n",
                encoding="utf-8",
            )

            report = close_failure(
                root,
                "S01",
                "provider_auth_failure",
                "docs/reviews/closure.md",
                "access_" + "token=" + "super" + "secret" + "value",
            )

            self.assertEqual(report["status"], "ok")
            rows = [json.loads(line) for line in ledger.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertEqual(rows[0]["closure_note"], "access_token=[REDACTED]")

    def test_close_failure_rejects_invalid_retarget_evidence(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=root, check=True)
            subprocess.run(["git", "config", "user.name", "Tests"], cwd=root, check=True)
            (root / "seed.txt").write_text("seed\n", encoding="utf-8")
            subprocess.run(["git", "add", "seed.txt"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-m", "seed"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, stdout=subprocess.PIPE, check=True).stdout.strip()
            evidence = root / "docs/evidence/S01-run-retarget-test.json"
            evidence.parent.mkdir(parents=True)
            write_json_atomic(
                evidence,
                {
                    "slice": "S01",
                    "run_id": "RUN_TEST",
                    "old_run_branch_head": head,
                    "new_target_commit": head,
                    "merge_base": head,
                    "item_checkpoint_ancestry_proof": "test",
                    "terminal_counts_before": {"passed": 1},
                    "terminal_counts_after": {"passed": 1},
                    "skipped_item_count": 1,
                    "repaired_files": ["seed.txt"],
                    "reason": "invalid skipped item test",
                    "closure_evidence": "docs/evidence/S01-run-retarget-test.json",
                },
            )
            ledger = root / "ops/autonomy/failure_ledger.jsonl"
            ledger.write_text(
                json.dumps({"slice": "S01", "failure_class": "audit_failure", "severity": "high", "open": True}) + "\n",
                encoding="utf-8",
            )

            report = close_failure(root, "S01", "audit_failure", "docs/evidence/S01-run-retarget-test.json", "closed in test")

            self.assertEqual(report["status"], "error")
            self.assertTrue(any("retarget closure evidence invalid" in error for error in report["errors"]))

    def test_tripwires_future_deadlines_do_not_fire(self) -> None:
        # Intent: a deadline that is still in the future must not fire, even
        # when the configured evidence is entirely missing.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            rewrite_tripwire_deadlines(root, default_offset_days=30)

            report = evaluate_tripwires(root)

            self.assertEqual(report["status"], "ok", report)
            self.assertEqual(report["fired"], [])

    def test_tripwires_newest_blocked_report_beats_older_ok_report(self) -> None:
        # Intent: newest report wins regardless of status. An older `ok`
        # report must never mask a newer failure report once the deadline has
        # passed. File names and mtimes deliberately oppose the embedded
        # timestamps to prove `created_at` drives selection.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            rewrite_tripwire_deadlines(root, {"on_oura_failure_week_1": -5}, default_offset_days=30)
            evidence_dir = root / "private/evidence/S03/oura_smoke"
            evidence_dir.mkdir(parents=True)
            (evidence_dir / "a-newer-blocked.json").write_text(
                json.dumps({"status": "blocked_external", "created_at": iso_days_ago(1)}), encoding="utf-8"
            )
            (evidence_dir / "z-older-ok.json").write_text(
                json.dumps({"status": "ok", "created_at": iso_days_ago(2)}), encoding="utf-8"
            )

            report = evaluate_tripwires(root)

            self.assertEqual(report["status"], "error", report)
            self.assertEqual([item["name"] for item in report["fired"]], ["on_oura_failure_week_1"])
            evidence_status = report["fired"][0]["evidence_status"]
            self.assertEqual(evidence_status["status"], "blocked_external")
            self.assertFalse(evidence_status["ok"])
            self.assertTrue(str(evidence_status["report"]).endswith("a-newer-blocked.json"), evidence_status)

    def test_tripwires_newest_ok_report_beats_older_blocked_report(self) -> None:
        # Intent: newest report wins in the other direction too. A newer `ok`
        # report clears an older failure, so nothing fires even though the
        # deadline has passed.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            rewrite_tripwire_deadlines(root, {"on_oura_failure_week_1": -5}, default_offset_days=30)
            evidence_dir = root / "private/evidence/S03/oura_smoke"
            evidence_dir.mkdir(parents=True)
            (evidence_dir / "a-newer-ok.json").write_text(
                json.dumps({"status": "ok", "created_at": iso_days_ago(1)}), encoding="utf-8"
            )
            (evidence_dir / "z-older-blocked.json").write_text(
                json.dumps({"status": "blocked_external", "created_at": iso_days_ago(2)}), encoding="utf-8"
            )

            report = evaluate_tripwires(root)

            self.assertEqual(report["status"], "ok", report)
            self.assertEqual(report["fired"], [])

    def test_tripwires_fall_back_to_file_mtime_when_reports_lack_timestamps(self) -> None:
        # Intent: reports without an embedded timestamp are ordered by file
        # mtime, so a newer-by-mtime failure still beats an older `ok`.
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            rewrite_tripwire_deadlines(root, {"on_oura_failure_week_1": -5}, default_offset_days=30)
            evidence_dir = root / "private/evidence/S03/oura_smoke"
            evidence_dir.mkdir(parents=True)
            older_ok = evidence_dir / "z-older-ok.json"
            newer_blocked = evidence_dir / "a-newer-blocked.json"
            older_ok.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
            newer_blocked.write_text(json.dumps({"status": "blocked_external"}), encoding="utf-8")
            now = datetime.now().timestamp()
            os.utime(older_ok, (now - 2 * 86400, now - 2 * 86400))
            os.utime(newer_blocked, (now - 86400, now - 86400))

            report = evaluate_tripwires(root)

            self.assertEqual(report["status"], "error", report)
            self.assertEqual([item["name"] for item in report["fired"]], ["on_oura_failure_week_1"])
            evidence_status = report["fired"][0]["evidence_status"]
            self.assertEqual(evidence_status["status"], "blocked_external")
            self.assertTrue(str(evidence_status["report"]).endswith("a-newer-blocked.json"), evidence_status)

    def test_preflight_checks_git_repo_and_policy_shape(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            copy_autonomy_fixture(root)
            subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
            from scripts.verify_autonomy_preflight import preflight

            report = preflight(root, Path("/Users/aeziz-local/keel"), strict_tools=False, run_keel_smoke=False)
            self.assertTrue(report["checks"]["git_repo"])
            self.assertFalse(any("policy.yaml missing required key" in error for error in report["errors"]))

    def test_row_author_preserves_high_risk_gate_term_and_slice_label(self) -> None:
        context = {
            "schema_version": "row_author_context_v1",
            "source_artifact_paths": ["docs/gstack/s02-mood-api-autoplan.md"],
            "task_cards": [
                {
                    "task_id": "task_001",
                    "phase": "Autonomous security review",
                    "task": "Generate the S02 autonomous security review artifact.",
                    "declared_deliverables": ["docs/reviews/s02-autonomous-security-review.md"],
                    "existing_repo_surfaces": ["docs/gstack/s02-mood-api-autoplan.md"],
                    "clamped_allowed_write_roots": ["docs/reviews"],
                    "verification_candidates": ["python scripts/check_autonomous_review_exists.py S02"],
                    "behavioral": True,
                }
            ],
        }
        prompt = "# Input: row_author_context_v1\n" + json.dumps(context)
        proc = subprocess.run(
            ["python", str(ROOT / "scripts/autokeel_row_author.py")],
            input=prompt,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        self.assertEqual(proc.returncode, 0, proc.stderr)
        payload = json.loads(proc.stdout)
        rendered = json.dumps(payload)
        self.assertIn("autonomous_gate_review", rendered)
        self.assertIn("Required for the S02 acceptance contract.", rendered)


if __name__ == "__main__":
    unittest.main()

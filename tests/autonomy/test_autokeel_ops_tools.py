from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from ops.autonomy.autokeel import write_json_atomic
from scripts.close_failure import close_failure
from scripts.evaluate_tripwires import evaluate_tripwires


ROOT = Path(__file__).resolve().parents[2]


def copy_autonomy_fixture(dst: Path) -> None:
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
    write_json_atomic(slices_path, slices)
    write_json_atomic(
        dst / "ops/autonomy/autonomy_state.json",
        {"active_run": None, "completed_slices": [], "current_slice": None, "last_event_id": 0, "v1_complete": False},
    )
    (dst / "ops/autonomy/failure_ledger.jsonl").write_text("", encoding="utf-8")


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

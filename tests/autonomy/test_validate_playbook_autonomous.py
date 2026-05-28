from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts.validate_playbook_autonomous import plan_orchestrator_roots, validate_playbook


ROOT = Path(__file__).resolve().parents[2]


VALID_PLAYBOOK = """# Playbook

## Ordered Execution Plan

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|
| 01 | Create DuckDB v1 schema file | src/db/schema.sql; tests/warehouse/test_schema.py | src/db; tests/warehouse | true | python -m pytest tests/warehouse -q | schema tests pass | none | none |
"""


class ValidatePlaybookTests(unittest.TestCase):
    def validate_text(self, text: str):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "playbook.md"
            path.write_text(text, encoding="utf-8")
            return validate_playbook(path)

    def validate_high_risk_text(self, text: str):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "playbook.md"
            path.write_text(text, encoding="utf-8")
            return validate_playbook(path, policy_path=ROOT / "ops/autonomy/policy.yaml", risk="high")

    def validate_repo_high_risk_text(self, text: str):
        with tempfile.TemporaryDirectory(dir=ROOT) as temp:
            path = Path(temp) / "playbook.md"
            path.write_text(text, encoding="utf-8")
            return validate_playbook(path, policy_path=ROOT / "ops/autonomy/policy.yaml", risk="high")

    def test_plan_orchestrator_roots_include_env_and_policy_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "ops/autonomy").mkdir(parents=True)
            policy_po = root / "configured-po"
            (root / "ops/autonomy/policy.yaml").write_text(
                f"plan_orchestrator_root: {policy_po}\n",
                encoding="utf-8",
            )
            playbook = root / "docs/playbooks/s02.md"
            playbook.parent.mkdir(parents=True)
            playbook.write_text("# playbook\n", encoding="utf-8")

            with patch.dict("os.environ", {"KEEL_PO_ROOT": str(root / "env-po")}):
                roots = plan_orchestrator_roots(playbook)

            self.assertEqual(roots[0], root / "env-po")
            self.assertIn(policy_po, roots)

    def test_valid_autonomous_row_passes(self) -> None:
        report = self.validate_text(VALID_PLAYBOOK)
        self.assertEqual(report["status"], "ok", report)

    def test_active_manual_gate_fails(self) -> None:
        report = self.validate_text(VALID_PLAYBOOK.replace("| none | none |", "| signoff | none |"))
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("manual_gate" in error for error in report["errors"]))

    def test_broad_write_root_fails(self) -> None:
        report = self.validate_text(VALID_PLAYBOOK.replace("src/db; tests/warehouse", "."))
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("broad allowed_write_root" in error for error in report["errors"]))

    def test_allowed_write_roots_requires_semicolon_separator(self) -> None:
        report = self.validate_text(VALID_PLAYBOOK.replace("src/db; tests/warehouse", "src/db, tests/warehouse"))
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("allowed_write_roots must use semicolon" in error for error in report["errors"]))

    def test_missing_verification_for_code_fails(self) -> None:
        report = self.validate_text(VALID_PLAYBOOK.replace("python -m pytest tests/warehouse -q", "none"))
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("verification" in error for error in report["errors"]))

    def test_ui_forbidden_language_fails(self) -> None:
        text = """# Playbook

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | manual_gate | external_check |
|---|---|---|---|---|---|---|---|
| 01 | Add Streamlit UI copy saying biggest drivers | app/streamlit_app.py | app | true | python -m pytest tests/ui -q | none | none |
"""
        report = self.validate_text(text)
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("forbidden v1 UI language" in error for error in report["errors"]))

    def test_v2_scope_fails_when_not_deferred(self) -> None:
        text = """# Playbook

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | manual_gate | external_check |
|---|---|---|---|---|---|---|---|
| 01 | Add Garmin training load to model | src/model/features.py | src/model | true | python -m pytest tests/model -q | none | none |
"""
        report = self.validate_text(text)
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("v2 scope creep" in error for error in report["errors"]))

    def test_v2_scope_boundary_language_passes_when_negated(self) -> None:
        text = """# Playbook

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|
| 01 | Keep protected retrospective placeholders with no prospective output and no recommendations. | src/api/app.py; tests/test_api_security.py | src/api/app.py; tests/test_api_security.py | true | python -m pytest tests/test_api_security.py -q | tests pass | none | none |
"""
        report = self.validate_text(text)
        self.assertEqual(report["status"], "ok", report)

    def test_human_approval_claim_fails(self) -> None:
        text = """# Playbook

Human approval is granted for this autonomous run.

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | manual_gate | external_check |
|---|---|---|---|---|---|---|---|
| 01 | Record autonomous review evidence | docs/reviews/s02.md | docs/reviews/s02.md | false | test -s docs/reviews/s02.md | none | docs/evidence/s02-review.json |
"""
        report = self.validate_high_risk_text(text)
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("human approval" in error for error in report["errors"]))

    def test_human_approval_boundary_language_passes_when_negated(self) -> None:
        text = """# Playbook

autonomous_gate_review evidence is required.
Active human approval gates are not emitted, and review artifacts are used in lieu of human approval.

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|
| 01 | Record autonomous review evidence | docs/reviews/s02.md | docs/reviews/s02.md | false | test -s docs/reviews/s02.md | review artifact validates | none | docs/evidence/s02-review.json |
"""
        report = self.validate_high_risk_text(text)
        self.assertEqual(report["status"], "ok", report)

    def test_negative_boundary_language_allowed_only_in_policy_notes(self) -> None:
        text = """# Playbook

autonomous_gate_review evidence is required.

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | safety_note | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|---|
| 01 | Record review evidence | docs/reviews/s02.md | docs/reviews/s02.md | false | test -s docs/reviews/s02.md | review artifact validates | Active human approval gates are not emitted. | none | docs/evidence/s02-review.json |
"""
        report = self.validate_high_risk_text(text)
        self.assertEqual(report["status"], "ok", report)

    def test_active_human_approval_in_exit_criteria_fails(self) -> None:
        text = """# Playbook

autonomous_gate_review evidence is required.

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|
| 01 | Record review evidence | docs/reviews/s02.md | docs/reviews/s02.md | false | test -s docs/reviews/s02.md | human approval is complete | none | docs/evidence/s02-review.json |
"""
        report = self.validate_high_risk_text(text)
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("exit_criteria" in error and "human approval" in error for error in report["errors"]))

    def test_active_prospective_output_in_action_fails(self) -> None:
        text = """# Playbook

autonomous_gate_review evidence is required.

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|
| 01 | Implement prospective output | src/api/app.py | src/api/app.py | true | python -m pytest tests/test_api_security.py -q | tests pass | none | none |
"""
        report = self.validate_high_risk_text(text)
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("v2 scope creep" in error for error in report["errors"]))

    def test_no_prospective_predictions_boundary_language_passes(self) -> None:
        text = """# Playbook

autonomous_gate_review evidence is required.
S02 preserves retrospective scope with no prospective predictions.

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|
| 01 | Record protected placeholder behavior | src/api/app.py | src/api/app.py | true | python -m pytest tests/test_api_security.py -q | tests pass | none | none |
"""
        report = self.validate_high_risk_text(text)
        self.assertEqual(report["status"], "ok", report)

    def test_long_out_of_scope_provider_list_passes_when_negated(self) -> None:
        text = VALID_PLAYBOOK + (
            "\nDo not implement Oura ingestion, 8 Sleep ingestion, provider OAuth, "
            "feature engineering, model training, Streamlit UI, Garmin, Withings, "
            "chest strap, nutrition, or multi-daily mood logging.\n"
        )
        report = self.validate_text(text)
        self.assertEqual(report["status"], "ok", report)

    def test_forbidden_term_in_verification_command_fails(self) -> None:
        text = """# Playbook

autonomous_gate_review evidence is required.

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|
| 01 | Record review evidence | docs/reviews/s02.md | docs/reviews/s02.md | false | grep -F "human approval" docs/reviews/s02.md | review artifact validates | none | docs/evidence/s02-review.json |
"""
        report = self.validate_high_risk_text(text)
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("executable field" in error and "human approval" in error for error in report["errors"]))

    def test_po_normalization_rejects_natural_language_prerequisites(self) -> None:
        text = """# S02 Mood API Playbook

Format: markdown_playbook_v1

autonomous_gate_review evidence is required.

## 1. Phase Overview

S02 scope.

## 2. Execution Items

| step_id | phase | action | why_now | owner_type | prerequisites | repo_surfaces | deliverable | exit_criteria | allowed_write_roots | requires_red_green | required_verification_commands | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 01 | Foundation | Write review scaffold | now | autonomous_executor | none | docs/reviews/s02-a.md | docs/reviews/s02-a.md | file exists | docs/reviews/s02-a.md | false | test -s docs/reviews/s02-a.md | none | none |
| 02 | Security | Write security scaffold | now | autonomous_executor | none | docs/reviews/s02-b.md | docs/reviews/s02-b.md | file exists | docs/reviews/s02-b.md | false | test -s docs/reviews/s02-b.md | none | none |
| 03 | Closure | Write closure scaffold | now | autonomous_executor | 01 and 02 | docs/reviews/s02-c.md | docs/reviews/s02-c.md | file exists | docs/reviews/s02-c.md | false | test -s docs/reviews/s02-c.md | none | none |
"""
        report = self.validate_high_risk_text(text)
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("plan-orchestrator normalization failed" in error for error in report["errors"]))

    def test_po_normalization_accepts_comma_prerequisites(self) -> None:
        text = """# S02 Mood API Playbook

Format: markdown_playbook_v1

autonomous_gate_review evidence is required.

## 1. Phase Overview

S02 scope.

## 2. Execution Items

| step_id | phase | action | why_now | owner_type | prerequisites | repo_surfaces | deliverable | exit_criteria | allowed_write_roots | requires_red_green | required_verification_commands | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 01 | Foundation | Write review scaffold | now | autonomous_executor | none | docs/gstack/s02-mood-api-autoplan.md | docs/reviews/s02-a.md | file exists | docs/reviews/s02-a.md | false | test -s docs/reviews/s02-a.md | none | none |
| 02 | Security | Write security scaffold | now | autonomous_executor | none | docs/gstack/s02-mood-api-autoplan.md | docs/reviews/s02-b.md | file exists | docs/reviews/s02-b.md | false | test -s docs/reviews/s02-b.md | none | none |
| 03 | Closure | Write closure scaffold | now | autonomous_executor | 01,02 | docs/gstack/s02-mood-api-autoplan.md | docs/reviews/s02-c.md | file exists | docs/reviews/s02-c.md | false | test -s docs/reviews/s02-c.md | none | none |
"""
        report = self.validate_high_risk_text(text)
        self.assertEqual(report["status"], "ok", report)

    def test_repo_surfaces_rejects_same_row_future_deliverable(self) -> None:
        text = """# S02 Mood API Playbook

Format: markdown_playbook_v1

autonomous_gate_review evidence is required.

## 1. Phase Overview

S02 scope.

## 2. Execution Items

| step_id | phase | action | why_now | owner_type | prerequisites | repo_surfaces | deliverable | exit_criteria | allowed_write_roots | requires_red_green | required_verification_commands | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 01 | Foundation | Write new API file | now | autonomous_executor | none | src/api/new_future_input.py | src/api/new_future_input.py | file exists | src/api/new_future_input.py | true | test -s src/api/new_future_input.py | none | none |
"""
        report = self.validate_repo_high_risk_text(text)
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("repo_surfaces references path unavailable" in error for error in report["errors"]))

    def test_repo_surfaces_accepts_prior_row_deliverable(self) -> None:
        text = """# S02 Mood API Playbook

Format: markdown_playbook_v1

autonomous_gate_review evidence is required.

## 1. Phase Overview

S02 scope.

## 2. Execution Items

| step_id | phase | action | why_now | owner_type | prerequisites | repo_surfaces | deliverable | exit_criteria | allowed_write_roots | requires_red_green | required_verification_commands | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 01 | Foundation | Write new API file | now | autonomous_executor | none | docs/gstack/s02-mood-api-autoplan.md | src/api/new_prior_output.py | file exists | src/api/new_prior_output.py | true | test -s src/api/new_prior_output.py | none | none |
| 02 | Follow up | Use prior API file | now | autonomous_executor | 01 | src/api/new_prior_output.py | tests/new_prior_output_test.py | file exists | tests/new_prior_output_test.py | true | test -s tests/new_prior_output_test.py | none | none |
"""
        report = self.validate_repo_high_risk_text(text)
        self.assertEqual(report["status"], "ok", report)

    def test_unverified_limiter_dependency_contract_fails(self) -> None:
        text = """# S02 Mood API Playbook

Format: markdown_playbook_v1

autonomous_gate_review evidence is required.

## 1. Phase Overview

S02 scope.

## 2. Execution Items

| step_id | phase | action | why_now | owner_type | prerequisites | repo_surfaces | deliverable | exit_criteria | allowed_write_roots | requires_red_green | required_verification_commands | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 01 | Security | Add POST rate limiting only through an already available in-memory limiter dependency. | now | autonomous_executor | none | docs/gstack/s02-mood-api-autoplan.md | src/api/security.py | If the limiter dependency is unavailable, block. | src/api/security.py | true | python -m pytest tests/test_api_security.py -q | none | none |
"""
        report = self.validate_high_risk_text(text)
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("unverified limiter dependency" in error for error in report["errors"]))

    def test_high_risk_old_swr_stage5_shape_fails(self) -> None:
        text = """# S02 Mood API Playbook

autonomous_gate_review evidence is required.

| step_id | phase | action | why_now | owner_type | prerequisites | repo_surfaces | deliverable | exit_criteria | allowed_write_roots | requires_red_green |
|---|---|---|---|---|---|---|---|---|---|---|
| 01 | Mood API | Implement API | now | ai | none | src/api/mood.py | src/api/mood.py | tests pass | src/api | true |
"""
        report = self.validate_high_risk_text(text)
        self.assertEqual(report["status"], "error")
        self.assertTrue(any("required_verification_commands" in error for error in report["errors"]))

    def test_high_risk_docs_false_row_with_verification_passes(self) -> None:
        text = """# S02 Mood API Playbook

autonomous_gate_review evidence is required before PO.

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | exit_criteria | manual_gate | external_check |
|---|---|---|---|---|---|---|---|---|
| 01 | Record autonomous review evidence | docs/reviews/s02.md | docs/reviews | false | python scripts/check_autonomous_review_exists.py S02 | review artifact validates | none | docs/evidence/s02-review.json |
"""
        report = self.validate_high_risk_text(text)
        self.assertEqual(report["status"], "ok", report)


if __name__ == "__main__":
    unittest.main()

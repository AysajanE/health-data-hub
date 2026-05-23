from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from scripts.validate_playbook_autonomous import validate_playbook


VALID_PLAYBOOK = """# Playbook

## Ordered Execution Plan

| item | action | deliverable | allowed_write_roots | requires_red_green | required_verification_commands | manual_gate | external_check |
|---|---|---|---|---|---|---|---|
| 01 | Create DuckDB v1 schema file | src/db/schema.sql; tests/warehouse/test_schema.py | src/db; tests/warehouse | true | python -m pytest tests/warehouse -q | none | none |
"""


class ValidatePlaybookTests(unittest.TestCase):
    def validate_text(self, text: str):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "playbook.md"
            path.write_text(text, encoding="utf-8")
            return validate_playbook(path)

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


if __name__ == "__main__":
    unittest.main()

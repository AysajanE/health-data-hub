from __future__ import annotations

from datetime import UTC, datetime
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4
from zoneinfo import ZoneInfo

from streamlit.testing.v1 import AppTest

from app import mood_form
from app.mood_form import (
    CUTOFF_NOTE,
    MOOD_ANCHORS,
    MOOD_PROMPT,
    PICK_FIRST,
    TOKEN_LABEL,
    TOKEN_MISSING,
    TOKEN_REJECTED,
    load_settings,
    read_summary,
    token_matches,
)
from src.api.mood_date import resolve_mood_date
from src.warehouse.locking import lock_path_for_database, warehouse_write_lock
from src.warehouse.warehouse import connect_duckdb, persist_mood_entry_locked


REPO_ROOT = Path(__file__).resolve().parents[2]
FORM_PATH = REPO_ROOT / "app" / "mood_form.py"
FAKE_TOKEN = "test-form-token-4d2f"
HOME_TIMEZONE = "America/Toronto"
# Tests-only list of avoided phrases; the app must never render any of them.
AVOIDED_PHRASES = (
    "drivers",
    "caused",
    "you should",
    "you would have felt",
    "tomorrow prediction",
    "recommendations today",
    "what made you tired",
)
TEXT_ELEMENT_GROUPS = (
    "title",
    "header",
    "subheader",
    "markdown",
    "caption",
    "text",
    "info",
    "success",
    "error",
    "warning",
)
WIDGET_GROUPS = ("radio", "multiselect", "text_area", "text_input", "button")


def rendered_texts(at: AppTest) -> list[str]:
    texts: list[str] = []
    for group in TEXT_ELEMENT_GROUPS:
        for element in getattr(at, group):
            texts.append(str(element.value))
    for group in WIDGET_GROUPS:
        for element in getattr(at, group):
            texts.append(str(element.label))
    return texts


class MoodFormHelpersTest(unittest.TestCase):
    def test_load_settings_requires_token(self) -> None:
        with self.assertRaises(ValueError) as raised:
            load_settings({"HOME_TIMEZONE": HOME_TIMEZONE})
        self.assertEqual(str(raised.exception), TOKEN_MISSING)

    def test_load_settings_defaults_timezone_and_database(self) -> None:
        settings = load_settings({"MOOD_FORM_TOKEN": FAKE_TOKEN})
        self.assertEqual(str(settings.home_timezone), HOME_TIMEZONE)
        self.assertTrue(str(settings.database_path).endswith("data/warehouse.duckdb"))

    def test_token_matches_is_exact(self) -> None:
        self.assertTrue(token_matches(f"  {FAKE_TOKEN} ", FAKE_TOKEN))
        self.assertFalse(token_matches(FAKE_TOKEN[:-1], FAKE_TOKEN))
        self.assertFalse(token_matches("", FAKE_TOKEN))
        self.assertFalse(token_matches("tökén", FAKE_TOKEN))

    def test_read_summary_waits_on_the_warehouse_lock_and_degrades_when_busy(self) -> None:
        with TemporaryDirectory() as tempdir:
            database_path = Path(tempdir) / "data" / "warehouse.duckdb"
            target = datetime(2026, 9, 10, tzinfo=UTC).date()
            persist_mood_entry_locked(
                database_path,
                {
                    "log_id": uuid4(),
                    "logged_at_utc": datetime(2026, 9, 11, 1, 0, tzinfo=UTC),
                    "mood_date": target,
                    "feeling": 8,
                    "energy": 6,
                    "notes": None,
                    "context_chips": (),
                    "source": "manual",
                    "supersedes_log_id": None,
                },
            )

            summary = read_summary(database_path, target)
            self.assertTrue(summary.available)
            self.assertEqual(summary.days_logged, 1)
            self.assertEqual(summary.feeling_for_target, 8)
            self.assertEqual(summary.recent, ((target, 8, 6),))
            self.assertEqual(summary.model_ready_days, 0)

            with patch.object(mood_form, "SUMMARY_LOCK_TIMEOUT_SECONDS", 0.2):
                with warehouse_write_lock(lock_path_for_database(database_path)):
                    busy = read_summary(database_path, target)
            self.assertFalse(busy.available)
            self.assertEqual(busy.days_logged, 0)


class MoodFormAppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "warehouse" / "test-only-warehouse.duckdb"
        self.env_patch = patch.dict(
            os.environ,
            {
                "MOOD_FORM_TOKEN": FAKE_TOKEN,
                "HOME_TIMEZONE": HOME_TIMEZONE,
                "HEALTH_HUB_DATABASE_PATH": str(self.database_path),
            },
        )
        self.env_patch.start()

    def tearDown(self) -> None:
        self.env_patch.stop()
        self.tempdir.cleanup()

    def start(self) -> AppTest:
        at = AppTest.from_file(str(FORM_PATH), default_timeout=30)
        at.run()
        return at

    def authenticate(self, at: AppTest, token: str) -> AppTest:
        at.text_input[0].input(token)
        at.button[0].click()
        at.run()
        return at

    def save_rating(self, at: AppTest, feeling: int) -> AppTest:
        at.radio[0].set_value(feeling)
        at.button[0].click()
        at.run()
        return at

    def mood_rows(self) -> list[tuple]:
        conn = connect_duckdb(self.database_path, read_only=True)
        try:
            return conn.execute(
                """
                SELECT log_id, mood_date, feeling, energy, source, supersedes_log_id
                FROM mood_entries
                ORDER BY logged_at_utc
                """
            ).fetchall()
        finally:
            conn.close()

    def mood_current_rows(self) -> list[tuple]:
        conn = connect_duckdb(self.database_path, read_only=True)
        try:
            return conn.execute("SELECT mood_date, log_id FROM mood_current").fetchall()
        finally:
            conn.close()

    def test_unauthenticated_view_shows_only_the_token_gate(self) -> None:
        at = self.start()

        self.assertEqual(len(at.text_input), 1)
        self.assertEqual(at.text_input[0].label, TOKEN_LABEL)
        self.assertEqual(len(at.radio), 0)
        self.assertEqual(len(at.error), 0)
        self.assertFalse(self.database_path.exists())

    def test_wrong_token_is_rejected_without_touching_the_warehouse(self) -> None:
        at = self.authenticate(self.start(), "not-the-token")

        self.assertEqual([element.value for element in at.error], [TOKEN_REJECTED])
        self.assertEqual(len(at.radio), 0)
        self.assertFalse(self.database_path.exists())

    def test_missing_token_configuration_is_a_visible_error(self) -> None:
        with patch.dict(os.environ, {"MOOD_FORM_TOKEN": ""}):
            at = self.start()

        self.assertEqual([element.value for element in at.error], [TOKEN_MISSING])
        self.assertEqual(len(at.text_input), 0)
        self.assertEqual(len(at.radio), 0)

    def test_prompt_anchors_and_cutoff_note_are_rendered(self) -> None:
        at = self.authenticate(self.start(), FAKE_TOKEN)

        self.assertEqual(at.radio[0].label, MOOD_PROMPT)
        captions = [element.value for element in at.caption]
        self.assertIn(CUTOFF_NOTE, captions)
        anchor_caption = next(value for value in captions if MOOD_ANCHORS[0] in value)
        for anchor in MOOD_ANCHORS:
            self.assertIn(anchor, anchor_caption)
        self.assertEqual(at.radio[0].value, None)

    def test_submit_without_a_rating_writes_nothing(self) -> None:
        at = self.authenticate(self.start(), FAKE_TOKEN)
        at.button[0].click()
        at.run()

        self.assertEqual([element.value for element in at.warning], [PICK_FIRST])
        self.assertFalse(self.database_path.exists())

    def test_save_and_correction_flow_persists_through_the_warehouse(self) -> None:
        home_tz = ZoneInfo(HOME_TIMEZONE)
        expected_before = resolve_mood_date(datetime.now(UTC), home_tz)
        at = self.authenticate(self.start(), FAKE_TOKEN)
        at = self.save_rating(at, 7)
        expected_after = resolve_mood_date(datetime.now(UTC), home_tz)

        self.assertEqual(len(at.success), 1)
        self.assertTrue(self.database_path.exists())
        self.assertFalse(Path(f"{self.database_path}.wal").exists())
        rows = self.mood_rows()
        self.assertEqual(len(rows), 1)
        first_log_id, first_date, first_feeling, first_energy, first_source, first_supersedes = rows[0]
        self.assertIn(first_date, {expected_before, expected_after})
        self.assertEqual(first_feeling, 7)
        self.assertIsNone(first_energy)
        self.assertEqual(first_source, "manual")
        self.assertIsNone(first_supersedes)
        self.assertEqual(self.mood_current_rows(), [(first_date, first_log_id)])
        markdown_values = [element.value for element in at.markdown]
        self.assertIn("**Days logged so far:** 1", markdown_values)
        self.assertIn("**Collecting model-ready days:** 0 / 37", markdown_values)

        at = self.save_rating(at, 5)

        rows = self.mood_rows()
        self.assertEqual(len(rows), 2)
        second_log_id, second_date, second_feeling, _, second_source, second_supersedes = rows[1]
        self.assertEqual(second_date, first_date)
        self.assertEqual(second_feeling, 5)
        self.assertEqual(second_source, "manual")
        self.assertEqual(second_supersedes, first_log_id)
        self.assertEqual(self.mood_current_rows(), [(first_date, second_log_id)])
        self.assertTrue(any("already exists" in element.value for element in at.info))

    def test_rendered_text_never_contains_the_token_or_avoided_phrases(self) -> None:
        at = self.authenticate(self.start(), FAKE_TOKEN)
        at = self.save_rating(at, 6)

        for text in rendered_texts(at):
            self.assertNotIn(FAKE_TOKEN, text)
            lowered = text.lower()
            for phrase in AVOIDED_PHRASES:
                self.assertNotIn(phrase, lowered, msg=f"avoided phrase {phrase!r} in {text!r}")


if __name__ == "__main__":
    unittest.main()

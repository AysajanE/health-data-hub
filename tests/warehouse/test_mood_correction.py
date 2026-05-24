from __future__ import annotations

from datetime import UTC, date, datetime
import unittest
from uuid import uuid4

from src.warehouse.warehouse import connect_duckdb, insert_mood_entry, select_current_mood_entries


class MoodCorrectionTest(unittest.TestCase):
    def test_second_entry_updates_mood_current_and_training_join(self) -> None:
        mood_date = date(2026, 5, 23)
        conn = connect_duckdb(":memory:", apply_schema=True)
        try:
            first_entry = insert_mood_entry(
                conn,
                {
                    "log_id": uuid4(),
                    "logged_at_utc": datetime(2026, 5, 23, 22, 0, tzinfo=UTC),
                    "mood_date": mood_date,
                    "feeling": 4,
                    "energy": 4,
                    "notes": "rough afternoon",
                    "context_chips": ("high_stress",),
                    "source": "ios_shortcut",
                    "supersedes_log_id": None,
                },
            )
            corrected_entry = insert_mood_entry(
                conn,
                {
                    "log_id": uuid4(),
                    "logged_at_utc": datetime(2026, 5, 23, 23, 15, tzinfo=UTC),
                    "mood_date": mood_date,
                    "feeling": 6,
                    "energy": 5,
                    "notes": "late improvement",
                    "context_chips": ("high_stress",),
                    "source": "ios_shortcut",
                    "supersedes_log_id": None,
                },
            )

            correction_rows = conn.execute(
                """
                SELECT log_id, supersedes_log_id
                FROM mood_entries
                WHERE mood_date = ?
                ORDER BY logged_at_utc
                """,
                [mood_date],
            ).fetchall()
            current_row = conn.execute(
                "SELECT mood_date, log_id FROM mood_current WHERE mood_date = ?",
                [mood_date],
            ).fetchone()
            training_rows = select_current_mood_entries(conn)
        finally:
            conn.close()

        self.assertEqual(len(correction_rows), 2)
        self.assertEqual(correction_rows[0][1], None)
        self.assertEqual(correction_rows[1][1], first_entry.log_id)
        self.assertEqual(corrected_entry.supersedes_log_id, first_entry.log_id)
        self.assertEqual(current_row, (mood_date, corrected_entry.log_id))
        self.assertEqual([row.log_id for row in training_rows], [corrected_entry.log_id])
        self.assertEqual(training_rows[0].feeling, 6)


if __name__ == "__main__":
    unittest.main()

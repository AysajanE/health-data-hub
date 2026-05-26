from __future__ import annotations

from datetime import date
import json
from pathlib import Path
from stat import S_IMODE
from tempfile import TemporaryDirectory
import unittest

from pydantic import ValidationError

from src.warehouse.warehouse import connect_duckdb, insert_mood_entry, insert_sleep_night


class QuarantineHandlingTest(unittest.TestCase):
    def test_sleep_validation_failure_writes_private_payload_and_redacted_log(self) -> None:
        conn = connect_duckdb(":memory:", apply_schema=True)
        try:
            with TemporaryDirectory() as temp_dir:
                quarantine_dir = Path(temp_dir) / "quarantine"
                log_path = Path(temp_dir) / "healthhub.log"

                with self.assertRaises(ValidationError):
                    insert_sleep_night(
                        conn,
                        {
                            "source": "oura",
                            "sleep_date": date(2026, 5, 23),
                            "total_sleep_min": -5,
                            "ingested_at_utc": "2026-05-23T06:00:00Z",
                            "debug_token": "sleep-secret-token",
                        },
                        quarantine_dir=quarantine_dir,
                        general_log_path=log_path,
                    )

                quarantine_files = list(quarantine_dir.glob("*.json"))
                self.assertEqual(len(quarantine_files), 1)

                quarantine_doc = json.loads(quarantine_files[0].read_text(encoding="utf-8"))
                self.assertEqual(quarantine_doc["payload"]["debug_token"], "sleep-secret-token")
                self.assertEqual(quarantine_doc["payload"]["sleep_date"], "2026-05-23")
                self.assertEqual(S_IMODE(quarantine_files[0].stat().st_mode), 0o600)

                log_entry = json.loads(log_path.read_text(encoding="utf-8").strip())
        finally:
            conn.close()

        self.assertEqual(
            set(log_entry),
            {"source", "detected_at_utc", "error_summary", "payload_hash"},
        )
        self.assertEqual(log_entry["source"], "oura")
        self.assertIn("total_sleep_min", log_entry["error_summary"])
        self.assertNotIn("sleep-secret-token", json.dumps(log_entry, sort_keys=True))
        self.assertEqual(quarantine_doc["metadata"]["payload_hash"], log_entry["payload_hash"])

    def test_mood_validation_failure_keeps_notes_out_of_general_log(self) -> None:
        conn = connect_duckdb(":memory:", apply_schema=True)
        try:
            with TemporaryDirectory() as temp_dir:
                quarantine_dir = Path(temp_dir) / "quarantine"
                log_path = Path(temp_dir) / "healthhub.log"

                with self.assertRaises(ValidationError):
                    insert_mood_entry(
                        conn,
                        {
                            "logged_at_utc": "2026-05-23T22:00:00Z",
                            "mood_date": "2026-05-23",
                            "feeling": 11,
                            "notes": "private note for quarantine only",
                            "source": "ios_shortcut",
                        },
                        quarantine_dir=quarantine_dir,
                        general_log_path=log_path,
                    )

                quarantine_doc = json.loads(
                    next(quarantine_dir.glob("*.json")).read_text(encoding="utf-8")
                )
                log_entry = json.loads(log_path.read_text(encoding="utf-8").strip())
        finally:
            conn.close()

        self.assertEqual(quarantine_doc["payload"]["notes"], "private note for quarantine only")
        self.assertEqual(log_entry["source"], "ios_shortcut")
        self.assertNotIn("private note for quarantine only", json.dumps(log_entry, sort_keys=True))


if __name__ == "__main__":
    unittest.main()

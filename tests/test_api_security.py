from __future__ import annotations

from datetime import UTC, date, datetime
import os
import unittest
from uuid import UUID

from pydantic import ValidationError

from src.api.dependencies import get_home_timezone, get_lan_bind_ip, get_mood_token, load_api_settings
from src.api.schemas import MoodLogRequest, MoodLogResponse


class MoodApiContractTest(unittest.TestCase):
    def test_request_schema_accepts_valid_payload(self) -> None:
        payload = MoodLogRequest.model_validate(
            {
                "feeling": 7,
                "energy": 5,
                "notes": "Late dinner.",
                "context_chips": ["late_meal", "high_stress"],
                "logged_at_utc": "2026-05-24T03:15:00-04:00",
            }
        )

        self.assertEqual(payload.feeling, 7)
        self.assertEqual(payload.energy, 5)
        self.assertEqual(payload.notes, "Late dinner.")
        self.assertEqual(payload.context_chips, ("late_meal", "high_stress"))
        self.assertEqual(payload.logged_at_utc, datetime(2026, 5, 24, 7, 15, tzinfo=UTC))

    def test_request_schema_rejects_out_of_range_feeling(self) -> None:
        with self.assertRaises(ValidationError):
            MoodLogRequest.model_validate({"feeling": 11})

    def test_request_schema_rejects_out_of_range_energy(self) -> None:
        with self.assertRaises(ValidationError):
            MoodLogRequest.model_validate({"feeling": 6, "energy": 0})

    def test_request_schema_rejects_non_strict_rating_values(self) -> None:
        invalid_payloads = (
            {"feeling": True},
            {"feeling": "7"},
            {"feeling": 6, "energy": False},
            {"feeling": 6, "energy": "5"},
        )

        for payload in invalid_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValidationError):
                    MoodLogRequest.model_validate(payload)

    def test_request_schema_rejects_unknown_context_chip(self) -> None:
        with self.assertRaises(ValidationError):
            MoodLogRequest.model_validate({"feeling": 6, "context_chips": ["unknown_chip"]})

    def test_request_schema_allows_optional_fields_to_be_omitted(self) -> None:
        payload = MoodLogRequest.model_validate({"feeling": 4})

        self.assertIsNone(payload.energy)
        self.assertIsNone(payload.notes)
        self.assertEqual(payload.context_chips, ())
        self.assertIsNone(payload.logged_at_utc)

    def test_response_schema_requires_ok_status(self) -> None:
        response = MoodLogResponse.model_validate(
            {
                "log_id": "12345678-1234-5678-1234-567812345678",
                "mood_date": "2026-05-24",
                "status": "ok",
            }
        )

        self.assertEqual(response.log_id, UUID("12345678-1234-5678-1234-567812345678"))
        self.assertEqual(response.mood_date, date(2026, 5, 24))
        self.assertEqual(response.status, "ok")

        with self.assertRaises(ValidationError):
            MoodLogResponse.model_validate(
                {
                    "log_id": "12345678-1234-5678-1234-567812345678",
                    "mood_date": "2026-05-24",
                    "status": "error",
                }
            )

    def test_settings_can_be_injected_from_fake_mapping(self) -> None:
        fake_env = {
            "MOOD_TOKEN": "fake-token",
            "LAN_BIND_IP": "192.168.1.55",
            "HOME_TIMEZONE": "America/Toronto",
        }
        original_values = dict(os.environ)
        os.environ["MOOD_TOKEN"] = "real-token-should-not-be-used"
        os.environ["LAN_BIND_IP"] = "10.0.0.9"
        os.environ["HOME_TIMEZONE"] = "UTC"

        try:
            settings = load_api_settings(fake_env)
        finally:
            os.environ.clear()
            os.environ.update(original_values)

        self.assertEqual(get_mood_token(settings), "fake-token")
        self.assertEqual(get_lan_bind_ip(settings), "192.168.1.55")
        self.assertEqual(get_home_timezone(settings).key, "America/Toronto")

    def test_settings_reject_invalid_home_timezone(self) -> None:
        with self.assertRaises(ValidationError):
            load_api_settings(
                {
                    "MOOD_TOKEN": "fake-token",
                    "LAN_BIND_IP": "192.168.1.55",
                    "HOME_TIMEZONE": "Not/AZone",
                }
            )

    def test_settings_default_home_timezone_fallback(self) -> None:
        settings = load_api_settings(
            {
                "MOOD_TOKEN": "fake-token",
                "LAN_BIND_IP": "192.168.1.55",
            }
        )

        self.assertEqual(get_home_timezone(settings).key, "America/Toronto")


if __name__ == "__main__":
    unittest.main()

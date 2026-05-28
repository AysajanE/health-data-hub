from __future__ import annotations

import asyncio
from datetime import UTC, date, datetime
import os
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

import httpx

from pydantic import ValidationError

from src.api.app import create_app
from src.api.dependencies import (
    build_api_settings,
    get_home_timezone,
    get_lan_bind_ip,
    get_mood_token,
    load_api_settings,
)
from src.api.schemas import MoodLogRequest, MoodLogResponse
from src.api.security import InMemoryRateLimiter


FAKE_MOOD_TOKEN = "test-mood-token"
SIMULATED_SAME_HOST_IP = "198.51.100.10"
SIMULATED_LAN_CLIENT_IP = "198.51.100.77"
SIMULATED_REMOTE_IP = "203.0.113.77"
LOOPBACK_IP = "127.0.0.1"
VALID_TOKEN_HEADERS = {"X-Mood-Token": FAKE_MOOD_TOKEN}


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
            "MOOD_TOKEN": FAKE_MOOD_TOKEN,
            "LAN_BIND_IP": SIMULATED_SAME_HOST_IP,
            "HOME_TIMEZONE": "America/Toronto",
        }
        with patch.dict(
            os.environ,
            {
                "MOOD_TOKEN": "fake-mood-token",
                "LAN_BIND_IP": "not-an-ip",
                "HOME_TIMEZONE": "Not/AZone",
            },
            clear=False,
        ):
            settings = load_api_settings(fake_env)

        self.assertEqual(get_mood_token(settings), FAKE_MOOD_TOKEN)
        self.assertEqual(get_lan_bind_ip(settings), SIMULATED_SAME_HOST_IP)
        self.assertEqual(get_home_timezone(settings).key, "America/Toronto")

    def test_settings_reject_invalid_home_timezone(self) -> None:
        with self.assertRaises(ValidationError):
            load_api_settings(
                {
                    "MOOD_TOKEN": FAKE_MOOD_TOKEN,
                    "LAN_BIND_IP": SIMULATED_SAME_HOST_IP,
                    "HOME_TIMEZONE": "Not/AZone",
                }
            )

    def test_settings_default_home_timezone_fallback(self) -> None:
        settings = load_api_settings(
            {
                "MOOD_TOKEN": FAKE_MOOD_TOKEN,
                "LAN_BIND_IP": SIMULATED_SAME_HOST_IP,
            }
        )

        self.assertEqual(get_home_timezone(settings).key, "America/Toronto")


class _FrozenClock:
    def __init__(self, now: datetime) -> None:
        self._now = now

    def now(self) -> datetime:
        return self._now


class MoodApiSecurityRouteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = build_api_settings(
            mood_token=FAKE_MOOD_TOKEN,
            lan_bind_ip=SIMULATED_SAME_HOST_IP,
            home_timezone="America/Toronto",
        )
        self.clock = _FrozenClock(datetime(2026, 5, 24, 1, 0, tzinfo=UTC))
        self.persisted_calls: list[tuple[MoodLogRequest, date]] = []
        self.app = create_app(
            settings=self.settings,
            persist_mood_entry=self._persist_mood_entry,
            rate_limiter=InMemoryRateLimiter(
                limit=10,
                window_seconds=60,
                now_provider=self.clock.now,
            ),
        )

    def _persist_mood_entry(self, payload: MoodLogRequest, mood_date: date) -> MoodLogResponse:
        self.persisted_calls.append((payload, mood_date))
        return MoodLogResponse(log_id=uuid4(), mood_date=mood_date, status="ok")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        client_host: str,
        headers: dict[str, str] | None = None,
        json: dict[str, object] | None = None,
    ) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app, client=(client_host, 8787))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.request(method, path, headers=headers, json=json)

    def request(
        self,
        method: str,
        path: str,
        *,
        client_host: str,
        headers: dict[str, str] | None = None,
        json: dict[str, object] | None = None,
    ) -> httpx.Response:
        return asyncio.run(
            self._request(
                method,
                path,
                client_host=client_host,
                headers=headers,
                json=json,
            )
        )

    def test_missing_token_returns_401(self) -> None:
        response = self.request("POST", "/api/mood", client_host=SIMULATED_LAN_CLIENT_IP, json={"feeling": 6})

        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.persisted_calls, [])

    def test_bad_token_returns_401(self) -> None:
        response = self.request(
            "GET",
            "/api/health",
            client_host=LOOPBACK_IP,
            headers={"X-Mood-Token": "bad-token"},
        )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(self.app.state.health_checks, 0)

    def test_remote_get_is_rejected_before_handler_logic(self) -> None:
        response = self.request(
            "GET",
            "/api/health",
            client_host=SIMULATED_REMOTE_IP,
            headers=VALID_TOKEN_HEADERS,
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json(), {"detail": "Reads same-host only"})
        self.assertEqual(self.app.state.health_checks, 0)

    def test_loopback_get_is_accepted_with_valid_token(self) -> None:
        response = self.request(
            "GET",
            "/api/health",
            client_host=LOOPBACK_IP,
            headers=VALID_TOKEN_HEADERS,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["scope"], "retrospective_only")
        self.assertEqual(self.app.state.health_checks, 1)

    def test_same_host_lan_bind_ip_get_is_accepted(self) -> None:
        response = self.request(
            "GET",
            "/api/health",
            client_host=SIMULATED_SAME_HOST_IP,
            headers=VALID_TOKEN_HEADERS,
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(self.app.state.health_checks, 1)

    def test_lan_post_uses_test_persistence_override(self) -> None:
        response = self.request(
            "POST",
            "/api/mood",
            client_host=SIMULATED_LAN_CLIENT_IP,
            headers=VALID_TOKEN_HEADERS,
            json={
                "feeling": 7,
                "energy": 5,
                "notes": "Late dinner.",
                "context_chips": ["late_meal"],
                "logged_at_utc": "2026-05-24T03:30:00-04:00",
            },
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "ok")
        self.assertEqual(response.json()["mood_date"], "2026-05-23")
        self.assertEqual(len(self.persisted_calls), 1)
        self.assertEqual(self.persisted_calls[0][1], date(2026, 5, 23))
        self.assertEqual(
            self.persisted_calls[0][0].logged_at_utc,
            datetime(2026, 5, 24, 7, 30, tzinfo=UTC),
        )
        self.assertEqual(self.persisted_calls[0][0].context_chips, ("late_meal",))

    def test_app_does_not_enable_cors_middleware(self) -> None:
        middleware_names = [middleware.cls.__name__ for middleware in self.app.user_middleware]

        self.assertNotIn("CORSMiddleware", middleware_names)

    def test_docs_and_schema_routes_are_disabled(self) -> None:
        for path in ("/docs", "/docs/oauth2-redirect", "/openapi.json", "/redoc"):
            with self.subTest(path=path):
                response = self.request(
                    "GET",
                    path,
                    client_host=SIMULATED_REMOTE_IP,
                )

                self.assertEqual(response.status_code, 404)

    def test_placeholder_routes_stay_retrospective_only(self) -> None:
        insights_response = self.request(
            "GET",
            "/api/insights/latest_logged_day",
            client_host=LOOPBACK_IP,
            headers=VALID_TOKEN_HEADERS,
        )
        counterfactual_response = self.request(
            "GET",
            "/api/counterfactuals/latest_logged_day",
            client_host=LOOPBACK_IP,
            headers=VALID_TOKEN_HEADERS,
        )

        self.assertEqual(insights_response.status_code, 200)
        self.assertEqual(counterfactual_response.status_code, 200)
        self.assertEqual(insights_response.json()["detail"], "collecting model-ready days")
        self.assertEqual(counterfactual_response.json()["detail"], "insufficient stable signal")
        self.assertEqual(insights_response.json()["scope"], "retrospective_only")
        self.assertEqual(counterfactual_response.json()["scope"], "retrospective_only")
        self.assertNotIn("tomorrow", counterfactual_response.text.lower())
        self.assertNotIn("recommend", counterfactual_response.text.lower())

    def test_post_rate_limit_uses_local_in_memory_limiter(self) -> None:
        payload = {"feeling": 6}
        responses = [
            self.request(
                "POST",
                "/api/mood",
                client_host=SIMULATED_LAN_CLIENT_IP,
                headers=VALID_TOKEN_HEADERS,
                json=payload,
            )
            for _ in range(11)
        ]

        self.assertTrue(all(response.status_code == 200 for response in responses[:10]))
        self.assertEqual(responses[10].status_code, 429)
        self.assertEqual(responses[10].json(), {"detail": "Rate limit exceeded"})
        self.assertEqual(len(self.persisted_calls), 10)


if __name__ == "__main__":
    unittest.main()

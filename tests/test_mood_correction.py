from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import UUID

import httpx

from src.api import dependencies as api_dependencies
from src.api.app import create_app
from src.api.dependencies import build_api_settings
from src.warehouse.warehouse import connect_duckdb, select_current_mood_entries


FAKE_MOOD_TOKEN = "test-only-mood-token"
SIMULATED_SAME_HOST_IP = "198.51.100.10"
SIMULATED_LAN_CLIENT_IP = "198.51.100.77"
VALID_TOKEN_HEADERS = {"X-Mood-Token": FAKE_MOOD_TOKEN}


class MoodPersistenceIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "test-only-warehouse.duckdb"
        self.settings = build_api_settings(
            mood_token=FAKE_MOOD_TOKEN,
            lan_bind_ip=SIMULATED_SAME_HOST_IP,
            home_timezone="America/Toronto",
        )
        self.database_patch = patch.object(
            api_dependencies,
            "DEFAULT_DATABASE_PATH",
            self.database_path,
            create=True,
        )
        self.database_patch.start()
        self.app = create_app(settings=self.settings)

    def tearDown(self) -> None:
        self.database_patch.stop()
        self.tempdir.cleanup()

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

    def test_second_post_appends_and_promotes_current_mood_entry(self) -> None:
        first_response = self.request(
            "POST",
            "/api/mood",
            client_host=SIMULATED_LAN_CLIENT_IP,
            headers=VALID_TOKEN_HEADERS,
            json={
                "feeling": 4,
                "energy": 4,
                "notes": "rough afternoon",
                "context_chips": ["high_stress"],
                "logged_at_utc": "2026-05-24T03:30:00-04:00",
            },
        )
        second_response = self.request(
            "POST",
            "/api/mood",
            client_host=SIMULATED_LAN_CLIENT_IP,
            headers=VALID_TOKEN_HEADERS,
            json={
                "feeling": 6,
                "energy": 5,
                "notes": "late improvement",
                "context_chips": ["high_stress"],
                "logged_at_utc": "2026-05-24T03:45:00-04:00",
            },
        )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(first_response.json()["mood_date"], "2026-05-23")
        self.assertEqual(second_response.json()["mood_date"], "2026-05-23")
        self.assertEqual(self.database_path.parent, Path(self.tempdir.name))
        self.assertTrue(self.database_path.exists())

        conn = connect_duckdb(self.database_path, read_only=True)
        try:
            correction_rows = conn.execute(
                """
                SELECT log_id, supersedes_log_id
                FROM mood_entries
                WHERE mood_date = DATE '2026-05-23'
                ORDER BY logged_at_utc
                """
            ).fetchall()
            current_row = conn.execute(
                """
                SELECT mood_date, log_id
                FROM mood_current
                WHERE mood_date = DATE '2026-05-23'
                """
            ).fetchone()
            training_rows = select_current_mood_entries(conn)
        finally:
            conn.close()

        first_log_id = UUID(first_response.json()["log_id"])
        second_log_id = UUID(second_response.json()["log_id"])

        self.assertEqual(len(correction_rows), 2)
        self.assertEqual(correction_rows[0][1], None)
        self.assertEqual(correction_rows[1][1], first_log_id)
        self.assertEqual(current_row, (date(2026, 5, 23), second_log_id))
        self.assertEqual(len(training_rows), 1)
        self.assertEqual(training_rows[0].log_id, second_log_id)
        self.assertEqual(training_rows[0].supersedes_log_id, first_log_id)
        self.assertEqual(training_rows[0].feeling, 6)


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import asyncio
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import httpx

from src.api import dependencies as api_dependencies
from src.api.app import create_app
from src.api.dependencies import build_api_settings
from src.warehouse.locking import lock_path_for_database, warehouse_write_lock
from src.warehouse.warehouse import connect_duckdb


FAKE_MOOD_TOKEN = "test-mood-token"
SIMULATED_SAME_HOST_IP = "198.51.100.10"
SIMULATED_LAN_CLIENT_IP = "198.51.100.77"
VALID_TOKEN_HEADERS = {"X-Mood-Token": FAKE_MOOD_TOKEN}
MOOD_PAYLOAD = {"feeling": 6, "logged_at_utc": "2026-09-10T23:30:00-04:00"}


class MoodApiLockContentionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "test-only-warehouse.duckdb"
        self.settings = build_api_settings(
            mood_token=FAKE_MOOD_TOKEN,
            lan_bind_ip=SIMULATED_SAME_HOST_IP,
            home_timezone="America/Toronto",
        )
        self.patches = [
            patch.object(api_dependencies, "DEFAULT_DATABASE_PATH", self.database_path, create=True),
            patch.object(api_dependencies, "MOOD_WRITE_LOCK_TIMEOUT_SECONDS", 0.2),
        ]
        for item in self.patches:
            item.start()
        self.app = create_app(settings=self.settings)

    def tearDown(self) -> None:
        for item in reversed(self.patches):
            item.stop()
        self.tempdir.cleanup()

    async def _post(self) -> httpx.Response:
        transport = httpx.ASGITransport(app=self.app, client=(SIMULATED_LAN_CLIENT_IP, 8787))
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            return await client.post("/api/mood", headers=VALID_TOKEN_HEADERS, json=MOOD_PAYLOAD)

    def post(self) -> httpx.Response:
        return asyncio.run(self._post())

    def test_post_fails_visibly_while_the_warehouse_lock_is_held(self) -> None:
        with warehouse_write_lock(lock_path_for_database(self.database_path)):
            blocked = self.post()
            self.assertEqual(blocked.status_code, 503)
            self.assertEqual(blocked.json()["detail"], "Warehouse busy")
            self.assertFalse(self.database_path.exists(), "no unguarded fallback write may happen")

        released = self.post()
        self.assertEqual(released.status_code, 200)
        self.assertEqual(released.json()["mood_date"], "2026-09-10")

        conn = connect_duckdb(self.database_path, read_only=True)
        try:
            count = conn.execute("SELECT COUNT(*) FROM mood_entries").fetchone()
        finally:
            conn.close()
        self.assertEqual(count, (1,))
        self.assertFalse(Path(f"{self.database_path}.wal").exists())


if __name__ == "__main__":
    unittest.main()

from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from scripts.run_mood_form import (
    ALLOWED_ENV_KEYS,
    build_check_summary,
    read_env_file,
    resolve_settings,
    streamlit_command,
)


SAMPLE_ENV = """
# comment line
OPENAI_API_KEY=should-never-be-read
OURA_ACCESS_TOKEN='should-never-be-read'
export LAN_BIND_IP=192.0.2.10
MOOD_FORM_TOKEN="quoted-token-value"
HOME_TIMEZONE='America/Toronto'
HEALTH_HUB_DATABASE_PATH=/tmp/example/warehouse.duckdb
"""


class ReadEnvFileTest(unittest.TestCase):
    def test_reads_only_whitelisted_keys_and_strips_quotes(self) -> None:
        with TemporaryDirectory() as tempdir:
            env_file = Path(tempdir) / ".env.local"
            env_file.write_text(SAMPLE_ENV, encoding="utf-8")

            values = read_env_file(env_file, ALLOWED_ENV_KEYS)

        self.assertEqual(
            values,
            {
                "LAN_BIND_IP": "192.0.2.10",
                "MOOD_FORM_TOKEN": "quoted-token-value",
                "HOME_TIMEZONE": "America/Toronto",
                "HEALTH_HUB_DATABASE_PATH": "/tmp/example/warehouse.duckdb",
            },
        )
        self.assertNotIn("OPENAI_API_KEY", values)
        self.assertNotIn("OURA_ACCESS_TOKEN", values)

    def test_missing_file_yields_no_values(self) -> None:
        self.assertEqual(read_env_file(Path("/nonexistent/.env.local"), ALLOWED_ENV_KEYS), {})


class ResolveSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.env_file = Path(self.tempdir.name) / ".env.local"
        self.env_file.write_text(SAMPLE_ENV, encoding="utf-8")

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_process_environment_wins_over_the_file(self) -> None:
        settings = resolve_settings({"LAN_BIND_IP": "198.51.100.5"}, self.env_file)

        self.assertEqual(settings.errors, ())
        self.assertEqual(settings.bind_ip, "198.51.100.5")
        self.assertEqual(settings.token, "quoted-token-value")
        self.assertEqual(settings.home_timezone, "America/Toronto")
        self.assertEqual(settings.database_path, Path("/tmp/example/warehouse.duckdb"))

    def test_invalid_ip_and_missing_token_are_reported(self) -> None:
        settings = resolve_settings(
            {"LAN_BIND_IP": "not-an-ip", "MOOD_FORM_TOKEN": ""},
            Path(self.tempdir.name) / "missing.env",
        )

        self.assertIn("LAN_BIND_IP must be a valid IP address", settings.errors)
        self.assertIn("MOOD_FORM_TOKEN must be set and non-empty", settings.errors)

    def test_wildcard_and_multicast_bind_addresses_are_rejected(self) -> None:
        for address in ("0.0.0.0", "::", "224.0.0.1", "ff02::1"):
            with self.subTest(address=address):
                settings = resolve_settings({"LAN_BIND_IP": address}, self.env_file)
                self.assertTrue(
                    any("wildcard or multicast" in error for error in settings.errors),
                    settings.errors,
                )

    def test_loopback_and_private_addresses_are_accepted(self) -> None:
        for address in ("127.0.0.1", "::1", "192.168.1.20", "172.30.160.162"):
            with self.subTest(address=address):
                settings = resolve_settings({"LAN_BIND_IP": address}, self.env_file)
                self.assertEqual(settings.errors, ())

    def test_invalid_timezone_is_reported(self) -> None:
        settings = resolve_settings({"HOME_TIMEZONE": "Mars/Olympus"}, self.env_file)

        self.assertIn("HOME_TIMEZONE is not a valid timezone name", settings.errors)

    def test_defaults_apply_when_optional_keys_are_absent(self) -> None:
        settings = resolve_settings(
            {"LAN_BIND_IP": "192.0.2.10", "MOOD_FORM_TOKEN": "abc"},
            Path(self.tempdir.name) / "missing.env",
        )

        self.assertEqual(settings.errors, ())
        self.assertEqual(settings.home_timezone, "America/Toronto")
        self.assertTrue(str(settings.database_path).endswith("data/warehouse.duckdb"))

    def test_check_summary_and_command_never_carry_the_token(self) -> None:
        settings = resolve_settings({}, self.env_file)

        summary = build_check_summary(settings)
        serialized = json.dumps(summary)
        self.assertTrue(summary["token_present"])
        self.assertNotIn("quoted-token-value", serialized)
        self.assertEqual(summary["url"], "http://192.0.2.10:8501")

        command = streamlit_command(settings)
        self.assertNotIn("quoted-token-value", " ".join(command))
        self.assertIn("--server.address", command)
        self.assertEqual(command[command.index("--server.address") + 1], "192.0.2.10")


if __name__ == "__main__":
    unittest.main()

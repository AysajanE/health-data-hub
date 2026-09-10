from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from scripts import run_explainer


class RunExplainerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name).resolve()
        self.env_file = self.root / ".env.local"
        self.env = {
            "MOOD_FORM_TOKEN": "synthetic-explainer-token",
            "HOME_TIMEZONE": "America/Toronto",
            "HEALTH_HUB_DATABASE_PATH": str(self.root / "warehouse.duckdb"),
            "HEALTH_HUB_MODEL_DIR": str(self.root / "models"),
        }
        self.env_file.write_text(
            "\n".join(f'export {key}="{value}"' for key, value in self.env.items())
            + "\nLAN_BIND_IP=0.0.0.0\nOURA_ACCESS_TOKEN=unrelated-secret\n",
            encoding="utf-8",
        )

    def test_only_whitelisted_settings_are_resolved_and_bind_is_fixed(self) -> None:
        with patch("scripts.run_explainer.resolve_env", wraps=run_explainer.resolve_env) as resolver:
            settings = run_explainer.resolve_settings({}, self.env_file)

        self.assertEqual(resolver.call_args.args[0], run_explainer.ALLOWED_ENV_KEYS)
        self.assertEqual(
            set(run_explainer.ALLOWED_ENV_KEYS),
            {"MOOD_FORM_TOKEN", "HOME_TIMEZONE", "HEALTH_HUB_DATABASE_PATH", "HEALTH_HUB_MODEL_DIR"},
        )
        self.assertEqual(settings.errors, ())
        self.assertEqual(settings.bind_ip, "127.0.0.1")
        self.assertEqual(settings.port, 8502)
        self.assertEqual(settings.token, self.env["MOOD_FORM_TOKEN"])
        self.assertEqual(settings.database_path, self.root / "warehouse.duckdb")
        self.assertEqual(settings.model_dir, self.root / "models")

    def test_process_environment_wins_and_complete_settings_skip_file_read(self) -> None:
        values = dict(self.env, MOOD_FORM_TOKEN="process-token", LAN_BIND_IP="192.0.2.12")
        with patch("scripts.run_explainer.resolve_env") as resolver:
            settings = run_explainer.resolve_settings(values, self.env_file)

        resolver.assert_not_called()
        self.assertEqual(settings.errors, ())
        self.assertEqual(settings.token, "process-token")
        self.assertEqual(settings.bind_ip, "127.0.0.1")

    def test_partial_environment_reads_only_missing_settings(self) -> None:
        with patch("scripts.run_explainer.resolve_env", wraps=run_explainer.resolve_env) as resolver:
            settings = run_explainer.resolve_settings({"MOOD_FORM_TOKEN": "process-token"}, self.env_file)

        self.assertEqual(
            resolver.call_args.args[0],
            ("HOME_TIMEZONE", "HEALTH_HUB_DATABASE_PATH", "HEALTH_HUB_MODEL_DIR"),
        )
        self.assertEqual(settings.token, "process-token")
        self.assertEqual(settings.errors, ())

    def test_defaults_when_optional_values_are_missing(self) -> None:
        settings = run_explainer.resolve_settings(
            {"MOOD_FORM_TOKEN": "synthetic-token"}, self.root / "missing.env"
        )

        self.assertEqual(settings.errors, ())
        self.assertEqual(settings.home_timezone, "America/Toronto")
        self.assertEqual(settings.database_path, run_explainer.REPO_ROOT / "data" / "warehouse.duckdb")
        self.assertEqual(settings.model_dir, run_explainer.REPO_ROOT / "models")

    def test_missing_token_invalid_timezone_and_port_are_reported_safely(self) -> None:
        settings = run_explainer.resolve_settings(
            {"MOOD_FORM_TOKEN": " ", "HOME_TIMEZONE": "Mars/Olympus"},
            self.root / "missing.env",
            port=65536,
        )

        self.assertEqual(
            settings.errors,
            (
                "MOOD_FORM_TOKEN must be set and non-empty",
                "HOME_TIMEZONE is not a valid timezone name",
                "port must be between 1 and 65535",
            ),
        )
        for port in (0, -1):
            with self.subTest(port=port):
                result = run_explainer.resolve_settings(self.env, self.env_file, port=port)
                self.assertIn("port must be between 1 and 65535", result.errors)

    def test_summary_has_exact_safe_fields(self) -> None:
        settings = run_explainer.resolve_settings(self.env, self.env_file)
        summary = run_explainer.build_check_summary(settings)

        self.assertEqual(
            summary,
            {
                "status": "ok",
                "bind_ip": "127.0.0.1",
                "port": 8502,
                "home_timezone": "America/Toronto",
                "database_path": self.env["HEALTH_HUB_DATABASE_PATH"],
                "model_dir": self.env["HEALTH_HUB_MODEL_DIR"],
                "token_present": True,
                "url": "http://127.0.0.1:8502",
            },
        )
        self.assertNotIn(self.env["MOOD_FORM_TOKEN"], json.dumps(summary))

    def test_streamlit_command_uses_loopback_and_required_flags(self) -> None:
        settings = run_explainer.resolve_settings(self.env, self.env_file, port=8512)

        self.assertEqual(
            run_explainer.streamlit_command(settings),
            [
                sys.executable, "-m", "streamlit", "run", str(run_explainer.EXPLAINER_PATH),
                "--server.address", "127.0.0.1", "--server.port", "8512",
                "--server.headless", "true", "--browser.gatherUsageStats", "false",
                "--client.toolbarMode", "minimal",
            ],
        )

    def test_check_is_read_only_and_does_not_print_token(self) -> None:
        output = io.StringIO()
        with (
            patch.dict(os.environ, self.env, clear=True),
            patch("scripts.run_explainer.setup_permissions") as permissions,
            patch("scripts.run_explainer.os.execve") as execve,
            patch("scripts.run_explainer.resolve_env") as resolver,
            redirect_stdout(output),
        ):
            result = run_explainer.main(["--check"])

        self.assertEqual(result, 0)
        self.assertEqual(json.loads(output.getvalue())["url"], "http://127.0.0.1:8502")
        self.assertNotIn(self.env["MOOD_FORM_TOKEN"], output.getvalue())
        permissions.assert_not_called()
        execve.assert_not_called()
        resolver.assert_not_called()

    def test_launch_prepares_permissions_prints_one_url_and_passes_settings(self) -> None:
        output = io.StringIO()
        with (
            patch.dict(os.environ, {"LAN_BIND_IP": "0.0.0.0"}, clear=True),
            patch("scripts.run_explainer.setup_permissions", return_value={"status": "ok"}) as permissions,
            patch("scripts.run_explainer.os.execve") as execve,
            redirect_stdout(output),
        ):
            result = run_explainer.main(["--env-file", str(self.env_file), "--port", "8512"])

        self.assertEqual(result, 0)
        permissions.assert_called_once_with(run_explainer.REPO_ROOT)
        self.assertEqual(output.getvalue(), "Explainer: http://127.0.0.1:8512\n")
        self.assertNotIn(self.env["MOOD_FORM_TOKEN"], output.getvalue())
        executable, command, child_env = execve.call_args.args
        self.assertEqual(executable, sys.executable)
        self.assertEqual(command[command.index("--server.address") + 1], "127.0.0.1")
        for key, value in self.env.items():
            self.assertEqual(child_env[key], value)
        self.assertNotIn("OURA_ACCESS_TOKEN", child_env)

    def test_invalid_settings_prevent_launch(self) -> None:
        output = io.StringIO()
        with (
            patch.dict(os.environ, {}, clear=True),
            patch("scripts.run_explainer.setup_permissions") as permissions,
            patch("scripts.run_explainer.os.execve") as execve,
            redirect_stdout(output),
        ):
            result = run_explainer.main(["--env-file", str(self.root / "missing.env")])

        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "error")
        permissions.assert_not_called()
        execve.assert_not_called()

    def test_permission_failure_prevents_launch(self) -> None:
        output = io.StringIO()
        with (
            patch.dict(os.environ, self.env, clear=True),
            patch("scripts.run_explainer.setup_permissions", return_value={"status": "error", "errors": ["fixture failure"]}),
            patch("scripts.run_explainer.os.execve") as execve,
            redirect_stdout(output),
        ):
            result = run_explainer.main([])

        self.assertEqual(result, 1)
        self.assertEqual(json.loads(output.getvalue())["errors"], ["fixture failure"])
        execve.assert_not_called()


if __name__ == "__main__":
    unittest.main()

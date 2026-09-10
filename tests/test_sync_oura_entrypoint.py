from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime, timezone
from io import StringIO
import json
from pathlib import Path
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from scripts import sync_oura as entrypoint
from src.ingestion.oura_auth import OuraAuthError
from src.ingestion.oura_sync import SyncReport


NOW = datetime(2026, 9, 10, 2, 30, tzinfo=timezone.utc)
SECRET = "fixture-client-secret-never-print"
TOKEN = "fixture-access-token-never-print"


class SyncOuraEntrypointTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.env_file = self.root / "settings.env"
        self.env_file.write_text(
            "OURA_CLIENT_ID=fixture-client\n"
            f"OURA_CLIENT_SECRET={SECRET}\n"
            "HOME_TIMEZONE=America/Toronto\n"
            f"HEALTH_HUB_DATABASE_PATH={self.root / 'warehouse.duckdb'}\n",
            encoding="utf-8",
        )
        self.token_file = self.root / "tokens.json"
        self.status_file = self.root / "status" / "sync.json"
        self.args = [
            "--env-file", str(self.env_file),
            "--token-file", str(self.token_file),
            "--status-file", str(self.status_file),
        ]
        self.env_patch = patch.dict("os.environ", {}, clear=True)
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.policy = object()
        self.policy_patch = patch.object(entrypoint, "load_sleep_provider_policy", return_value=self.policy)
        self.policy_patch.start()
        self.addCleanup(self.policy_patch.stop)

    def run_main(self, sync_fn: Mock, extra: list[str] | None = None) -> tuple[int, str, str]:
        stdout, stderr = StringIO(), StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            result = entrypoint.main(self.args + (extra or []), now=NOW, sync_fn=sync_fn)
        output, error = stdout.getvalue(), stderr.getvalue()
        self.assertNotIn(SECRET, output + error)
        self.assertNotIn(TOKEN, output + error)
        return result, output, error

    def test_window_uses_local_date_and_default_fourteen_days(self) -> None:
        fake = Mock(return_value=SyncReport(status="ok", main_sleep_nights=3, rows_upserted=3))
        code, output, error = self.run_main(fake)
        self.assertEqual(code, 0)
        self.assertEqual(output, "ok nights=3 upserted=3 features=0\n")
        self.assertEqual(error, "")
        kwargs = fake.call_args.kwargs
        self.assertEqual(kwargs["start_date"], date(2026, 8, 26))
        self.assertEqual(kwargs["end_date"], date(2026, 9, 9))
        self.assertEqual(str(kwargs["home_tz"]), "America/Toronto")
        self.assertEqual(kwargs["database_path"], self.root / "warehouse.duckdb")
        self.assertIs(kwargs["provider_policy"], self.policy)
        self.assertEqual(kwargs["store"].path, self.token_file)
        self.assertEqual(kwargs["now"], NOW)

    def test_custom_days_and_start_end_overrides(self) -> None:
        cases = [
            (["--days", "3"], date(2026, 9, 6), date(2026, 9, 9)),
            (["--start", "2026-09-01", "--end", "2026-09-08"], date(2026, 9, 1), date(2026, 9, 8)),
            (["--end", "2026-09-08"], date(2026, 8, 26), date(2026, 9, 8)),
        ]
        for arguments, start, end in cases:
            with self.subTest(arguments=arguments):
                fake = Mock(return_value=SyncReport(status="ok"))
                code, _, _ = self.run_main(fake, arguments)
                self.assertEqual(code, 0)
                self.assertEqual(fake.call_args.kwargs["start_date"], start)
                self.assertEqual(fake.call_args.kwargs["end_date"], end)

    def test_process_environment_overrides_timezone_and_database(self) -> None:
        fake = Mock(return_value=SyncReport(status="ok"))
        database = self.root / "overridden.duckdb"
        with patch.dict("os.environ", {"HOME_TIMEZONE": "Asia/Tokyo", "HEALTH_HUB_DATABASE_PATH": str(database)}):
            code, _, _ = self.run_main(fake)
        self.assertEqual(code, 0)
        self.assertEqual(fake.call_args.kwargs["end_date"], date(2026, 9, 10))
        self.assertEqual(fake.call_args.kwargs["database_path"], database)

    def test_report_status_exit_codes_and_private_status_file(self) -> None:
        for status, expected_exit in (("ok", 0), ("auth_required", 2), ("error", 1)):
            with self.subTest(status=status):
                fake = Mock(return_value=SyncReport(status=status))
                code, output, error = self.run_main(fake, ["--json"])
                self.assertEqual(code, expected_exit)
                payload = json.loads(output)
                self.assertEqual(payload["status"], status)
                self.assertEqual(payload["schema"], "oura_sync_status.v1")
                self.assertEqual(json.loads(self.status_file.read_text()), payload)
                self.assertEqual(stat.S_IMODE(self.status_file.stat().st_mode), 0o600)
                self.assertEqual(stat.S_IMODE(self.status_file.parent.stat().st_mode), 0o700)
                self.assertEqual(list(self.status_file.parent.iterdir()), [self.status_file])
                if status == "auth_required":
                    self.assertEqual(error, entrypoint.AUTHORIZATION_HINT + "\n")
                else:
                    self.assertEqual(error, "")

    def test_setup_and_unexpected_sync_errors_do_not_echo_secrets(self) -> None:
        for exception, expected_exit in ((OuraAuthError(TOKEN), 2), (RuntimeError(SECRET), 1)):
            with self.subTest(exception=type(exception).__name__):
                fake = Mock(side_effect=exception)
                code, output, _ = self.run_main(fake, ["--json"])
                self.assertEqual(code, expected_exit)
                self.assertEqual(
                    json.loads(output)["error"],
                    f"{type(exception).__name__}: Oura sync setup failed",
                )

    def test_missing_credentials_are_auth_required_and_never_sync(self) -> None:
        self.env_file.write_text("HOME_TIMEZONE=America/Toronto\n", encoding="utf-8")
        fake = Mock()
        code, output, error = self.run_main(fake, ["--json"])
        self.assertEqual(code, 2)
        self.assertEqual(json.loads(output)["status"], "auth_required")
        self.assertIn("scripts/oura_authorize.py", error)
        fake.assert_not_called()

    def test_invalid_window_fails_without_sync(self) -> None:
        for args in (["--days", "-1"], ["--start", "2026-09-10", "--end", "2026-09-09"]):
            with self.subTest(args=args):
                fake = Mock()
                code, output, _ = self.run_main(fake, args + ["--json"])
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(output)["status"], "error")
                fake.assert_not_called()

    def test_status_write_error_is_redacted_and_returns_error(self) -> None:
        fake = Mock(return_value=SyncReport(status="ok"))
        with patch.object(entrypoint, "write_private_json", side_effect=OSError(TOKEN)):
            code, output, _ = self.run_main(fake, ["--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["error"], "OSError: Could not write Oura sync status")

    def test_status_collisions_never_sync_or_write_status(self) -> None:
        database = self.root / "warehouse.duckdb"
        protected = (
            self.token_file,
            self.token_file.with_suffix(".json.tmp"),
            database,
            Path(str(database) + ".wal"),
            self.root / ".healthhub.lock",
            self.env_file,
        )
        original_env = self.env_file.read_text()
        for status_path in protected:
            with self.subTest(status_path=status_path):
                fake = Mock()
                with patch.object(entrypoint, "write_private_json") as writer:
                    code, output, _ = self.run_main(fake, ["--status-file", str(status_path), "--json"])
                self.assertEqual(code, 1)
                self.assertEqual(json.loads(output)["status"], "error")
                fake.assert_not_called()
                writer.assert_not_called()
        self.assertEqual(self.env_file.read_text(), original_env)

    def test_status_temporary_path_cannot_collide_with_token_or_database(self) -> None:
        temporary_path = self.status_file.with_suffix(".json.tmp")
        cases = (
            (["--token-file", str(temporary_path)], {}),
            (["--token-file", str(self.status_file.with_suffix(".other"))], {}),
            ([], {"HEALTH_HUB_DATABASE_PATH": str(temporary_path)}),
        )
        for extra_args, environment in cases:
            with self.subTest(extra_args=extra_args, environment=environment):
                fake = Mock()
                with (
                    patch.dict("os.environ", environment),
                    patch.object(entrypoint, "write_private_json") as writer,
                ):
                    code, _, _ = self.run_main(fake, extra_args + ["--json"])
                self.assertEqual(code, 1)
                fake.assert_not_called()
                writer.assert_not_called()

    def test_status_alias_and_temporary_alias_are_rejected_before_sync(self) -> None:
        self.status_file.parent.mkdir()
        temporary_path = self.status_file.with_suffix(".json.tmp")
        for target in (self.env_file, self.status_file):
            with self.subTest(target=target):
                temporary_path.symlink_to(target)
                fake = Mock()
                with patch.object(entrypoint, "write_private_json") as writer:
                    code, _, _ = self.run_main(fake, ["--json"])
                self.assertEqual(code, 1)
                fake.assert_not_called()
                writer.assert_not_called()
                temporary_path.unlink()

    def test_production_call_leaves_sync_clock_unfrozen(self) -> None:
        fake = Mock(return_value=SyncReport(status="ok"))
        with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
            code = entrypoint.main(self.args, sync_fn=fake)
        self.assertEqual(code, 0)
        self.assertIsNone(fake.call_args.kwargs["now"])


if __name__ == "__main__":
    unittest.main()

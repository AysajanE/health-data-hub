from __future__ import annotations

from pathlib import Path
import plistlib
from tempfile import TemporaryDirectory
import unittest

from scripts.install_launchd import (
    BACKUP_LABEL,
    EXPLAINER_LABEL,
    MOOD_FORM_LABEL,
    MOOD_PROMPT_LABEL,
    OURA_SYNC_LABEL,
    RETRAIN_LABEL,
    build_plists,
    plist_path,
    write_plists,
)


class BuildPlistsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.repo_root = Path("/tmp/example-repo")
        self.python = self.repo_root / ".venv" / "bin" / "python"
        self.log_dir = Path("/tmp/example-logs")
        self.plists = build_plists(repo_root=self.repo_root, python=self.python, log_dir=self.log_dir)

    def test_six_agents_with_expected_labels(self) -> None:
        self.assertEqual(
            set(self.plists),
            {MOOD_FORM_LABEL, EXPLAINER_LABEL, OURA_SYNC_LABEL, RETRAIN_LABEL, BACKUP_LABEL, MOOD_PROMPT_LABEL},
        )

    def test_mood_prompt_asks_early_and_late_evening(self) -> None:
        payload = self.plists[MOOD_PROMPT_LABEL]
        self.assertFalse(payload["RunAtLoad"])
        self.assertNotIn("KeepAlive", payload)
        self.assertEqual(payload["ProcessType"], "Interactive")
        self.assertEqual(
            payload["StartCalendarInterval"],
            [{"Hour": 18, "Minute": 20}, {"Hour": 21, "Minute": 30}],
        )
        self.assertTrue(payload["ProgramArguments"][1].endswith("scripts/mood_prompt.py"))
        self.assertEqual(payload["StandardOutPath"], "/tmp/example-logs/healthhub-mood-prompt.log")

    def test_backup_runs_after_the_retrain_with_notification_on_failure(self) -> None:
        payload = self.plists[BACKUP_LABEL]
        self.assertFalse(payload["RunAtLoad"])
        self.assertEqual(payload["StartCalendarInterval"], {"Hour": 23, "Minute": 30})
        self.assertTrue(payload["ProgramArguments"][1].endswith("scripts/backup_snapshot.py"))
        self.assertIn("--notify", payload["ProgramArguments"])
        self.assertIn("--json", payload["ProgramArguments"])
        for label, payload in self.plists.items():
            self.assertEqual(payload["Label"], label)
            self.assertEqual(payload["WorkingDirectory"], str(self.repo_root))
            self.assertEqual(payload["ProgramArguments"][0], str(self.python))
            self.assertTrue(payload["StandardOutPath"].startswith(str(self.log_dir)))

    def test_mood_form_is_kept_alive(self) -> None:
        payload = self.plists[MOOD_FORM_LABEL]
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        self.assertTrue(payload["ProgramArguments"][1].endswith("scripts/run_mood_form.py"))

    def test_explainer_is_kept_alive(self) -> None:
        payload = self.plists[EXPLAINER_LABEL]
        self.assertTrue(payload["RunAtLoad"])
        self.assertTrue(payload["KeepAlive"])
        self.assertTrue(payload["ProgramArguments"][1].endswith("scripts/run_explainer.py"))

    def test_sync_runs_morning_and_early_evening(self) -> None:
        payload = self.plists[OURA_SYNC_LABEL]
        self.assertFalse(payload["RunAtLoad"])
        self.assertEqual(
            payload["StartCalendarInterval"],
            [{"Hour": 8, "Minute": 0}, {"Hour": 19, "Minute": 30}],
        )
        self.assertTrue(payload["ProgramArguments"][1].endswith("scripts/sync_oura.py"))

    def test_retrain_runs_after_the_evening_log_through_the_redacting_wrapper(self) -> None:
        payload = self.plists[RETRAIN_LABEL]
        self.assertEqual(payload["StartCalendarInterval"], {"Hour": 23, "Minute": 0})
        self.assertTrue(payload["ProgramArguments"][1].endswith("scripts/nightly_retrain.py"))
        self.assertNotIn("--json", payload["ProgramArguments"])
        for other in (MOOD_FORM_LABEL, EXPLAINER_LABEL, OURA_SYNC_LABEL, BACKUP_LABEL):
            self.assertFalse(
                any(arg.endswith("retrain_model.py") for arg in self.plists[other]["ProgramArguments"])
            )

    def test_no_secrets_in_environment(self) -> None:
        for payload in self.plists.values():
            self.assertEqual(set(payload["EnvironmentVariables"]), {"PATH", "PYTHONUNBUFFERED"})


class BootstrapRetryTest(unittest.TestCase):
    def test_retries_transient_io_error_then_succeeds(self) -> None:
        from subprocess import CompletedProcess
        from unittest.mock import patch

        from scripts import install_launchd

        outcomes = iter(
            [
                CompletedProcess(["launchctl", "bootout"], 0, stdout="", stderr=""),
                CompletedProcess(["launchctl", "bootstrap"], 5, stdout="", stderr="Bootstrap failed: 5: Input/output error"),
                CompletedProcess(["launchctl", "bootstrap"], 5, stdout="", stderr="Bootstrap failed: 5: Input/output error"),
                CompletedProcess(["launchctl", "bootstrap"], 0, stdout="", stderr=""),
            ]
        )
        with patch.object(install_launchd, "_launchctl", side_effect=lambda *args: next(outcomes)), patch.object(
            install_launchd.time, "sleep"
        ) as sleep:
            result = install_launchd.bootstrap("com.healthhub.mood-form", Path("/tmp/x.plist"))

        self.assertTrue(result["ok"])
        self.assertEqual(result["attempts"], 3)
        self.assertEqual(sleep.call_count, 2)

    def test_non_transient_failure_is_not_retried(self) -> None:
        from subprocess import CompletedProcess
        from unittest.mock import patch

        from scripts import install_launchd

        outcomes = iter(
            [
                CompletedProcess(["launchctl", "bootout"], 0, stdout="", stderr=""),
                CompletedProcess(["launchctl", "bootstrap"], 1, stdout="", stderr="Bootstrap failed: 1: Operation not permitted"),
            ]
        )
        with patch.object(install_launchd, "_launchctl", side_effect=lambda *args: next(outcomes)), patch.object(
            install_launchd.time, "sleep"
        ) as sleep:
            result = install_launchd.bootstrap("com.healthhub.mood-form", Path("/tmp/x.plist"))

        self.assertFalse(result["ok"])
        self.assertEqual(result["attempts"], 1)
        self.assertEqual(sleep.call_count, 0)


class OnlyFlagTest(unittest.TestCase):
    def test_only_limits_install_to_the_named_agent(self) -> None:
        from unittest.mock import patch

        from scripts import install_launchd

        with TemporaryDirectory() as tempdir, patch.object(
            install_launchd, "bootstrap", side_effect=lambda label, path: {"label": label, "ok": True}
        ) as boot, patch.object(install_launchd, "LOG_DIR", Path(tempdir) / "logs"):
            code = install_launchd.main(
                ["--install", "--only", MOOD_PROMPT_LABEL, "--launch-agents-dir", tempdir]
            )
            written = sorted(path.name for path in Path(tempdir).glob("*.plist"))

        self.assertEqual(code, 0)
        self.assertEqual([call.args[0] for call in boot.call_args_list], [MOOD_PROMPT_LABEL])
        self.assertEqual(written, [f"{MOOD_PROMPT_LABEL}.plist"])

    def test_unknown_label_is_rejected(self) -> None:
        from scripts import install_launchd

        self.assertEqual(install_launchd.main(["--status", "--only", "com.healthhub.nope"]), 2)


class WritePlistsTest(unittest.TestCase):
    def test_writes_parseable_plists(self) -> None:
        plists = build_plists(
            repo_root=Path("/tmp/example-repo"),
            python=Path("/tmp/example-repo/.venv/bin/python"),
            log_dir=Path("/tmp/example-logs"),
        )
        with TemporaryDirectory() as tempdir:
            target = Path(tempdir) / "LaunchAgents"
            written = write_plists(plists, target)

            self.assertEqual(sorted(written), sorted(plist_path(label, target) for label in plists))
            for path in written:
                with path.open("rb") as handle:
                    payload = plistlib.load(handle)
                self.assertEqual(payload["Label"], path.stem)


if __name__ == "__main__":
    unittest.main()

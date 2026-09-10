from __future__ import annotations

from pathlib import Path
import plistlib
from tempfile import TemporaryDirectory
import unittest

from scripts.install_launchd import (
    EXPLAINER_LABEL,
    MOOD_FORM_LABEL,
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

    def test_four_agents_with_expected_labels(self) -> None:
        self.assertEqual(
            set(self.plists),
            {MOOD_FORM_LABEL, EXPLAINER_LABEL, OURA_SYNC_LABEL, RETRAIN_LABEL},
        )
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
        for other in (MOOD_FORM_LABEL, EXPLAINER_LABEL, OURA_SYNC_LABEL):
            self.assertFalse(
                any(arg.endswith("retrain_model.py") for arg in self.plists[other]["ProgramArguments"])
            )

    def test_no_secrets_in_environment(self) -> None:
        for payload in self.plists.values():
            self.assertEqual(set(payload["EnvironmentVariables"]), {"PATH", "PYTHONUNBUFFERED"})


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

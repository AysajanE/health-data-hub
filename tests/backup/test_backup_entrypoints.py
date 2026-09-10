from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import timedelta
import io
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from scripts import backup_snapshot as backup_cli
from scripts import restore_snapshot as restore_cli
from src.backup.snapshot import sha256_file
from tests.backup.test_snapshot import HEALTH_FIXTURE, NOW, SnapshotFixture, TOKEN_FIXTURE


class BackupEntrypointsTest(SnapshotFixture):
    def setUp(self):
        super().setUp()
        self.seed()
        for cli in (backup_cli, restore_cli):
            for name, value in {
                "REPO_ROOT": self.root, "DEFAULT_DATABASE_PATH": self.database,
                "DEFAULT_DESTINATION": self.destination, "DEFAULT_PASSPHRASE_PATH": self.key,
            }.items():
                self.enterContext(patch.object(cli, name, value))
            self.enterContext(patch.object(cli, "OpensslCipher", return_value=self.cipher))
        self.run_process = self.enterContext(patch.object(
            backup_cli.subprocess, "run",
            return_value=subprocess.CompletedProcess([], 0, stdout="abcdef1\n", stderr=""),
        ))

    def invoke(self, cli, argv, *, allow_show=False):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            try:
                code = cli.main(argv)
            except SystemExit as error:
                code = error.code
        output, errors = stdout.getvalue(), stderr.getvalue()
        if not allow_show:
            self.assertNotIn(self.key.read_text().strip(), output + errors)
        self.assertNotIn(TOKEN_FIXTURE, output + errors)
        self.assertNotIn(HEALTH_FIXTURE, output + errors)
        return code, output, errors

    def test_init_key_without_show_prints_only_path_and_mode(self):
        key = self.base / "new/key"
        code, output, errors = self.invoke(backup_cli, ["--init-key", "--passphrase-file", str(key)])
        self.assertEqual(code, 0)
        self.assertEqual(errors, "")
        self.assertEqual(output, f"passphrase_file={key} mode=0600\n")
        self.assertNotIn(key.read_text().strip(), output)
        self.run_process.assert_not_called()

    def test_no_option_ever_prints_the_passphrase(self):
        # The former --show option is gone: the passphrase is copied from the
        # private file by the owner, never written to stdout.
        key = self.base / "show/key"
        code, output, errors = self.invoke(backup_cli, ["--init-key", "--show", "--passphrase-file", str(key)])
        self.assertEqual(code, 2)
        self.assertIn("unrecognized arguments: --show", errors)
        self.assertFalse(key.exists())
        code, output, errors = self.invoke(backup_cli, ["--init-key", "--passphrase-file", str(key)])
        self.assertEqual(code, 0)
        self.assertNotIn(key.read_text().strip(), output + errors)
        self.assertEqual(self.cipher.calls, [])

    def test_init_key_json_and_overwrite_error(self):
        key = self.base / "json/key"
        code, output, _ = self.invoke(backup_cli, ["--init-key", "--json", "--passphrase-file", str(key)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output), {"passphrase_file": str(key), "mode": "0600"})
        code, output, _ = self.invoke(backup_cli, ["--init-key", "--passphrase-file", str(key)])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["error_type"], "SnapshotError")
        self.assertNotIn(key.read_text().strip(), output)

    def test_backup_ok_json_and_text_report(self):
        code, output, errors = self.invoke(backup_cli, ["--json"])
        self.assertEqual((code, errors), (0, ""))
        report = json.loads(output)
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["file_count"], 6)
        self.assertEqual(report["app_commit"], "abcdef1")
        self.assertEqual(report["destination"], str(self.destination))
        self.assertEqual(self.cipher.calls[0][0], "encrypt")
        self.assertEqual(self.run_process.call_args.args[0], ["git", "rev-parse", "--short", "HEAD"])
        code, output, _ = self.invoke(backup_cli, ["--destination", str(self.base / "second-snapshots"), "--include-env"])
        self.assertEqual(code, 0)
        self.assertRegex(output, r"^ok snapshot=hh-snapshot-\d{8}T\d{6}Z.tar.gz.enc files=7 bytes=\d+ kept=1 pruned=0\n$")

    def test_errors_are_json_and_raw_exception_details_are_redacted(self):
        code, output, errors = self.invoke(backup_cli, ["--passphrase-file", str(self.base / "missing"), "--json"])
        self.assertEqual((code, errors), (1, ""))
        self.assertEqual(json.loads(output), {
            "status": "error", "error_type": "SnapshotError",
            "message": "backup passphrase missing; run scripts/backup_snapshot.py --init-key",
        })
        with patch.object(backup_cli, "create_snapshot", side_effect=RuntimeError(self.key.read_text() + TOKEN_FIXTURE)):
            code, output, errors = self.invoke(backup_cli, ["--json"])
        self.assertEqual((code, errors), (1, ""))
        self.assertEqual(json.loads(output), {"status": "error", "error_type": "RuntimeError", "message": "backup failed"})

    def test_git_failure_does_not_block_backup_or_leak_stderr(self):
        self.run_process.return_value = subprocess.CompletedProcess([], 1, stdout="", stderr=TOKEN_FIXTURE)
        code, output, _ = self.invoke(backup_cli, ["--json"])
        self.assertEqual(code, 0)
        self.assertIsNone(json.loads(output)["app_commit"])

    def test_notification_only_requested_on_failure_and_contains_no_details(self):
        code, _, _ = self.invoke(backup_cli, ["--notify", "--json"])
        self.assertEqual(code, 0)
        self.assertFalse(any(call.args[0][0] == "/usr/bin/osascript" for call in self.run_process.call_args_list))
        self.run_process.reset_mock()
        self.invoke(backup_cli, ["--passphrase-file", str(self.base / "missing")])
        self.assertFalse(any(call.args[0][0] == "/usr/bin/osascript" for call in self.run_process.call_args_list))
        self.run_process.reset_mock()
        code, _, _ = self.invoke(backup_cli, ["--notify", "--passphrase-file", str(self.base / "missing")])
        self.assertEqual(code, 1)
        calls = [call for call in self.run_process.call_args_list if call.args[0][0] == "/usr/bin/osascript"]
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0].args[0], [
            "/usr/bin/osascript", "-e",
            'display notification "Health Data Hub backup failed" with title "Health Data Hub"',
        ])
        self.assertEqual(calls[0].kwargs, {"check": False, "capture_output": True})

    def test_latest_selection_verify_only_and_no_live_writes(self):
        self.backup(now=NOW)
        newest, _ = self.backup(now=NOW + timedelta(days=1))
        before = {path.relative_to(self.root): sha256_file(path) for path in self.root.rglob("*") if path.is_file()}
        code, output, errors = self.invoke(restore_cli, ["--snapshot", "latest", "--verify-only", "--json"])
        self.assertEqual((code, errors), (0, ""))
        report = json.loads(output)
        self.assertEqual(report["snapshot"], newest.name)
        self.assertTrue(report["verify_only"])
        self.assertIsNone(report["restored_to"])
        self.assertEqual(report["warehouse"]["mood_current"], 1)
        after = {path.relative_to(self.root): sha256_file(path) for path in self.root.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(self.cipher.calls[-1][1], newest)
        self.assertFalse(self.cipher.calls[-1][2].parent.exists())

    def test_restore_default_and_explicit_into_are_fresh_and_reported(self):
        snapshot, _ = self.backup()
        before = sha256_file(self.database)
        code, output, errors = self.invoke(restore_cli, ["--snapshot", str(snapshot), "--json"])
        self.assertEqual((code, errors), (0, ""))
        report = json.loads(output)
        restored = Path(report["restored_to"])
        self.assertEqual(restored.parent, self.root / "data/restore")
        self.assertTrue(restored.name.startswith("restore-"))
        self.assertTrue((restored / "data/warehouse.duckdb").is_file())
        self.assertEqual(sha256_file(self.database), before)
        into = self.base / "explicit-restore"
        code, output, _ = self.invoke(restore_cli, ["--snapshot", str(snapshot), "--into", str(into)])
        self.assertEqual(code, 0)
        self.assertEqual(output, f"ok snapshot={snapshot.name} files=6 restored_to={into}\n")

    def test_in_place_without_force_exits_two_before_reading_snapshot(self):
        before = sha256_file(self.database)
        code, output, errors = self.invoke(restore_cli, ["--snapshot", str(self.base / "missing"), "--in-place"])
        self.assertEqual(code, 2)
        self.assertEqual(output, "")
        self.assertIn("--in-place requires --force", errors)
        self.assertEqual(self.cipher.calls, [])
        self.assertEqual(sha256_file(self.database), before)

    def test_force_in_place_and_conflicting_options(self):
        snapshot, _ = self.backup()
        code, output, _ = self.invoke(restore_cli, ["--snapshot", str(snapshot), "--in-place", "--force", "--json"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output)["restored_to"], "in_place")
        self.assertTrue(Path(json.loads(output)["moved_aside"]).is_dir())
        for options in (["--force"], ["--into", str(self.base / "out"), "--in-place", "--force"],
                        ["--verify-only", "--in-place", "--force"]):
            code, _, _ = self.invoke(restore_cli, ["--snapshot", str(snapshot), *options])
            self.assertEqual(code, 2)

    def test_restore_missing_latest_and_unexpected_error_are_redacted(self):
        code, output, _ = self.invoke(restore_cli, ["--snapshot", "latest", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output)["message"], "no snapshots found")
        with patch.object(restore_cli, "restore_snapshot", side_effect=RuntimeError(HEALTH_FIXTURE + self.key.read_text())):
            code, output, _ = self.invoke(restore_cli, ["--snapshot", "fixture.enc", "--json"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output), {"status": "error", "error_type": "RuntimeError", "message": "restore failed"})

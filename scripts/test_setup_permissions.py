from __future__ import annotations

from pathlib import Path
from stat import S_IMODE
from tempfile import TemporaryDirectory
import unittest

from scripts.setup_permissions import inspect_permissions, setup_permissions


class SetupPermissionsTests(unittest.TestCase):
    def test_setup_permissions_creates_managed_roots_with_private_modes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            report = setup_permissions(root)

            self.assertEqual(report["status"], "ok")
            self.assertEqual(
                report["changed_paths"],
                ["data", "models", "private", "private/evidence"],
            )
            for relative_path in ("data", "private", "private/evidence", "models"):
                self.assertEqual(S_IMODE((root / relative_path).stat().st_mode), 0o700)

    def test_setup_permissions_secures_managed_directories_and_sensitive_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            warehouse_path = root / "data" / "warehouse.duckdb"
            secret_path = root / "data" / "secrets" / "oura_tokens.json"
            quarantine_path = root / "data" / "quarantine" / "2026-05-24-failure.json"
            raw_path = root / "data" / "raw" / "oura" / "2026-05-24.json"
            snapshot_path = root / "data" / "snapshots" / "warehouse-2026-05-24.duckdb"
            lock_path = root / "data" / ".healthhub.lock"
            activation_path = root / "private" / "evidence" / "S11" / "activation" / "proof.json"
            model_path = root / "models" / "ridge.joblib"

            for path in (
                warehouse_path,
                secret_path,
                quarantine_path,
                raw_path,
                snapshot_path,
                lock_path,
                activation_path,
                model_path,
            ):
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("private", encoding="utf-8")
                path.chmod(0o644)

            report = setup_permissions(root)

            self.assertEqual(report["status"], "ok")
            self.assertEqual(S_IMODE((root / "data").stat().st_mode), 0o700)
            self.assertEqual(S_IMODE((root / "data" / "secrets").stat().st_mode), 0o700)
            self.assertEqual(S_IMODE((root / "data" / "quarantine").stat().st_mode), 0o700)
            self.assertEqual(S_IMODE((root / "data" / "raw").stat().st_mode), 0o700)
            self.assertEqual(S_IMODE((root / "data" / "snapshots").stat().st_mode), 0o700)
            self.assertEqual(S_IMODE((root / "private").stat().st_mode), 0o700)
            self.assertEqual(S_IMODE((root / "private" / "evidence").stat().st_mode), 0o700)
            self.assertEqual(S_IMODE(activation_path.parent.stat().st_mode), 0o700)
            self.assertEqual(S_IMODE((root / "models").stat().st_mode), 0o700)
            self.assertEqual(S_IMODE(warehouse_path.stat().st_mode), 0o600)
            self.assertEqual(S_IMODE(secret_path.stat().st_mode), 0o600)
            self.assertEqual(S_IMODE(quarantine_path.stat().st_mode), 0o600)
            self.assertEqual(S_IMODE(raw_path.stat().st_mode), 0o600)
            self.assertEqual(S_IMODE(snapshot_path.stat().st_mode), 0o600)
            self.assertEqual(S_IMODE(lock_path.stat().st_mode), 0o600)
            self.assertEqual(S_IMODE(activation_path.stat().st_mode), 0o600)
            self.assertEqual(S_IMODE(model_path.stat().st_mode), 0o600)

            second_report = setup_permissions(root)
            self.assertEqual(second_report["status"], "ok")
            self.assertEqual(second_report["changed_paths"], [])

    def test_setup_permissions_fails_closed_on_symlink_in_managed_tree(self) -> None:
        with TemporaryDirectory() as temp_dir, TemporaryDirectory() as outside_dir:
            root = Path(temp_dir)
            outside = Path(outside_dir)
            outside_file = outside / "outside.json"
            outside_file.write_text("do not touch", encoding="utf-8")
            outside_file.chmod(0o644)
            (root / "data").mkdir()
            (root / "data" / "escape").symlink_to(outside, target_is_directory=True)

            report = setup_permissions(root)

            self.assertEqual(report["status"], "error")
            self.assertTrue(any("refusing symlink" in error for error in report["errors"]))
            self.assertEqual(S_IMODE(outside_file.stat().st_mode), 0o644)

    def test_setup_permissions_rejects_symlink_managed_root(self) -> None:
        with TemporaryDirectory() as temp_dir, TemporaryDirectory() as outside_dir:
            root = Path(temp_dir)
            outside_file = Path(outside_dir) / "outside.json"
            outside_file.write_text("do not touch", encoding="utf-8")
            outside_file.chmod(0o644)
            (root / "private").symlink_to(Path(outside_dir), target_is_directory=True)

            report = setup_permissions(root)

            self.assertEqual(report["status"], "error")
            self.assertIn("refusing symlink in managed runtime path: private", report["errors"])
            self.assertEqual(S_IMODE(outside_file.stat().st_mode), 0o644)

    def test_inspect_permissions_is_read_only_and_reports_unsafe_modes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            setup_permissions(root)
            sensitive = root / "models" / "ridge.joblib"
            sensitive.write_text("private", encoding="utf-8")
            sensitive.chmod(0o644)

            report = inspect_permissions(root)

            self.assertEqual(report["status"], "error")
            self.assertIn(
                "unsafe file mode for models/ridge.joblib: 0o644 (expected 0o600)",
                report["errors"],
            )
            self.assertEqual(S_IMODE(sensitive.stat().st_mode), 0o644)
            self.assertFalse(report["checks"]["file_contents_read"])
            self.assertFalse(report["checks"]["symlinks_followed"])


if __name__ == "__main__":
    unittest.main()

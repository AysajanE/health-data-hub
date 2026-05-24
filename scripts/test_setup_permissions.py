from __future__ import annotations

from pathlib import Path
from stat import S_IMODE
from tempfile import TemporaryDirectory
import unittest

from scripts.setup_permissions import setup_permissions


class SetupPermissionsTests(unittest.TestCase):
    def test_setup_permissions_skips_missing_local_data_roots(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)

            report = setup_permissions(root)

            self.assertEqual(report["status"], "ok")
            self.assertEqual(report["changed_paths"], [])
            self.assertFalse((root / "data").exists())

    def test_setup_permissions_secures_managed_directories_and_sensitive_files(self) -> None:
        with TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            warehouse_path = root / "data" / "warehouse.duckdb"
            secret_path = root / "data" / "secrets" / "oura_tokens.json"
            quarantine_path = root / "data" / "quarantine" / "2026-05-24-failure.json"
            raw_path = root / "data" / "raw" / "oura" / "2026-05-24.json"
            snapshot_path = root / "data" / "snapshots" / "warehouse-2026-05-24.duckdb"
            lock_path = root / "data" / ".healthhub.lock"

            for path in (warehouse_path, secret_path, quarantine_path, raw_path, snapshot_path, lock_path):
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
            self.assertEqual(S_IMODE(warehouse_path.stat().st_mode), 0o600)
            self.assertEqual(S_IMODE(secret_path.stat().st_mode), 0o600)
            self.assertEqual(S_IMODE(quarantine_path.stat().st_mode), 0o600)
            self.assertEqual(S_IMODE(raw_path.stat().st_mode), 0o600)
            self.assertEqual(S_IMODE(snapshot_path.stat().st_mode), 0o600)
            self.assertEqual(S_IMODE(lock_path.stat().st_mode), 0o600)

            second_report = setup_permissions(root)
            self.assertEqual(second_report["status"], "ok")
            self.assertEqual(second_report["changed_paths"], [])


if __name__ == "__main__":
    unittest.main()

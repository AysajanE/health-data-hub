from __future__ import annotations

from datetime import UTC, date, datetime
import fcntl
import json
import os
from pathlib import Path
from stat import S_IMODE
from tempfile import TemporaryDirectory
import unittest
from uuid import uuid4

from unittest.mock import patch

from src.warehouse import warehouse as warehouse_module
from src.warehouse.locking import (
    LOCK_FILE_NAME,
    WarehouseLockTimeout,
    lock_path_for_database,
    warehouse_write_lock,
)
from src.warehouse.warehouse import connect_duckdb, persist_mood_entry_locked


class WarehouseWriteLockTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.data_dir = Path(self.tempdir.name) / "data"
        self.lock_path = self.data_dir / LOCK_FILE_NAME

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def test_first_acquisition_creates_private_lock_file_and_parent(self) -> None:
        self.assertFalse(self.data_dir.exists())

        with warehouse_write_lock(self.lock_path):
            self.assertTrue(self.lock_path.exists())
            self.assertEqual(S_IMODE(self.lock_path.stat().st_mode), 0o600)
            self.assertEqual(S_IMODE(self.data_dir.stat().st_mode), 0o700)

        self.assertTrue(self.lock_path.exists(), "lock file must never be deleted")

    def test_second_holder_is_blocked_and_times_out(self) -> None:
        with warehouse_write_lock(self.lock_path):
            fd = os.open(self.lock_path, os.O_RDWR)
            try:
                with self.assertRaises(OSError):
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            finally:
                os.close(fd)

            with self.assertRaises(WarehouseLockTimeout):
                with warehouse_write_lock(self.lock_path, timeout_seconds=0.2):
                    self.fail("second holder must not enter the locked block")

    def test_lock_is_released_after_an_exception(self) -> None:
        with self.assertRaises(RuntimeError):
            with warehouse_write_lock(self.lock_path):
                raise RuntimeError("boom")

        with warehouse_write_lock(self.lock_path, timeout_seconds=0.2):
            pass

    def test_lock_path_is_next_to_the_database(self) -> None:
        database = self.data_dir / "warehouse.duckdb"
        self.assertEqual(
            lock_path_for_database(database),
            self.data_dir.resolve() / LOCK_FILE_NAME,
        )


class PersistMoodEntryLockedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = TemporaryDirectory()
        self.database_path = Path(self.tempdir.name) / "data" / "warehouse.duckdb"

    def tearDown(self) -> None:
        self.tempdir.cleanup()

    def _payload(self, feeling: int) -> dict[str, object]:
        return {
            "log_id": uuid4(),
            "logged_at_utc": datetime(2026, 9, 10, 23, 30, tzinfo=UTC),
            "mood_date": date(2026, 9, 10),
            "feeling": feeling,
            "energy": None,
            "notes": None,
            "context_chips": (),
            "source": "manual",
            "supersedes_log_id": None,
        }

    def test_write_commits_checkpoints_and_keeps_correction_flow(self) -> None:
        first = persist_mood_entry_locked(self.database_path, self._payload(7))
        second = persist_mood_entry_locked(self.database_path, self._payload(5))

        self.assertTrue(self.database_path.exists())
        self.assertEqual(S_IMODE(self.database_path.stat().st_mode), 0o600)
        self.assertFalse(Path(f"{self.database_path}.wal").exists())
        self.assertTrue(lock_path_for_database(self.database_path).exists())
        self.assertEqual(second.supersedes_log_id, first.log_id)

        conn = connect_duckdb(self.database_path, read_only=True)
        try:
            entries = conn.execute("SELECT COUNT(*) FROM mood_entries").fetchone()
            current = conn.execute("SELECT log_id FROM mood_current").fetchall()
        finally:
            conn.close()
        self.assertEqual(entries, (2,))
        self.assertEqual(current, [(second.log_id,)])

    def test_checkpoint_failure_after_commit_is_logged_redacted_and_not_raised(self) -> None:
        general_log_path = Path(self.tempdir.name) / "logs" / "healthhub.log"

        def failing_checkpoint(conn: object) -> None:
            raise RuntimeError("simulated checkpoint failure")

        with patch.object(warehouse_module, "_checkpoint", failing_checkpoint):
            row = persist_mood_entry_locked(
                self.database_path,
                self._payload(7),
                general_log_path=general_log_path,
            )

        self.assertEqual(row.feeling, 7)
        conn = connect_duckdb(self.database_path, read_only=True)
        try:
            stored = conn.execute("SELECT feeling FROM mood_entries").fetchall()
        finally:
            conn.close()
        self.assertEqual(stored, [(7,)])

        log_lines = general_log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(log_lines), 1)
        record = json.loads(log_lines[0])
        self.assertEqual(set(record), {"detected_at_utc", "error_type", "event"})
        self.assertEqual(record["event"], "checkpoint_failed_after_commit")
        self.assertEqual(record["error_type"], "RuntimeError")
        self.assertNotIn("simulated", log_lines[0])
        self.assertNotIn(str(self.database_path), log_lines[0])

    def test_symlink_database_path_is_refused_before_any_write(self) -> None:
        real_target = Path(self.tempdir.name) / "elsewhere" / "target.duckdb"
        real_target.parent.mkdir()
        self.database_path.parent.mkdir(mode=0o700)
        self.database_path.symlink_to(real_target)

        with self.assertRaises(OSError):
            persist_mood_entry_locked(self.database_path, self._payload(7))

        self.assertFalse(real_target.exists(), "no data may be written through a symlink")

    def test_write_times_out_visibly_while_lock_is_held(self) -> None:
        with warehouse_write_lock(lock_path_for_database(self.database_path)):
            with self.assertRaises(WarehouseLockTimeout):
                persist_mood_entry_locked(
                    self.database_path,
                    self._payload(7),
                    lock_timeout_seconds=0.2,
                )
            self.assertFalse(self.database_path.exists(), "no unguarded write may happen")


if __name__ == "__main__":
    unittest.main()

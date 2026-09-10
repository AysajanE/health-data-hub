from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
import fcntl
import io
import json
import os
from pathlib import Path
import shutil
from stat import S_IMODE
import subprocess
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from uuid import uuid4

from src.backup import snapshot as module
from src.backup.snapshot import (
    OpensslCipher, SnapshotError, SnapshotManifest, build_archive,
    checkpoint_and_copy_warehouse, collect_data_plane, create_snapshot,
    init_passphrase, list_snapshots, restore_snapshot, sha256_file,
    verify_snapshot, warehouse_summary,
)
from src.warehouse.locking import WarehouseLockTimeout, lock_path_for_database, warehouse_write_lock
from src.warehouse.warehouse import (
    connect_duckdb, insert_daily_features_row, insert_mood_entry, insert_sleep_night,
    secure_database_files,
)


NOW = datetime(2026, 9, 10, 12, 30, tzinfo=UTC)
TOKEN_FIXTURE = "fixture-token"
HEALTH_FIXTURE = "fixture-health-note"


class FakeCipher:
    """Reversible test transform; deliberately not cryptographic encryption."""

    def __init__(self):
        self.calls = []

    @staticmethod
    def encode(payload):
        return b"FAKE-CIPHER\n" + bytes(byte ^ 0xA5 for byte in payload)

    @staticmethod
    def decode(payload):
        if not payload.startswith(b"FAKE-CIPHER\n"):
            raise SnapshotError("decryption failed (wrong passphrase or corrupt snapshot)")
        return bytes(byte ^ 0xA5 for byte in payload[len(b"FAKE-CIPHER\n"):])

    def encrypt(self, source, destination, *, passphrase_file):
        self.calls.append(("encrypt", source, destination, passphrase_file))
        destination.write_bytes(self.encode(source.read_bytes()))

    def decrypt(self, source, destination, *, passphrase_file):
        self.calls.append(("decrypt", source, destination, passphrase_file))
        destination.write_bytes(self.decode(source.read_bytes()))


def populate_warehouse(database: Path) -> None:
    with warehouse_write_lock(lock_path_for_database(database)):
        conn = connect_duckdb(database, apply_schema=True)
        try:
            paths = {"quarantine_dir": database.parent / "quarantine", "general_log_path": database.parent / "fixture.log"}
            insert_sleep_night(conn, {
                "source": "oura", "sleep_date": date(2026, 9, 10),
                "total_sleep_min": 423, "ingested_at_utc": NOW,
            }, **paths)
            for feeling in (7, 6):
                insert_mood_entry(conn, {
                    "log_id": uuid4(), "logged_at_utc": NOW, "mood_date": date(2026, 9, 10),
                    "feeling": feeling, "notes": HEALTH_FIXTURE, "source": "manual",
                }, **paths)
            insert_daily_features_row(conn, {
                "feature_date": date(2026, 9, 10), "total_sleep_min": 423, "computed_at_utc": NOW,
            })
        finally:
            conn.close()
        secure_database_files(database)


class SnapshotFixture(unittest.TestCase):
    def setUp(self):
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()
        self.root = self.base / "repo"
        self.root.mkdir(mode=0o700)
        self.database = self.root / "data/warehouse.duckdb"
        self.key = init_passphrase(self.root / "data/secrets/backup_passphrase")
        self.destination = self.base / "cloud/snapshots"
        self.cipher = FakeCipher()

    def seed(self):
        populate_warehouse(self.database)
        for name, value in {
            "data/secrets/oura_tokens.json": json.dumps({"access_token": TOKEN_FIXTURE}),
            "data/oura_sync_status.json": json.dumps({"status": "ok"}),
            "models/eval.jsonl": HEALTH_FIXTURE + "\n",
            "models/z.pkl": "fixture-model-z",
            "models/a.pkl": "fixture-model-a",
            ".env.local": "FIXTURE=" + TOKEN_FIXTURE,
            "data/quarantine/ignored.json": HEALTH_FIXTURE,
            "data/restore/ignored": HEALTH_FIXTURE,
            "private/ignored": TOKEN_FIXTURE,
        }.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(value, encoding="utf-8")

    def backup(self, **kwargs):
        options = dict(root=self.root, destination=self.destination, passphrase_file=self.key,
                       cipher=self.cipher, database=self.database, now=NOW, app_commit="abcdef1")
        options.update(kwargs)
        report = create_snapshot(**options)
        return self.destination / report["snapshot"], report

    def verify(self, snapshot, name="verify"):
        return verify_snapshot(snapshot, passphrase_file=self.key, cipher=self.cipher, workdir=self.base / name)

    def restore(self, snapshot, **kwargs):
        options = dict(snapshot=snapshot, passphrase_file=self.key, cipher=self.cipher,
                       root=self.root, database=self.database, into=self.base / "restored", now=NOW)
        options.update(kwargs)
        return restore_snapshot(**options)

    def rewrite_archive(self, snapshot, transform):
        # Remove the transport digest to exercise the independent inner validation.
        snapshot.with_name(snapshot.name + ".manifest.json").unlink(missing_ok=True)
        with tarfile.open(fileobj=io.BytesIO(FakeCipher.decode(snapshot.read_bytes())), mode="r:gz") as archive:
            members = [(member, archive.extractfile(member).read()) for member in archive.getmembers()]
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w:gz") as archive:
            for member, content in transform(members):
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
        snapshot.write_bytes(FakeCipher.encode(output.getvalue()))


class PassphraseAndCollectionTest(SnapshotFixture):
    def test_private_init_refuses_overwrite_and_force_replaces(self):
        original = self.key.read_text()
        self.assertRegex(original, r"^[a-f0-9]{64}\n$")
        self.assertEqual(S_IMODE(self.key.stat().st_mode), 0o600)
        self.assertEqual(S_IMODE(self.key.parent.stat().st_mode), 0o700)
        with self.assertRaises(SnapshotError):
            init_passphrase(self.key)
        self.assertEqual(self.key.read_text(), original)
        self.assertEqual(init_passphrase(self.key, force=True), self.key)
        self.assertNotEqual(self.key.read_text(), original)
        self.assertEqual(S_IMODE(self.key.stat().st_mode), 0o600)
        self.assertEqual([path.name for path in self.key.parent.iterdir()], [self.key.name])

    def test_force_init_preserves_the_existing_key_when_replacement_fails(self):
        # The old key protects every existing snapshot; a failed re-init must
        # leave it exactly in place and clean up its temporary file.
        original = self.key.read_bytes()
        with patch.object(module.os, "fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(SnapshotError, "^could not initialize backup passphrase$"):
                init_passphrase(self.key, force=True)
        self.assertEqual(self.key.read_bytes(), original)
        self.assertEqual(S_IMODE(self.key.stat().st_mode), 0o600)
        self.assertEqual([path.name for path in self.key.parent.iterdir()], [self.key.name])

    def test_non_forced_init_never_clobbers_a_key_created_concurrently(self):
        key = self.root / "data/secrets/race_key"
        planted = None

        def plant_key_during_generation(*args, **kwargs):
            # Simulate another initializer publishing between the existence
            # check and our publication: the second token_hex call is the
            # passphrase text, by which point the check has already passed.
            nonlocal planted
            value = original_token_hex(*args, **kwargs)
            if planted is None and args == (32,):
                key.write_text("competitor-key\n", encoding="utf-8")
                key.chmod(0o600)
                planted = "competitor-key\n"
            return value

        original_token_hex = module.secrets.token_hex
        with patch.object(module.secrets, "token_hex", side_effect=plant_key_during_generation):
            with self.assertRaisesRegex(SnapshotError, "^backup passphrase already exists$"):
                init_passphrase(key)
        self.assertEqual(key.read_text(encoding="utf-8"), planted)
        self.assertEqual([path.name for path in key.parent.iterdir() if path.name != self.key.name], [key.name])

    def test_forced_init_restores_the_previous_key_when_a_post_rename_step_fails(self):
        original = self.key.read_bytes()
        with patch.object(module, "_fsync", side_effect=OSError("directory fsync failed")):
            with self.assertRaisesRegex(SnapshotError, "^could not initialize backup passphrase$"):
                init_passphrase(self.key, force=True)
        self.assertEqual(self.key.read_bytes(), original)
        self.assertEqual(S_IMODE(self.key.stat().st_mode), 0o600)
        self.assertEqual([path.name for path in self.key.parent.iterdir()], [self.key.name])

    def test_forced_init_rolls_back_on_interrupt_after_the_rename(self):
        original = self.key.read_bytes()
        with patch.object(module, "_fsync", side_effect=KeyboardInterrupt()):
            with self.assertRaises(KeyboardInterrupt):
                init_passphrase(self.key, force=True)
        self.assertEqual(self.key.read_bytes(), original)
        self.assertEqual(S_IMODE(self.key.stat().st_mode), 0o600)
        self.assertEqual([path.name for path in self.key.parent.iterdir()], [self.key.name])

    def test_forced_init_preserves_the_previous_copy_when_rollback_itself_fails(self):
        original = self.key.read_bytes()
        replace = os.replace
        calls = []

        def replace_then_fail_rollback(source, target):
            calls.append(Path(source).name)
            if len(calls) == 2:  # the rollback rename of .previous -> key
                raise OSError("rollback rename failed")
            return replace(source, target)

        with patch.object(module, "_fsync", side_effect=OSError("directory fsync failed")), patch.object(
            module.os, "replace", side_effect=replace_then_fail_rollback
        ):
            with self.assertRaisesRegex(SnapshotError, r"^backup passphrase rotation failed; previous key preserved as \.backup_passphrase\.[0-9a-f]{8}\.previous$"):
                init_passphrase(self.key, force=True)
        preserved = [path for path in self.key.parent.iterdir() if path.name.endswith(".previous")]
        self.assertEqual(len(preserved), 1)
        self.assertEqual(preserved[0].read_bytes(), original)
        self.assertNotEqual(self.key.read_bytes(), original)
        self.assertEqual([p.name for p in self.key.parent.iterdir() if p.name.endswith(".tmp")], [])

    def test_forced_init_reports_a_restored_key_whose_mode_reset_failed_distinctly(self):
        original = self.key.read_bytes()
        chmod = module.Path.chmod
        calls = []

        def chmod_then_fail_after_rollback(self_path, mode, *args, **kwargs):
            # Count only chmods of the key file itself (the parent directory is
            # chmod'ed too): 1st is the new key on the success path, 2nd is the
            # restored key after the rollback rename.
            if Path(self_path) == self.key:
                calls.append(mode)
                if len(calls) == 2:
                    raise OSError("chmod failed")
            return chmod(self_path, mode, *args, **kwargs)

        with patch.object(module, "_fsync", side_effect=OSError("directory fsync failed")), patch.object(
            module.Path, "chmod", autospec=True, side_effect=chmod_then_fail_after_rollback
        ):
            with self.assertRaisesRegex(SnapshotError, "^backup passphrase restored but its mode could not be reset to 0600$"):
                init_passphrase(self.key, force=True)
        self.assertEqual(self.key.read_bytes(), original)
        self.assertEqual([p.name for p in self.key.parent.iterdir() if p.name.endswith((".previous", ".tmp"))], [])

    def test_concurrent_backups_are_serialized_by_the_destination_lock(self):
        self.seed()
        self.destination.mkdir(parents=True, mode=0o700)
        lock_path = self.destination / ".backup.lock"
        holder = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(holder, fcntl.LOCK_EX | fcntl.LOCK_NB)
            with self.assertRaisesRegex(SnapshotError, "^another backup is already running$"):
                self.backup()
            self.assertEqual(list_snapshots(self.destination), [])
        finally:
            fcntl.flock(holder, fcntl.LOCK_UN)
            os.close(holder)
        snapshot, report = self.backup()
        self.assertEqual(report["status"], "ok")
        self.assertEqual(list_snapshots(self.destination), [snapshot])
        self.assertTrue(lock_path.exists(), "the lock file is a permanent, non-snapshot resident of the destination")
        _, report = self.backup(now=NOW + timedelta(days=1), keep=1)
        self.assertEqual(report["pruned"], 1)
        self.assertTrue(lock_path.exists())

    def test_init_refuses_leaf_and_parent_symlinks_even_with_force(self):
        link = self.base / "key-link"
        link.symlink_to(self.key)
        original = self.key.read_bytes()
        for force in (False, True):
            with self.assertRaises(SnapshotError):
                init_passphrase(link, force=force)
        parent = self.base / "linked-parent"
        parent.symlink_to(self.key.parent, target_is_directory=True)
        with self.assertRaises(SnapshotError):
            init_passphrase(parent / "new-key")
        self.assertEqual(self.key.read_bytes(), original)
        self.assertFalse((self.key.parent / "new-key").exists())

    def test_collection_fixed_order_and_exclusions(self):
        self.seed()
        (self.root / "models/linked.pkl").symlink_to(self.root / "models/a.pkl")
        files, excluded = collect_data_plane(self.root)
        self.assertEqual([path.relative_to(self.root).as_posix() for path in files], [
            "data/warehouse.duckdb", "data/secrets/oura_tokens.json", "data/oura_sync_status.json",
            "models/eval.jsonl", "models/a.pkl", "models/z.pkl",
        ])
        self.assertEqual(set(excluded), {
            "data/quarantine/**", "data/.healthhub.lock", "data/restore/**",
            "data/secrets/backup_passphrase", ".env.local", "private/**", "symlinks",
        })
        included, exclusions = collect_data_plane(self.root, include_env=True)
        self.assertEqual(included, [*files, self.root / ".env.local"])
        self.assertNotIn(".env.local", exclusions)

    def test_symlink_directories_are_not_traversed(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "a.pkl").write_text(TOKEN_FIXTURE)
        (self.root / "models").symlink_to(outside, target_is_directory=True)
        (self.root / ".env.local").symlink_to(outside / "a.pkl")
        files, _ = collect_data_plane(self.root, include_env=True)
        self.assertEqual(files, [])


class WarehouseCopyTest(SnapshotFixture):
    def test_checkpoint_copy_is_self_contained_and_lock_covers_closed_connection_and_copy(self):
        populate_warehouse(self.database)
        destination = self.base / "copy/warehouse.duckdb"
        copy2 = shutil.copy2
        checkpoint = module._checkpoint
        connection = None

        def record_checkpoint(conn):
            nonlocal connection
            connection = conn
            checkpoint(conn)

        def guarded_copy(source, target):
            with self.assertRaises(WarehouseLockTimeout):
                with warehouse_write_lock(lock_path_for_database(self.database), timeout_seconds=0):
                    self.fail("copy must remain inside the lock")
            with self.assertRaises(Exception):
                connection.execute("SELECT 1")
            return copy2(source, target)

        with patch.object(module, "_checkpoint", side_effect=record_checkpoint), patch.object(module.shutil, "copy2", side_effect=guarded_copy):
            self.assertEqual(checkpoint_and_copy_warehouse(self.database, destination), destination)
        self.assertFalse(Path(f"{destination}.wal").exists())
        self.assertEqual(S_IMODE(destination.stat().st_mode), 0o600)
        self.assertEqual(warehouse_summary(destination), {
            "sleep_nights": 1, "mood_entries": 2, "mood_current": 1, "daily_features": 1,
            "latest_sleep_date": "2026-09-10", "latest_mood_date": "2026-09-10",
        })

    def test_busy_warehouse_and_checkpoint_error_are_redacted(self):
        populate_warehouse(self.database)
        with warehouse_write_lock(lock_path_for_database(self.database)):
            with self.assertRaisesRegex(SnapshotError, "^warehouse busy$"):
                checkpoint_and_copy_warehouse(self.database, self.base / "copy", lock_timeout_seconds=0.01)
        with patch.object(module, "_checkpoint", side_effect=RuntimeError(HEALTH_FIXTURE)):
            with self.assertRaisesRegex(SnapshotError, "^warehouse snapshot failed$"):
                checkpoint_and_copy_warehouse(self.database, self.base / "copy")
        with warehouse_write_lock(lock_path_for_database(self.database), timeout_seconds=0):
            pass

    def test_summary_empty_tables_and_error_redaction(self):
        with warehouse_write_lock(lock_path_for_database(self.database)):
            conn = connect_duckdb(self.database, apply_schema=True)
            conn.close()
        summary = warehouse_summary(self.database)
        self.assertEqual(summary["mood_entries"], 0)
        self.assertIsNone(summary["latest_sleep_date"])
        self.assertIsNone(summary["latest_mood_date"])
        with patch.object(module, "connect_duckdb", side_effect=RuntimeError(HEALTH_FIXTURE)):
            with self.assertRaisesRegex(SnapshotError, "^warehouse summary failed$"):
                warehouse_summary(self.database)


class ArchiveTest(SnapshotFixture):
    def test_archive_member_order_modes_and_manifest_digests(self):
        self.seed()
        files, excluded = collect_data_plane(self.root)
        pairs = [(path, path.relative_to(self.root).as_posix()) for path in files]
        extra = {"created_at_utc": NOW.isoformat(), "app_commit": "abcdef1", "excluded": excluded}
        first = build_archive(pairs, self.base / "first.tar.gz", manifest_extra=extra)
        second = build_archive(list(reversed(pairs)), self.base / "second.tar.gz", manifest_extra=extra)
        self.assertEqual(first, second)
        self.assertEqual(SnapshotManifest.from_dict(first.to_dict()), first)
        names = []
        for path in (self.base / "first.tar.gz", self.base / "second.tar.gz"):
            with tarfile.open(path) as archive:
                names.append(archive.getnames())
                for member in archive.getmembers():
                    self.assertEqual((member.mode, member.uid, member.gid), (0o600, 0, 0))
                for source, relative in pairs:
                    self.assertEqual(archive.getmember(relative).mtime, source.stat().st_mtime)
        self.assertEqual(names[0], ["manifest.json", *sorted(relative for _, relative in pairs)])
        self.assertEqual(names[0], names[1])
        for entry in first.entries:
            self.assertEqual(entry.sha256, sha256_file(self.root / entry.relative_path))
            self.assertEqual(entry.size, (self.root / entry.relative_path).stat().st_size)
        serialized = json.dumps(first.to_dict())
        for forbidden in (TOKEN_FIXTURE, HEALTH_FIXTURE, self.key.read_text().strip()):
            self.assertNotIn(forbidden, serialized)

    def test_snapshot_modes_sidecar_report_and_staging_cleanup(self):
        self.seed()
        snapshot, report = self.backup()
        sidecar = snapshot.with_name(snapshot.name + ".manifest.json")
        metadata = json.loads(sidecar.read_text())
        for path in (snapshot, sidecar):
            self.assertEqual(S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(S_IMODE(self.destination.stat().st_mode), 0o700)
        self.assertEqual(S_IMODE(self.destination.parent.stat().st_mode), 0o700)
        self.assertEqual(metadata["encrypted_sha256"], sha256_file(snapshot))
        self.assertEqual(metadata["encrypted_size"], snapshot.stat().st_size)
        self.assertEqual(set(report), {"status", "snapshot", "destination", "encrypted_size", "encrypted_sha256", "files",
                                       "file_count", "total_bytes", "excluded", "kept", "pruned", "app_commit"})
        self.assertEqual(report["file_count"], 6)
        self.assertEqual(report["total_bytes"], sum(entry["size"] for entry in metadata["entries"]))
        for value in (TOKEN_FIXTURE, HEALTH_FIXTURE, self.key.read_text().strip()):
            self.assertNotIn(value, json.dumps(metadata) + json.dumps(report))
        self.assertFalse(self.cipher.calls[0][1].parent.exists())
        self.assertEqual(self.cipher.calls[0][3], self.key)

    def test_failure_cleans_staging_and_temporary_output(self):
        self.seed()
        with patch.object(self.cipher, "encrypt", side_effect=RuntimeError(TOKEN_FIXTURE)) as encrypt:
            with self.assertRaisesRegex(SnapshotError, "^snapshot creation failed$"):
                self.backup()
        self.assertFalse(encrypt.call_args.args[0].parent.exists())
        self.assertEqual([p for p in self.destination.iterdir() if p.name != ".backup.lock"], [])

    def test_invalid_key_destination_and_keep_fail_before_encryption(self):
        for key in (self.base / "missing", self.base / "directory", self.base / "link"):
            if key.name == "directory":
                key.mkdir()
            elif key.name == "link":
                key.symlink_to(self.key)
            with self.assertRaisesRegex(SnapshotError, "backup passphrase missing"):
                self.backup(passphrase_file=key)
        self.key.chmod(0o644)
        with self.assertRaisesRegex(SnapshotError, "backup passphrase missing"):
            self.backup()
        self.key.chmod(0o600)
        with self.assertRaises(SnapshotError):
            self.backup(keep=0)
        link = self.base / "destination-link"
        link.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(SnapshotError):
            self.backup(destination=link)
        self.assertEqual(self.cipher.calls, [])

    def test_retention_newest_names_only_and_timestamp_collision(self):
        self.destination.mkdir(parents=True)
        foreign = self.destination / "other-backup.enc"
        foreign.write_text("keep me")
        near_match = self.destination / "hh-snapshot-owner-notes.txt"
        near_match.write_text("keep me too")
        symlink = self.destination / "hh-snapshot-19990101T000000Z.tar.gz.enc"
        symlink.symlink_to(foreign)
        snapshots = []
        for day in range(4):
            snapshot, report = self.backup(now=NOW + timedelta(days=day), keep=2)
            snapshots.append(snapshot)
        self.assertEqual(list_snapshots(self.destination), list(reversed(snapshots[-2:])))
        self.assertEqual((report["kept"], report["pruned"]), (2, 1))
        for old in snapshots[:2]:
            self.assertFalse(old.exists())
            self.assertFalse(old.with_name(old.name + ".manifest.json").exists())
        self.assertTrue(foreign.exists())
        self.assertTrue(near_match.exists())
        self.assertTrue(symlink.is_symlink())
        original = snapshots[-1].read_bytes()
        with self.assertRaisesRegex(SnapshotError, "already exists"):
            self.backup(now=NOW + timedelta(days=3))
        self.assertEqual(snapshots[-1].read_bytes(), original)

    def test_database_override_uses_only_selected_warehouse(self):
        selected = self.base / "selected/warehouse.duckdb"
        populate_warehouse(selected)
        self.database.write_bytes(b"not a database")
        snapshot, _ = self.backup(database=selected)
        _, extracted = self.verify(snapshot)
        self.assertEqual(warehouse_summary(extracted / "data/warehouse.duckdb")["mood_entries"], 2)


class VerificationTest(SnapshotFixture):
    def setUp(self):
        super().setUp()
        self.seed()
        self.snapshot, _ = self.backup()

    def test_valid_snapshot_and_private_extraction(self):
        manifest, extracted = self.verify(self.snapshot)
        self.assertEqual(len(manifest.entries), 6)
        for path in [extracted, *extracted.rglob("*")]:
            self.assertEqual(S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        self.assertFalse((extracted.parent / "snapshot.tar.gz").exists())

    def test_corrupt_encrypted_byte_and_sidecar_digest_mismatch(self):
        payload = bytearray(self.snapshot.read_bytes())
        payload[len(payload) // 2] ^= 0xFF
        self.snapshot.write_bytes(payload)
        with self.assertRaisesRegex(SnapshotError, "encrypted snapshot digest mismatch"):
            self.verify(self.snapshot)
        self.snapshot.with_name(self.snapshot.name + ".manifest.json").unlink()
        with self.assertRaises(SnapshotError):
            self.verify(self.snapshot, "no-sidecar")

    def test_altered_sidecar_digest_is_rejected_before_decrypt(self):
        sidecar = self.snapshot.with_name(self.snapshot.name + ".manifest.json")
        payload = json.loads(sidecar.read_text())
        payload["encrypted_sha256"] = "0" * 64
        sidecar.write_text(json.dumps(payload))
        with self.assertRaisesRegex(SnapshotError, "encrypted snapshot digest mismatch"):
            self.verify(self.snapshot)
        self.assertEqual(len(self.cipher.calls), 1)

    def test_tampered_member_size_or_digest(self):
        def tamper(members):
            return [(member, b"x" * len(content) if member.name == "models/a.pkl" else content) for member, content in members]
        self.rewrite_archive(self.snapshot, tamper)
        with self.assertRaisesRegex(SnapshotError, "^digest mismatch: models/a.pkl$"):
            self.verify(self.snapshot)
        self.assertFalse((self.base / "verify/extracted").exists())

    def test_missing_member(self):
        self.rewrite_archive(self.snapshot, lambda members: [(m, data) for m, data in members if m.name != "models/a.pkl"])
        with self.assertRaisesRegex(SnapshotError, "^digest mismatch: models/a.pkl$"):
            self.verify(self.snapshot)

    def test_traversal_absolute_unlisted_links_special_and_duplicate_members(self):
        original = self.snapshot.read_bytes()
        for index, (name, kind) in enumerate([
            ("../escape", tarfile.REGTYPE), (str(self.base / "escaped"), tarfile.REGTYPE),
            ("unlisted.txt", tarfile.REGTYPE), ("models/link.pkl", tarfile.SYMTYPE),
            ("models/link.pkl", tarfile.LNKTYPE), ("models/pipe.pkl", tarfile.FIFOTYPE),
            ("models/a.pkl", tarfile.REGTYPE),
        ]):
            with self.subTest(name=name, kind=kind):
                self.snapshot.write_bytes(original)
                member = tarfile.TarInfo(name)
                member.type, member.linkname = kind, "../../escape"
                self.rewrite_archive(self.snapshot, lambda members: [*members, (member, b"")])
                with self.assertRaises(SnapshotError):
                    self.verify(self.snapshot, f"verify-{index}")
        self.assertFalse((self.base / "escaped").exists())
        self.assertFalse((self.base / "escape").exists())

    def test_manifest_cannot_authorize_non_data_plane_files(self):
        def change_manifest(members):
            output = []
            for member, content in members:
                if member.name == "manifest.json":
                    payload = json.loads(content)
                    payload["entries"][0]["relative_path"] = "data/secrets/backup_passphrase"
                    content = json.dumps(payload).encode()
                output.append((member, content))
            return output
        self.rewrite_archive(self.snapshot, change_manifest)
        with self.assertRaisesRegex(SnapshotError, "invalid snapshot manifest"):
            self.verify(self.snapshot)


class RestoreTest(SnapshotFixture):
    def setUp(self):
        super().setUp()
        self.seed()

    def test_full_round_trip_into_fresh_dir_preserves_digests_modes_and_counts(self):
        snapshot, backup_report = self.backup(include_env=True)
        report = self.restore(snapshot)
        restored = Path(report["restored_to"])
        self.assertEqual(report["status"], "ok")
        self.assertEqual(report["file_count"], backup_report["file_count"])
        self.assertEqual(report["total_bytes"], backup_report["total_bytes"])
        self.assertEqual(report["warehouse"]["mood_entries"], 2)
        self.assertIsNone(report["moved_aside"])
        for relative in backup_report["files"]:
            self.assertEqual(sha256_file(self.root / relative), sha256_file(restored / relative))
        for path in [restored, *restored.rglob("*")]:
            self.assertEqual(S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        self.assertFalse((restored / "data/secrets/backup_passphrase").exists())
        self.assertFalse((restored / "data/.healthhub.lock").exists())
        self.assertFalse((restored / "manifest.json").exists())
        self.assertNotIn(HEALTH_FIXTURE, json.dumps(report))

    def test_empty_directory_allowed_nonempty_and_symlink_refused(self):
        snapshot, _ = self.backup()
        into = self.base / "restored"
        into.mkdir(mode=0o755)
        self.restore(snapshot, into=into)
        before = sha256_file(into / "data/warehouse.duckdb")
        with self.assertRaisesRegex(SnapshotError, "restore directory must be empty"):
            self.restore(snapshot, into=into)
        self.assertEqual(sha256_file(into / "data/warehouse.duckdb"), before)
        link = self.base / "restore-link"
        link.symlink_to(into, target_is_directory=True)
        with self.assertRaises(SnapshotError):
            self.restore(snapshot, into=link)

    def test_in_place_requires_explicit_force_and_exclusive_destination(self):
        snapshot, _ = self.backup()
        before = sha256_file(self.database)
        for options in ({"into": None}, {"force_in_place": True}, {"into": None, "force_in_place": 1}, {"into": self.root}):
            with self.assertRaises(SnapshotError):
                self.restore(snapshot, **options)
        self.assertEqual(sha256_file(self.database), before)

    def test_force_moves_live_files_and_wal_aside_under_lock(self):
        snapshot, backup_report = self.backup()
        token = self.root / "data/secrets/oura_tokens.json"
        token.write_text("changed-live-token")
        self.database.write_bytes(b"old-live-warehouse")
        wal = Path(f"{self.database}.wal")
        wal.write_bytes(b"old-live-wal")
        before = {relative: sha256_file(self.root / relative) for relative in backup_report["files"]}
        before["data/warehouse.duckdb.wal"] = sha256_file(wal)
        key_digest = sha256_file(self.key)
        copy2, replace = shutil.copy2, os.replace

        def check_lock():
            with self.assertRaises(WarehouseLockTimeout):
                with warehouse_write_lock(lock_path_for_database(self.database), timeout_seconds=0):
                    self.fail("live mutations must hold the warehouse lock")

        def guarded_copy(source, target):
            if Path(target).is_relative_to(self.root):
                check_lock()
            return copy2(source, target)

        def guarded_replace(source, target):
            if Path(source).is_relative_to(self.root):
                check_lock()
            return replace(source, target)

        with patch.object(module.shutil, "copy2", side_effect=guarded_copy), patch.object(module.os, "replace", side_effect=guarded_replace):
            report = self.restore(snapshot, into=None, force_in_place=True)
        aside = Path(report["moved_aside"])
        self.assertEqual(aside, self.root / "data/restore/pre-restore-20260910T123000Z")
        self.assertEqual(report["restored_to"], "in_place")
        for relative, digest in before.items():
            self.assertEqual(sha256_file(aside / relative), digest)
        for path in [aside, *aside.rglob("*")]:
            self.assertEqual(S_IMODE(path.stat().st_mode), 0o700 if path.is_dir() else 0o600)
        for relative in backup_report["files"]:
            self.assertEqual(S_IMODE((self.root / relative).stat().st_mode), 0o600)
        self.assertFalse(wal.exists())
        self.assertEqual(sha256_file(self.key), key_digest)
        self.assertEqual(token.read_text(), json.dumps({"access_token": TOKEN_FIXTURE}))
        with warehouse_write_lock(lock_path_for_database(self.database)):
            self.assertEqual(warehouse_summary(self.database)["mood_entries"], 2)

    def test_interrupt_during_the_live_swap_rolls_back_and_leaves_no_staging(self):
        snapshot, report = self.backup()
        self.database.write_bytes(b"old-live-warehouse")
        originals = {relative: sha256_file(self.root / relative) for relative in report["files"]}
        # Interrupt after every restored file has been placed; the rollback
        # must run for BaseException, not only Exception.
        with patch.object(module, "secure_database_files", side_effect=[KeyboardInterrupt(), None]):
            with self.assertRaises(KeyboardInterrupt):
                self.restore(snapshot, into=None, force_in_place=True)
        for relative, digest in originals.items():
            self.assertEqual(sha256_file(self.root / relative), digest)
        self.assertEqual(self.database.read_bytes(), b"old-live-warehouse")
        self.assertEqual([p.name for p in (self.root / "data/restore").iterdir() if p.name.startswith(".incoming-")], [])
        with warehouse_write_lock(lock_path_for_database(self.database), timeout_seconds=0.2):
            pass  # the lock was released

    def test_interrupt_between_a_rename_and_its_bookkeeping_still_rolls_back(self):
        snapshot, report = self.backup()
        self.database.write_bytes(b"old-live-warehouse")
        originals = {relative: sha256_file(self.root / relative) for relative in report["files"]}
        replace = os.replace
        interrupted = False

        def replace_then_interrupt(source, target):
            # Perform the very first live rename, then interrupt before the
            # caller can record it. Rollback must still find and undo it.
            nonlocal interrupted
            result = replace(source, target)
            if not interrupted and Path(source).is_relative_to(self.root) and not Path(source).is_relative_to(self.root / "data/restore"):
                interrupted = True
                raise KeyboardInterrupt()
            return result

        with patch.object(module.os, "replace", side_effect=replace_then_interrupt):
            with self.assertRaises(KeyboardInterrupt):
                self.restore(snapshot, into=None, force_in_place=True)
        self.assertTrue(interrupted)
        for relative, digest in originals.items():
            self.assertEqual(sha256_file(self.root / relative), digest)
        self.assertEqual(self.database.read_bytes(), b"old-live-warehouse")
        self.assertEqual([p.name for p in (self.root / "data/restore").iterdir() if p.name.startswith(".incoming-")], [])

    def test_failed_staging_copy_leaves_no_incoming_directory(self):
        snapshot, report = self.backup()
        originals = {relative: sha256_file(self.root / relative) for relative in report["files"]}
        copy2 = shutil.copy2

        def fail_staging_copy(source, target):
            if ".incoming-" in str(target) and Path(target).name == "eval.jsonl":
                raise OSError("disk full")
            return copy2(source, target)

        with patch.object(module.shutil, "copy2", side_effect=fail_staging_copy):
            with self.assertRaisesRegex(SnapshotError, "^snapshot restore failed$"):
                self.restore(snapshot, into=None, force_in_place=True)
        for relative, digest in originals.items():
            self.assertEqual(sha256_file(self.root / relative), digest)
        self.assertEqual([p.name for p in (self.root / "data/restore").iterdir() if p.name.startswith(".incoming-")], [])

    def test_failed_verification_does_not_touch_live_and_failed_copy_rolls_back(self):
        snapshot, report = self.backup()
        originals = {relative: sha256_file(self.root / relative) for relative in report["files"]}
        copy2 = shutil.copy2

        def fail_live_copy(source, target):
            # Restored files are staged under data/restore before the live
            # swap; failing that staging copy must leave the live tree intact.
            if Path(target).name == "a.pkl" and Path(target).is_relative_to(self.root):
                raise OSError(TOKEN_FIXTURE)
            return copy2(source, target)

        with patch.object(module.shutil, "copy2", side_effect=fail_live_copy):
            with self.assertRaisesRegex(SnapshotError, "^snapshot restore failed$"):
                self.restore(snapshot, into=None, force_in_place=True)
        for relative, digest in originals.items():
            self.assertEqual(sha256_file(self.root / relative), digest)
        snapshot.write_bytes(b"corrupt")
        with self.assertRaises(SnapshotError):
            self.restore(snapshot, into=None, force_in_place=True, now=NOW + timedelta(days=1))
        for relative, digest in originals.items():
            self.assertEqual(sha256_file(self.root / relative), digest)
        self.assertFalse((self.root / "data/restore/pre-restore-20260911T123000Z").exists())


class OpensslTest(SnapshotFixture):
    def test_openssl_command_uses_only_passphrase_file_and_redacts_errors(self):
        source, destination = self.base / "input", self.base / "output"
        source.write_text("small fixture")
        result = subprocess.CompletedProcess([], 1, stdout=b"", stderr=TOKEN_FIXTURE.encode())
        with patch.object(module.subprocess, "run", return_value=result) as run:
            with self.assertRaisesRegex(SnapshotError, "^encryption failed$"):
                OpensslCipher().encrypt(source, destination, passphrase_file=self.key)
        command = run.call_args.args[0]
        self.assertEqual(command, ["/usr/bin/openssl", "enc", "-aes-256-cbc", "-pbkdf2", "-iter", "200000", "-salt",
                                   "-in", str(source), "-out", str(destination), "-pass", f"file:{self.key}"])
        self.assertEqual(run.call_args.kwargs, {"check": False, "capture_output": True})
        self.assertNotIn(self.key.read_text().strip(), " ".join(command))
        self.assertFalse(destination.exists())

    @unittest.skipUnless(Path("/usr/bin/openssl").exists(), "system OpenSSL unavailable")
    def test_real_openssl_round_trip_and_wrong_passphrase_redaction(self):
        source, encrypted, restored = (self.base / name for name in ("source", "encrypted", "restored"))
        source.write_bytes(b"small hermetic round-trip fixture\n")
        cipher = OpensslCipher()
        cipher.encrypt(source, encrypted, passphrase_file=self.key)
        self.assertNotIn(source.read_bytes(), encrypted.read_bytes())
        cipher.decrypt(encrypted, restored, passphrase_file=self.key)
        self.assertEqual(restored.read_bytes(), source.read_bytes())
        self.assertEqual(S_IMODE(encrypted.stat().st_mode), 0o600)
        wrong = init_passphrase(self.base / "wrong-key")
        with self.assertRaises(SnapshotError) as caught:
            cipher.decrypt(encrypted, self.base / "wrong-output", passphrase_file=wrong)
        self.assertEqual(str(caught.exception), "decryption failed (wrong passphrase or corrupt snapshot)")
        self.assertNotIn(wrong.read_text().strip(), str(caught.exception))
        self.assertNotIn("bad decrypt", str(caught.exception))


if __name__ == "__main__":
    unittest.main()

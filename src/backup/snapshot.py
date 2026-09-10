"""Local encrypted snapshots; plaintext exists only in private staging directories."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import UTC, datetime
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
from typing import Mapping, Protocol, Sequence

from src.ingestion.oura_auth import write_private_json
from src.warehouse.locking import (
    WarehouseLockTimeout,
    lock_path_for_database,
    warehouse_write_lock,
)
from src.warehouse.warehouse import (
    DEFAULT_DATABASE_PATH,
    REPO_ROOT,
    _checkpoint,
    connect_duckdb,
    secure_database_files,
)

SNAPSHOT_SCHEMA = "hh_snapshot.v1"
SNAPSHOT_PREFIX = "hh-snapshot-"
DEFAULT_DESTINATION = Path.home() / "Library" / "Mobile Documents" / "com~apple~CloudDocs" / "HealthDataHub" / "snapshots"
DEFAULT_PASSPHRASE_PATH = REPO_ROOT / "data" / "secrets" / "backup_passphrase"
DEFAULT_KEEP = 30
OPENSSL_PATH = "/usr/bin/openssl"
PBKDF2_ITERATIONS = 200000

_DATA_FILES = (
    "data/warehouse.duckdb",
    "data/secrets/oura_tokens.json",
    "data/oura_sync_status.json",
    "models/eval.jsonl",
)
_EXCLUDED = (
    "data/quarantine/**",
    "data/.healthhub.lock",
    "data/restore/**",
    "data/secrets/backup_passphrase",
    "private/**",
    "symlinks",
)
_KEY_ERROR = "backup passphrase missing; run scripts/backup_snapshot.py --init-key"


class SnapshotError(RuntimeError):
    """An error safe to include in an aggregate report."""


def _safe_name(name: str) -> bool:
    return (
        isinstance(name, str)
        and bool(name)
        and not PurePosixPath(name).is_absolute()
        and all(part not in {"", ".", ".."} for part in name.split("/"))
        and "\\" not in name
        and not any(ord(char) < 32 or ord(char) == 127 for char in name)
    )


def _data_plane_name(name: str) -> bool:
    return _safe_name(name) and (
        name in (*_DATA_FILES, ".env.local")
        or (len(name.split("/")) == 2 and name.startswith("models/") and name.endswith(".pkl"))
    )


@dataclass(frozen=True)
class SnapshotEntry:
    relative_path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class SnapshotManifest:
    schema: str
    created_at_utc: str
    app_commit: str | None
    entries: tuple[SnapshotEntry, ...]
    excluded: tuple[str, ...]

    def to_dict(self) -> dict:
        return {
            "schema": self.schema,
            "created_at_utc": self.created_at_utc,
            "app_commit": self.app_commit,
            "entries": [asdict(entry) for entry in self.entries],
            "excluded": list(self.excluded),
        }

    @classmethod
    def from_dict(cls, payload: Mapping) -> SnapshotManifest:
        try:
            if set(payload) != {"schema", "created_at_utc", "app_commit", "entries", "excluded"}:
                raise ValueError
            if payload["schema"] != SNAPSHOT_SCHEMA:
                raise ValueError
            timestamp = datetime.fromisoformat(payload["created_at_utc"])
            if timestamp.utcoffset() != UTC.utcoffset(timestamp):
                raise ValueError
            commit = payload["app_commit"]
            if commit is not None and (not isinstance(commit, str) or not re.fullmatch(r"[0-9a-fA-F]{4,64}", commit)):
                raise ValueError
            if not isinstance(payload["entries"], (list, tuple)):
                raise ValueError
            entries = tuple(SnapshotEntry(**entry) for entry in payload["entries"])
            names = set()
            for entry in entries:
                if (
                    not _data_plane_name(entry.relative_path)
                    or entry.relative_path in names
                    or type(entry.size) is not int or entry.size < 0
                    or not isinstance(entry.sha256, str)
                    or not re.fullmatch(r"[0-9a-f]{64}", entry.sha256)
                ):
                    raise ValueError
                names.add(entry.relative_path)
            excluded = payload["excluded"]
            if not isinstance(excluded, (list, tuple)) or any(name not in (*_EXCLUDED, ".env.local") for name in excluded):
                raise ValueError
            return cls(SNAPSHOT_SCHEMA, payload["created_at_utc"], commit, entries, tuple(excluded))
        except (TypeError, ValueError, KeyError, AttributeError):
            raise SnapshotError("invalid snapshot manifest") from None


class Cipher(Protocol):
    def encrypt(self, source: Path, destination: Path, *, passphrase_file: Path) -> None: ...

    def decrypt(self, source: Path, destination: Path, *, passphrase_file: Path) -> None: ...


def _refuse_symlinks(path: Path) -> None:
    absolute = path.absolute()
    if ".." in absolute.parts:
        raise SnapshotError("unsafe filesystem path")
    for candidate in reversed((absolute, *absolute.parents)):
        if candidate.is_symlink():
            raise SnapshotError("symlinks are not allowed")


def _private_directory(path: Path) -> None:
    _refuse_symlinks(path)
    if not path.exists():
        if not path.parent.exists():
            _private_directory(path.parent)
        path.mkdir(mode=0o700)
    if not path.is_dir():
        raise SnapshotError("invalid directory")
    path.chmod(0o700)


def _validate_passphrase(path: Path) -> None:
    try:
        _refuse_symlinks(path)
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid():
            raise ValueError
    except (OSError, ValueError, SnapshotError):
        raise SnapshotError(_KEY_ERROR) from None


class OpensslCipher:
    def __init__(self, openssl_path=OPENSSL_PATH, iterations=PBKDF2_ITERATIONS):
        self.openssl_path = openssl_path
        self.iterations = iterations

    def _run(self, source: Path, destination: Path, passphrase_file: Path, *, decrypt: bool) -> None:
        message = "decryption failed (wrong passphrase or corrupt snapshot)" if decrypt else "encryption failed"
        _validate_passphrase(passphrase_file)
        _refuse_symlinks(source)
        _refuse_symlinks(destination)
        if source.absolute() == destination.absolute():
            raise SnapshotError(message)
        try:
            fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as stream:
                os.fchmod(stream.fileno(), 0o600)
            command = [str(self.openssl_path), "enc", "-aes-256-cbc", "-pbkdf2", "-iter", str(self.iterations), "-salt"]
            if decrypt:
                command.append("-d")
            command.extend(["-in", str(source), "-out", str(destination), "-pass", f"file:{passphrase_file}"])
            result = subprocess.run(command, check=False, capture_output=True)
            if result.returncode:
                raise SnapshotError(message)
            destination.chmod(0o600)
        except Exception:
            destination.unlink(missing_ok=True)
            raise SnapshotError(message) from None

    def encrypt(self, source: Path, destination: Path, *, passphrase_file: Path) -> None:
        self._run(source, destination, passphrase_file, decrypt=False)

    def decrypt(self, source: Path, destination: Path, *, passphrase_file: Path) -> None:
        self._run(source, destination, passphrase_file, decrypt=True)


def init_passphrase(path=DEFAULT_PASSPHRASE_PATH, *, force=False) -> Path:
    path = Path(path)
    _refuse_symlinks(path)
    _private_directory(path.parent)
    if path.exists():
        if not force:
            raise SnapshotError("backup passphrase already exists")
        if not path.is_file():
            raise SnapshotError("invalid passphrase file")
    # Write the replacement privately and fsync it first. Without force the
    # key is published with a no-clobber hard link, so two initializers racing
    # past the existence check cannot overwrite each other. With force the
    # previous key, which protects every existing snapshot, is kept as a
    # recoverable copy until the replacement is durable on disk.
    token = secrets.token_hex(4)
    temporary = path.with_name(f".{path.name}.{token}.tmp")
    previous = path.with_name(f".{path.name}.{token}.previous")
    replaced = False
    try:
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(secrets.token_hex(32) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        if force and path.exists():
            os.link(path, previous)
            replaced = True  # intent recorded before the rename
            os.replace(temporary, path)
        else:
            try:
                os.link(temporary, path)
            except FileExistsError:
                raise SnapshotError("backup passphrase already exists") from None
            temporary.unlink()
        path.chmod(0o600)
        _fsync(path.parent, directory=True)
    except BaseException as error:
        # Any failure or interrupt after the rename rolls the previous key
        # back into place; the .previous copy is consumed only by a successful
        # rollback and is otherwise preserved as recovery material.
        temporary.unlink(missing_ok=True)
        if replaced and previous.exists():
            try:
                os.replace(previous, path)
            except OSError:
                # The rename did not happen, so .previous still holds the old key.
                raise SnapshotError(
                    f"backup passphrase rotation failed; previous key preserved as {previous.name}"
                ) from None
            try:
                path.chmod(0o600)
            except OSError:
                # The old key is back at the active path; only its mode is off.
                raise SnapshotError(
                    "backup passphrase restored but its mode could not be reset to 0600"
                ) from None
        if isinstance(error, SnapshotError):
            raise
        if isinstance(error, OSError):
            raise SnapshotError("could not initialize backup passphrase") from None
        raise
    # Durable publication confirmed: the previous key is no longer needed.
    previous.unlink(missing_ok=True)
    temporary.unlink(missing_ok=True)
    return path


class _DestinationLock:
    """Exclusive advisory lock on the destination so concurrent backups cannot
    reserve the same snapshot name or interleave publication and pruning."""

    def __init__(self, destination: Path) -> None:
        self.path = destination / ".backup.lock"
        self.fd: int | None = None

    def __enter__(self) -> "_DestinationLock":
        _refuse_symlinks(self.path)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(self.fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(self.fd)
            self.fd = None
            raise SnapshotError("another backup is already running") from None
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self.fd is not None:
            try:
                fcntl.flock(self.fd, fcntl.LOCK_UN)
            finally:
                os.close(self.fd)
                self.fd = None


def collect_data_plane(root: Path, *, include_env=False) -> tuple[list[Path], list[str]]:
    _refuse_symlinks(root)
    candidates = [root / name for name in _DATA_FILES]
    try:
        _refuse_symlinks(root / "models")
    except SnapshotError:
        pass
    else:
        candidates.extend(sorted((root / "models").glob("*.pkl")))
    if include_env:
        candidates.append(root / ".env.local")
    files = []
    for path in candidates:
        try:
            _refuse_symlinks(path)
        except SnapshotError:
            continue
        if path.is_file():
            if not _data_plane_name(path.relative_to(root).as_posix()):
                raise SnapshotError("unsafe data plane filename")
            files.append(path)
    return files, [*_EXCLUDED, *([] if include_env else [".env.local"])]


def _copy_private(source: Path, destination: Path) -> None:
    _refuse_symlinks(source)
    _refuse_symlinks(destination)
    if not source.is_file():
        raise SnapshotError("snapshot source is not a regular file")
    _private_directory(destination.parent)
    shutil.copy2(source, destination)
    destination.chmod(0o600)


def checkpoint_and_copy_warehouse(database: Path, destination: Path, *, lock_timeout_seconds=30.0) -> Path:
    _refuse_symlinks(database)
    _refuse_symlinks(Path(f"{database}.wal"))
    _refuse_symlinks(destination)
    if not database.is_file() or database.absolute() == destination.absolute():
        raise SnapshotError("invalid warehouse source or destination")
    try:
        with warehouse_write_lock(lock_path_for_database(database), timeout_seconds=lock_timeout_seconds):
            conn = connect_duckdb(database)
            try:
                _checkpoint(conn)
            finally:
                conn.close()
            _copy_private(database, destination)
    except WarehouseLockTimeout:
        raise SnapshotError("warehouse busy") from None
    except SnapshotError:
        raise
    except Exception:
        raise SnapshotError("warehouse snapshot failed") from None
    return destination


def sha256_file(path: Path) -> str:
    _refuse_symlinks(path)
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _utc(now: datetime | None) -> datetime:
    value = now if now is not None else datetime.now(UTC)
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def build_archive(files: Sequence[tuple[Path, str]], archive_path: Path, *, manifest_extra: Mapping) -> SnapshotManifest:
    ordered = sorted(files, key=lambda pair: pair[1])
    entries = []
    for path, name in ordered:
        _refuse_symlinks(path)
        if not path.is_file() or not _data_plane_name(name):
            raise SnapshotError("invalid archive source")
        entries.append(SnapshotEntry(name, path.stat().st_size, sha256_file(path)))
    manifest = SnapshotManifest.from_dict({
        "schema": SNAPSHOT_SCHEMA,
        "created_at_utc": manifest_extra.get("created_at_utc", _utc(None).isoformat()),
        "app_commit": manifest_extra.get("app_commit"),
        "entries": [asdict(entry) for entry in entries],
        "excluded": list(manifest_extra.get("excluded", ())),
    })
    _refuse_symlinks(archive_path)
    fd = os.open(archive_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as output, tarfile.open(fileobj=output, mode="w:gz") as archive:
        encoded = json.dumps(manifest.to_dict(), sort_keys=True).encode("utf-8")
        member = tarfile.TarInfo("manifest.json")
        member.size, member.mode = len(encoded), 0o600
        member.mtime = int(datetime.fromisoformat(manifest.created_at_utc).timestamp())
        archive.addfile(member, io.BytesIO(encoded))
        for (path, name), entry in zip(ordered, manifest.entries):
            member = tarfile.TarInfo(name)
            member.size, member.mode = entry.size, 0o600
            member.uid = member.gid = 0
            member.mtime = path.stat().st_mtime
            with path.open("rb") as source:
                archive.addfile(member, source)
    return manifest


def _sidecar(snapshot: Path) -> Path:
    return snapshot.with_name(snapshot.name + ".manifest.json")


def list_snapshots(destination: Path) -> list[Path]:
    _refuse_symlinks(destination)
    if not destination.exists():
        return []
    return sorted(
        (path for path in destination.iterdir()
         if re.fullmatch(r"hh-snapshot-\d{8}T\d{6}Z\.tar\.gz\.enc", path.name)
         and not path.is_symlink() and path.is_file()),
        reverse=True,
    )


def _fsync(path: Path, *, directory=False) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | (os.O_DIRECTORY if directory else 0))
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_snapshot(*, root: Path, destination: Path, passphrase_file: Path, cipher: Cipher,
                    keep: int = DEFAULT_KEEP, include_env=False, now: datetime | None = None,
                    app_commit: str | None = None, database: Path = DEFAULT_DATABASE_PATH) -> dict:
    _validate_passphrase(passphrase_file)
    if type(keep) is not int or keep < 1:
        raise SnapshotError("keep must be at least 1")
    _private_directory(destination)
    with _DestinationLock(destination):
        return _create_snapshot_locked(
            root=root, destination=destination, passphrase_file=passphrase_file, cipher=cipher,
            keep=keep, include_env=include_env, now=now, app_commit=app_commit, database=database,
        )


def _create_snapshot_locked(*, root: Path, destination: Path, passphrase_file: Path, cipher: Cipher,
                            keep: int, include_env: bool, now: datetime | None,
                            app_commit: str | None, database: Path) -> dict:
    moment = _utc(now)
    name = f"{SNAPSHOT_PREFIX}{moment:%Y%m%dT%H%M%SZ}.tar.gz.enc"
    target = destination / name
    _refuse_symlinks(target)
    _refuse_symlinks(_sidecar(target))
    if target.exists() or _sidecar(target).exists():
        raise SnapshotError("snapshot already exists for this timestamp")
    stage = Path(tempfile.mkdtemp(prefix="hh-snapshot-")).resolve()
    stage.chmod(0o700)
    temporary = destination / f".{name}.{secrets.token_hex(8)}.tmp"
    try:
        files, excluded = collect_data_plane(root, include_env=include_env)
        staged = []
        # --database changes the source, while the archive keeps the canonical layout.
        for path in files:
            relative = path.relative_to(root).as_posix()
            if relative == "data/warehouse.duckdb":
                continue
            copy = stage / relative
            _copy_private(path, copy)
            staged.append((copy, relative))
        _refuse_symlinks(database)
        if database.exists():
            copy = stage / "data/warehouse.duckdb"
            checkpoint_and_copy_warehouse(database, copy)
            staged.append((copy, "data/warehouse.duckdb"))
        archive = stage / "snapshot.tar.gz"
        manifest = build_archive(staged, archive, manifest_extra={
            "created_at_utc": moment.isoformat(), "app_commit": app_commit, "excluded": excluded,
        })
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        os.close(fd)
        cipher.encrypt(archive, temporary, passphrase_file=passphrase_file)
        temporary.chmod(0o600)
        _fsync(temporary)
        encrypted_size = temporary.stat().st_size
        encrypted_sha256 = sha256_file(temporary)
        os.replace(temporary, target)
        target.chmod(0o600)
        _fsync(target)
        write_private_json(_sidecar(target), {
            **manifest.to_dict(), "encrypted_sha256": encrypted_sha256, "encrypted_size": encrypted_size,
        })
        snapshots = list_snapshots(destination)
        pruned = 0
        for old in snapshots[keep:]:
            _refuse_symlinks(_sidecar(old))
            old.unlink()
            _sidecar(old).unlink(missing_ok=True)
            pruned += 1
        _fsync(destination, directory=True)
        return {
            "status": "ok", "snapshot": name, "destination": str(destination),
            "encrypted_size": encrypted_size, "encrypted_sha256": encrypted_sha256,
            "files": [entry.relative_path for entry in manifest.entries],
            "file_count": len(manifest.entries), "total_bytes": sum(entry.size for entry in manifest.entries),
            "excluded": list(manifest.excluded), "kept": len(snapshots) - pruned,
            "pruned": pruned, "app_commit": app_commit,
        }
    except SnapshotError:
        raise
    except Exception:
        raise SnapshotError("snapshot creation failed") from None
    finally:
        temporary.unlink(missing_ok=True)
        shutil.rmtree(stage)


def verify_snapshot(snapshot: Path, *, passphrase_file: Path, cipher: Cipher,
                    workdir: Path) -> tuple[SnapshotManifest, Path]:
    _validate_passphrase(passphrase_file)
    _refuse_symlinks(snapshot)
    _private_directory(workdir)
    archive_path = workdir / "snapshot.tar.gz"
    extracted = workdir / "extracted"
    _refuse_symlinks(archive_path)
    _refuse_symlinks(extracted)
    if archive_path.exists() or extracted.exists():
        raise SnapshotError("verification work directory is not fresh")
    try:
        sidecar = _sidecar(snapshot)
        _refuse_symlinks(sidecar)
        if sidecar.exists():
            payload = json.loads(sidecar.read_text(encoding="utf-8"))
            if payload.get("encrypted_sha256") != sha256_file(snapshot):
                raise SnapshotError("encrypted snapshot digest mismatch")
        cipher.decrypt(snapshot, archive_path, passphrase_file=passphrase_file)
        archive_path.chmod(0o600)
        with tarfile.open(archive_path, "r:gz") as archive:
            members = archive.getmembers()
            names = [member.name for member in members]
            if not names or names[0] != "manifest.json" or len(set(names)) != len(names):
                raise SnapshotError("invalid archive members")
            for member in members:
                if not _safe_name(member.name) or not member.isfile() or member.issym() or member.islnk():
                    raise SnapshotError("unsafe archive member")
            with archive.extractfile(members[0]) as stream:
                manifest = SnapshotManifest.from_dict(json.load(stream))
            expected = {entry.relative_path: entry for entry in manifest.entries}
            if set(names) - {"manifest.json"} - set(expected):
                raise SnapshotError("unlisted archive member")
            for name, entry in expected.items():
                member = next((member for member in members if member.name == name), None)
                if member is None or member.size != entry.size:
                    raise SnapshotError(f"digest mismatch: {name}")
            _private_directory(extracted)
            for member in members:
                _private_directory((extracted / member.name).parent)
                member.mode = 0o600
            archive.extractall(extracted, members=members, filter="data")
        for entry in manifest.entries:
            path = extracted / entry.relative_path
            path.chmod(0o600)
            if path.stat().st_size != entry.size or sha256_file(path) != entry.sha256:
                raise SnapshotError(f"digest mismatch: {entry.relative_path}")
        return manifest, extracted
    except Exception as error:
        if extracted.exists():
            shutil.rmtree(extracted)
        if isinstance(error, SnapshotError):
            raise
        raise SnapshotError("snapshot verification failed") from None
    finally:
        archive_path.unlink(missing_ok=True)


def warehouse_summary(database: Path) -> dict:
    summary = {name: 0 for name in ("sleep_nights", "mood_entries", "mood_current", "daily_features")}
    summary.update(latest_sleep_date=None, latest_mood_date=None)
    _refuse_symlinks(database)
    if not database.exists():
        return summary
    try:
        conn = connect_duckdb(database, read_only=True)
        try:
            for name in ("sleep_nights", "mood_entries", "mood_current", "daily_features"):
                summary[name] = int(conn.execute(f"SELECT COUNT(*) FROM {name}").fetchone()[0])
            for key, table, column in (
                ("latest_sleep_date", "sleep_nights", "sleep_date"),
                ("latest_mood_date", "mood_entries", "mood_date"),
            ):
                value = conn.execute(f"SELECT MAX({column}) FROM {table}").fetchone()[0]
                summary[key] = value.isoformat() if value is not None else None
        finally:
            conn.close()
    except Exception:
        raise SnapshotError("warehouse summary failed") from None
    return summary


def restore_snapshot(*, snapshot: Path, passphrase_file: Path, cipher: Cipher, into: Path | None,
                     root: Path, force_in_place: bool = False,
                     database: Path = DEFAULT_DATABASE_PATH, now=None) -> dict:
    if into is None and force_in_place is not True:
        raise SnapshotError("in-place restore requires explicit force")
    if into is not None and force_in_place:
        raise SnapshotError("into and in-place restore are mutually exclusive")
    _refuse_symlinks(root)
    if into is not None and into.absolute() == root.absolute():
        raise SnapshotError("in-place restore requires explicit force")
    if force_in_place and database.absolute() != (root / "data/warehouse.duckdb").absolute():
        raise SnapshotError("in-place warehouse must match the root data plane")
    workdir = Path(tempfile.mkdtemp(prefix="hh-restore-")).resolve()
    workdir.chmod(0o700)
    moved_aside = None
    try:
        manifest, extracted = verify_snapshot(snapshot, passphrase_file=passphrase_file, cipher=cipher, workdir=workdir)
        # All database inspection completes on the private copy before touching live files.
        summary = warehouse_summary(extracted / "data/warehouse.duckdb")
        if into is not None:
            _refuse_symlinks(into)
            if into.exists() and (not into.is_dir() or any(into.iterdir())):
                raise SnapshotError("restore directory must be empty")
            _private_directory(into)
            for entry in manifest.entries:
                _copy_private(extracted / entry.relative_path, into / entry.relative_path)
        else:
            moved_aside = root / "data/restore" / f"pre-restore-{_utc(now):%Y%m%dT%H%M%SZ}"
            _refuse_symlinks(database)
            _refuse_symlinks(moved_aside)
            with warehouse_write_lock(lock_path_for_database(database), timeout_seconds=30.0):
                if moved_aside.exists():
                    raise SnapshotError("pre-restore directory already exists")
                paths = [root / entry.relative_path for entry in manifest.entries]
                if "data/warehouse.duckdb" in {entry.relative_path for entry in manifest.entries}:
                    paths.append(Path(f"{database}.wal"))
                for path in paths:
                    _refuse_symlinks(path)
                    if path.exists() and not path.is_file():
                        raise SnapshotError("live restore target is not a regular file")
                _private_directory(moved_aside)
                # Stage every restored file next to the live tree first so the
                # live swap is only a sequence of renames, never a slow copy
                # that an interrupt could leave half-written at a live path.
                incoming = root / "data/restore" / f".incoming-{_utc(now):%Y%m%dT%H%M%SZ}-{secrets.token_hex(4)}"
                _refuse_symlinks(incoming)
                moved: list[tuple[Path, Path]] = []
                placed: list[Path] = []
                try:
                    _private_directory(incoming)
                    for entry in manifest.entries:
                        _copy_private(extracted / entry.relative_path, incoming / entry.relative_path)
                    # Intent is recorded BEFORE each rename so an interrupt
                    # between the rename and its bookkeeping still rolls back;
                    # rollback checks what actually exists on disk.
                    for path in paths:
                        if path.exists():
                            aside = moved_aside / path.relative_to(root)
                            _private_directory(aside.parent)
                            moved.append((aside, path))
                            os.replace(path, aside)
                            aside.chmod(0o600)
                    for entry in manifest.entries:
                        target = root / entry.relative_path
                        _private_directory(target.parent)
                        placed.append(target)
                        os.replace(incoming / entry.relative_path, target)
                        target.chmod(0o600)
                    secure_database_files(database)
                except BaseException:
                    # KeyboardInterrupt included: the live tree must never be
                    # left half-swapped once the lock is released.
                    for path in reversed(placed):
                        path.unlink(missing_ok=True)
                    for aside, original in reversed(moved):
                        if aside.exists() and not original.exists():
                            os.replace(aside, original)
                    secure_database_files(database)
                    raise
                finally:
                    shutil.rmtree(incoming, ignore_errors=True)
        return {
            "status": "ok", "snapshot": snapshot.name,
            "restored_to": str(into) if into is not None else "in_place",
            "file_count": len(manifest.entries), "total_bytes": sum(entry.size for entry in manifest.entries),
            "warehouse": summary, "moved_aside": str(moved_aside) if moved_aside else None,
        }
    except WarehouseLockTimeout:
        raise SnapshotError("warehouse busy") from None
    except SnapshotError:
        raise
    except Exception:
        raise SnapshotError("snapshot restore failed") from None
    finally:
        shutil.rmtree(workdir)

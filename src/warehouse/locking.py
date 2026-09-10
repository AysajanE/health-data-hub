"""Shared advisory write lock for the local DuckDB warehouse.

Every live warehouse write (mood form, mood API, future Oura sync) must open its
DuckDB connection inside :func:`warehouse_write_lock` and close it before the
lock is released. DuckDB allows one read-write process at a time, so this lock
serializes our own writers instead of letting the second one crash.
"""

from __future__ import annotations

from contextlib import contextmanager
import errno
import fcntl
import os
from pathlib import Path
import stat
import time
from typing import Iterator


REPO_ROOT = Path(__file__).resolve().parents[2]
LOCK_FILE_NAME = ".healthhub.lock"
DEFAULT_LOCK_PATH = REPO_ROOT / "data" / LOCK_FILE_NAME
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
DEFAULT_TIMEOUT_SECONDS = 10.0
DEFAULT_POLL_INTERVAL_SECONDS = 0.05
_BLOCKED_ERRNOS = frozenset({errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES})


class WarehouseLockTimeout(TimeoutError):
    """Raised when the shared warehouse write lock cannot be acquired in time."""


def lock_path_for_database(database: str | Path) -> Path:
    """Return the lock file that guards writes to the given DuckDB file."""

    return Path(database).expanduser().resolve().parent / LOCK_FILE_NAME


def _ensure_private_parent(lock_path: Path) -> None:
    parent = lock_path.parent
    if parent.is_symlink():
        raise OSError(f"refusing symlink lock directory: {parent}")
    if not parent.exists():
        parent.mkdir(mode=DIRECTORY_MODE, parents=True)


@contextmanager
def warehouse_write_lock(
    lock_path: str | Path = DEFAULT_LOCK_PATH,
    *,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    poll_interval_seconds: float = DEFAULT_POLL_INTERVAL_SECONDS,
) -> Iterator[int]:
    """Hold an exclusive ``flock`` on the lock file for the duration of the block.

    The lock file is created with mode ``0600`` inside a ``0700`` directory and
    is never deleted. A holder in any process, including this one through a
    separate file descriptor, blocks acquisition until it releases.
    """

    path = Path(lock_path)
    _ensure_private_parent(path)
    if path.is_symlink():
        raise OSError(f"refusing symlink lock file: {path}")

    fd = os.open(path, os.O_RDWR | os.O_CREAT, FILE_MODE)
    try:
        if stat.S_IMODE(os.fstat(fd).st_mode) != FILE_MODE:
            os.fchmod(fd, FILE_MODE)

        deadline = time.monotonic() + max(0.0, float(timeout_seconds))
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in _BLOCKED_ERRNOS:
                    raise
                if time.monotonic() >= deadline:
                    raise WarehouseLockTimeout(
                        f"timed out after {float(timeout_seconds):.1f}s waiting for the warehouse write lock"
                    ) from None
                time.sleep(poll_interval_seconds)

        try:
            yield fd
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


__all__ = [
    "DEFAULT_LOCK_PATH",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "DEFAULT_TIMEOUT_SECONDS",
    "LOCK_FILE_NAME",
    "WarehouseLockTimeout",
    "lock_path_for_database",
    "warehouse_write_lock",
]

#!/usr/bin/env python3
"""Verify an encrypted snapshot or restore it into a fresh private directory."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path
import sys
from tempfile import TemporaryDirectory

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backup.snapshot import (
    DEFAULT_DATABASE_PATH,
    DEFAULT_DESTINATION,
    DEFAULT_PASSPHRASE_PATH,
    OpensslCipher,
    REPO_ROOT,
    SnapshotError,
    list_snapshots,
    restore_snapshot,
    verify_snapshot,
    warehouse_summary,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", required=True, help="Snapshot path, or latest in --destination.")
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--passphrase-file", type=Path, default=DEFAULT_PASSPHRASE_PATH)
    target = parser.add_mutually_exclusive_group()
    target.add_argument("--into", type=Path, help="Fresh directory (default: data/restore/restore-<UTC timestamp>).")
    target.add_argument("--in-place", action="store_true")
    parser.add_argument("--force", action="store_true", help="Required with --in-place to replace live files.")
    parser.add_argument("--verify-only", action="store_true", help="Verify digests and warehouse counts; restore nothing.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if args.in_place and not args.force:
        parser.error("--in-place requires --force to replace the live data plane")
    if args.force and not args.in_place:
        parser.error("--force requires --in-place")
    if args.verify_only and args.in_place:
        parser.error("--verify-only and --in-place are mutually exclusive")
    try:
        if args.snapshot == "latest":
            available = list_snapshots(args.destination.expanduser())
            if not available:
                raise SnapshotError("no snapshots found")
            snapshot = available[0]
        else:
            snapshot = Path(args.snapshot).expanduser()
        passphrase_file = args.passphrase_file.expanduser()
        cipher = OpensslCipher()
        now = datetime.now(UTC)
        if args.verify_only:
            with TemporaryDirectory(prefix="hh-verify-") as temporary:
                manifest, extracted = verify_snapshot(
                    snapshot, passphrase_file=passphrase_file, cipher=cipher,
                    workdir=Path(temporary).resolve(),
                )
                report = {
                    "status": "ok", "snapshot": snapshot.name, "verify_only": True,
                    "restored_to": None, "moved_aside": None, "file_count": len(manifest.entries),
                    "total_bytes": sum(entry.size for entry in manifest.entries),
                    "warehouse": warehouse_summary(extracted / "data/warehouse.duckdb"),
                }
        else:
            into = None if args.in_place else (
                args.into.expanduser() if args.into is not None
                else REPO_ROOT / "data/restore" / f"restore-{now:%Y%m%dT%H%M%SZ}"
            )
            report = restore_snapshot(
                snapshot=snapshot, passphrase_file=passphrase_file, cipher=cipher,
                into=into, root=REPO_ROOT, force_in_place=args.in_place and args.force,
                database=DEFAULT_DATABASE_PATH, now=now,
            )
        if args.json:
            print(json.dumps(report, sort_keys=True))
        else:
            print(f"ok snapshot={report['snapshot']} files={report['file_count']} "
                  f"restored_to={report['restored_to'] or 'verified'}")
        return 0
    except Exception as error:
        print(json.dumps({
            "status": "error", "error_type": type(error).__name__,
            "message": str(error) if isinstance(error, SnapshotError) else "restore failed",
        }, sort_keys=True))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

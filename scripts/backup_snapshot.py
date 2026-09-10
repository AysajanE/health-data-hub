#!/usr/bin/env python3
"""Create an encrypted local data-plane snapshot and print aggregate metadata."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import subprocess
import sys

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.backup.snapshot import (
    DEFAULT_DATABASE_PATH,
    DEFAULT_DESTINATION,
    DEFAULT_KEEP,
    DEFAULT_MIRROR,
    DEFAULT_PASSPHRASE_PATH,
    OpensslCipher,
    REPO_ROOT,
    SnapshotError,
    create_snapshot,
    init_passphrase,
)

NOTIFY_COMMAND = "/usr/bin/osascript"
FAILURE_NOTIFICATION = 'display notification "Health Data Hub backup failed" with title "Health Data Hub"'
MIRROR_NOTIFICATION = (
    'display notification "Health Data Hub backup saved locally but the iCloud mirror failed" '
    'with title "Health Data Hub"'
)


def _notify(script: str) -> None:
    try:
        subprocess.run([NOTIFY_COMMAND, "-e", script], check=False, capture_output=True)
    except Exception:
        pass


def _app_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT,
            check=False, capture_output=True, text=True,
        )
        commit = result.stdout.strip()
        return commit if result.returncode == 0 and re.fullmatch(r"[0-9a-fA-F]{4,64}", commit) else None
    except Exception:
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", type=Path, default=DEFAULT_DESTINATION)
    parser.add_argument("--mirror", type=Path, default=DEFAULT_MIRROR, help="Best-effort second copy (iCloud Drive).")
    parser.add_argument("--no-mirror", action="store_true", help="Skip the mirror copy.")
    parser.add_argument("--passphrase-file", type=Path, default=DEFAULT_PASSPHRASE_PATH)
    parser.add_argument("--keep", type=int, default=DEFAULT_KEEP)
    parser.add_argument("--include-env", action="store_true")
    parser.add_argument("--init-key", action="store_true", help="Create a private passphrase file and exit.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--notify", action="store_true", help="Show a generic macOS notification on failure.")
    parser.add_argument("--database", type=Path, default=DEFAULT_DATABASE_PATH)
    args = parser.parse_args(argv)
    try:
        passphrase_file = args.passphrase_file.expanduser()
        if args.init_key:
            # The passphrase is never printed; the owner copies it from the
            # private file into a password manager.
            path = init_passphrase(passphrase_file)
            if args.json:
                print(json.dumps({"passphrase_file": str(path), "mode": "0600"}, sort_keys=True))
            else:
                print(f"passphrase_file={path} mode=0600")
            return 0
        report = create_snapshot(
            root=REPO_ROOT, destination=args.destination.expanduser(), passphrase_file=passphrase_file,
            cipher=OpensslCipher(), keep=args.keep, include_env=args.include_env,
            app_commit=_app_commit(), database=args.database.expanduser(),
            mirror=None if args.no_mirror else args.mirror.expanduser(),
        )
        mirror = report.get("mirror")
        mirror_state = "skipped" if mirror is None else mirror["status"]
        if args.json:
            print(json.dumps(report, sort_keys=True))
        else:
            print(f"ok snapshot={report['snapshot']} files={report['file_count']} "
                  f"bytes={report['encrypted_size']} kept={report['kept']} pruned={report['pruned']} "
                  f"mirror={mirror_state}")
        if args.notify and mirror_state == "error":
            _notify(MIRROR_NOTIFICATION)
        return 0
    except Exception as error:
        print(json.dumps({
            "status": "error", "error_type": type(error).__name__,
            "message": str(error) if isinstance(error, SnapshotError) else "backup failed",
        }, sort_keys=True))
        if args.notify:
            _notify(FAILURE_NOTIFICATION)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

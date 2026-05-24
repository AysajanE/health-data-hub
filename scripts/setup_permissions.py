#!/usr/bin/env python3
"""Apply local-only filesystem permissions for sensitive Health Data Hub data."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from stat import S_IMODE
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
DIRECTORY_MODE = 0o700
FILE_MODE = 0o600
REQUIRED_DIRECTORIES = (
    "data",
    "data/secrets",
    "data/quarantine",
    "data/snapshots",
)
PRIVATE_TREES = (
    "data/secrets",
    "data/quarantine",
    "data/snapshots",
    "data/raw",
)
SENSITIVE_FILES = (
    "data/.healthhub.lock",
    "data/warehouse.duckdb",
    "data/warehouse.duckdb.wal",
)


def _relative_to_root(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def _apply_mode(
    *,
    root: Path,
    path: Path,
    desired_mode: int,
    changed_paths: list[str],
    errors: list[str],
    warnings: list[str],
) -> None:
    if not path.exists():
        return
    if path.is_symlink():
        warnings.append(f"skipped symlink: {_relative_to_root(root, path)}")
        return

    actual_mode = S_IMODE(path.stat().st_mode)
    if actual_mode == desired_mode:
        return

    try:
        path.chmod(desired_mode)
    except OSError as exc:
        errors.append(f"failed to chmod {_relative_to_root(root, path)} to {oct(desired_mode)}: {exc}")
        return

    changed_paths.append(_relative_to_root(root, path))


def _ensure_directory(
    *,
    root: Path,
    relative_path: str,
    changed_paths: list[str],
    errors: list[str],
    warnings: list[str],
) -> Path | None:
    path = root / relative_path
    if not path.exists():
        return None
    if not path.is_dir():
        errors.append(f"expected directory but found file: {relative_path}")
        return None

    _apply_mode(
        root=root,
        path=path,
        desired_mode=DIRECTORY_MODE,
        changed_paths=changed_paths,
        errors=errors,
        warnings=warnings,
    )
    return path


def _secure_tree(
    *,
    root: Path,
    tree_root: Path,
    changed_paths: list[str],
    errors: list[str],
    warnings: list[str],
) -> None:
    if not tree_root.exists():
        return
    if not tree_root.is_dir():
        errors.append(f"expected directory but found file: {_relative_to_root(root, tree_root)}")
        return

    for path in sorted(tree_root.rglob("*"), key=lambda item: item.as_posix()):
        desired_mode = DIRECTORY_MODE if path.is_dir() else FILE_MODE
        _apply_mode(
            root=root,
            path=path,
            desired_mode=desired_mode,
            changed_paths=changed_paths,
            errors=errors,
            warnings=warnings,
        )


def setup_permissions(root: Path) -> dict[str, Any]:
    root = root.resolve()
    changed_paths: list[str] = []
    errors: list[str] = []
    warnings: list[str] = []

    for relative_path in REQUIRED_DIRECTORIES:
        _ensure_directory(
            root=root,
            relative_path=relative_path,
            changed_paths=changed_paths,
            errors=errors,
            warnings=warnings,
        )

    for relative_path in PRIVATE_TREES:
        _secure_tree(
            root=root,
            tree_root=root / relative_path,
            changed_paths=changed_paths,
            errors=errors,
            warnings=warnings,
        )

    for relative_path in SENSITIVE_FILES:
        _apply_mode(
            root=root,
            path=root / relative_path,
            desired_mode=FILE_MODE,
            changed_paths=changed_paths,
            errors=errors,
            warnings=warnings,
        )

    changed_paths = sorted(dict.fromkeys(changed_paths))
    return {
        "status": "ok" if not errors else "error",
        "changed_paths": changed_paths,
        "errors": errors,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Secure local Health Data Hub data directories and files.")
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = setup_permissions(Path(args.root))
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        for warning in report["warnings"]:
            print(f"WARNING: {warning}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

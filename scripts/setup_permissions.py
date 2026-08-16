#!/usr/bin/env python3
"""Apply and audit local-only permissions for Health Data Hub runtime data.

The managed roots are deliberately small and fixed.  This module never follows
symlinks, and it creates missing managed directories one component at a time so
the first runtime write does not inherit a process-wide default mode.
"""

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
    "private",
    "private/evidence",
    "models",
)
PRIVATE_TREES = (
    "data",
    "private",
    "models",
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
    if path.is_symlink():
        errors.append(f"refusing symlink in managed runtime tree: {_relative_to_root(root, path)}")
        return
    if not path.exists():
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
    """Create a fixed managed path without traversing a symlink component."""

    path = root
    for part in Path(relative_path).parts:
        path = path / part
        rel = _relative_to_root(root, path)
        if path.is_symlink():
            errors.append(f"refusing symlink in managed runtime path: {rel}")
            return None
        if path.exists():
            if not path.is_dir():
                errors.append(f"expected directory but found non-directory: {rel}")
                return None
        else:
            try:
                path.mkdir(mode=DIRECTORY_MODE)
            except OSError as exc:
                errors.append(f"failed to create managed directory {rel}: {exc}")
                return None
            changed_paths.append(rel)

        _apply_mode(
            root=root,
            path=path,
            desired_mode=DIRECTORY_MODE,
            changed_paths=changed_paths,
            errors=errors,
            warnings=warnings,
        )
    return path


def _iter_tree(root: Path, tree_root: Path, errors: list[str]):
    """Yield a managed tree without following or concealing symlinks."""

    try:
        children = sorted(tree_root.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        errors.append(f"failed to inspect managed directory {_relative_to_root(root, tree_root)}: {exc}")
        return
    for path in children:
        if path.is_symlink():
            errors.append(f"refusing symlink in managed runtime tree: {_relative_to_root(root, path)}")
            continue
        yield path
        if path.is_dir():
            yield from _iter_tree(root, path, errors)


def _secure_tree(
    *,
    root: Path,
    tree_root: Path,
    changed_paths: list[str],
    errors: list[str],
    warnings: list[str],
) -> None:
    if tree_root.is_symlink():
        errors.append(f"refusing symlink in managed runtime tree: {_relative_to_root(root, tree_root)}")
        return
    if not tree_root.exists():
        return
    if not tree_root.is_dir():
        errors.append(f"expected directory but found file: {_relative_to_root(root, tree_root)}")
        return

    _apply_mode(
        root=root,
        path=tree_root,
        desired_mode=DIRECTORY_MODE,
        changed_paths=changed_paths,
        errors=errors,
        warnings=warnings,
    )

    for path in _iter_tree(root, tree_root, errors):
        desired_mode = DIRECTORY_MODE if path.is_dir() else FILE_MODE
        _apply_mode(
            root=root,
            path=path,
            desired_mode=desired_mode,
            changed_paths=changed_paths,
            errors=errors,
            warnings=warnings,
        )


def inspect_permissions(root: Path) -> dict[str, Any]:
    """Read modes only; never read runtime file contents."""

    root = root.resolve()
    errors: list[str] = []
    checked_directories = 0
    checked_files = 0

    for relative_path in REQUIRED_DIRECTORIES:
        path = root / relative_path
        if path.is_symlink():
            errors.append(f"managed runtime path is a symlink: {relative_path}")
            continue
        if not path.exists() or not path.is_dir():
            errors.append(f"managed runtime directory is missing: {relative_path}")
            continue
        checked_directories += 1
        actual_mode = S_IMODE(path.stat().st_mode)
        if actual_mode != DIRECTORY_MODE:
            errors.append(
                f"unsafe directory mode for {relative_path}: {oct(actual_mode)} (expected {oct(DIRECTORY_MODE)})"
            )

    for relative_path in PRIVATE_TREES:
        tree_root = root / relative_path
        if tree_root.is_symlink() or not tree_root.is_dir():
            continue
        for path in _iter_tree(root, tree_root, errors):
            rel = _relative_to_root(root, path)
            if path.is_dir():
                checked_directories += 1
                desired_mode = DIRECTORY_MODE
                kind = "directory"
            else:
                checked_files += 1
                desired_mode = FILE_MODE
                kind = "file"
            actual_mode = S_IMODE(path.stat().st_mode)
            if actual_mode != desired_mode:
                errors.append(
                    f"unsafe {kind} mode for {rel}: {oct(actual_mode)} (expected {oct(desired_mode)})"
                )

    return {
        "status": "ok" if not errors else "error",
        "errors": sorted(dict.fromkeys(errors)),
        "checks": {
            "directory_mode": oct(DIRECTORY_MODE),
            "file_mode": oct(FILE_MODE),
            "managed_roots": list(REQUIRED_DIRECTORIES),
            "checked_directories": checked_directories,
            "checked_files": checked_files,
            "file_contents_read": False,
            "symlinks_followed": False,
        },
    }


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

    changed_paths = sorted(dict.fromkeys(changed_paths))
    audit = inspect_permissions(root)
    errors.extend(audit["errors"])
    errors = sorted(dict.fromkeys(errors))
    return {
        "status": "ok" if not errors else "error",
        "changed_paths": changed_paths,
        "errors": errors,
        "warnings": warnings,
        "checks": audit["checks"],
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

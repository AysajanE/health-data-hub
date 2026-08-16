#!/usr/bin/env python3
"""Classify whether a completed AutoKeel slice is landed on a Git ref.

Durable success is derived exclusively from committed Git objects. Files in the
index or working tree are intentionally invisible to this module.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any


RECEIPT_SCHEMA_VERSION = "autokeel.slice_integration.v1"
ANCHORED_RECEIPT_SCHEMA_VERSION = "autokeel.slice_integration.v2"
SUPPORTED_RECEIPT_SCHEMA_VERSIONS = {
    RECEIPT_SCHEMA_VERSION,
    ANCHORED_RECEIPT_SCHEMA_VERSION,
}
DEFAULT_SLICES_PATH = "ops/autonomy/slices.json"

APPEND_ONLY_SURFACES = {
    "ops/autonomy/events.jsonl",
    "ops/autonomy/failure_ledger.jsonl",
    "ops/autonomy/progress.md",
}
SLICE_ENTRY_STABLE_SURFACE = "ops/autonomy/slices.json"
RUNTIME_STATE_SURFACE = "ops/autonomy/autonomy_state.json"
COMMAND_EVIDENCE_TRUST_BOUNDARY = {
    "evidence_kind": "self_attested_command_transcript",
    "runner_attestation": "not_cryptographically_verified",
    "claim_limit": "structural_integrity_and_committed_hash_binding_only",
}

PASS_RE = re.compile(r"(?im)^\s*(verdict|result)\s*:\s*pass\s*$")
FAIL_RE = re.compile(r"(?im)^\s*(verdict|result)\s*:\s*fail\s*$")
PROVENANCE_RE = re.compile(r"(?i)autonomous slice review|independent reviewer|autonomous review")
EVIDENCE_RE = re.compile(r"(?i)evidence files? checked|evidence checked|files checked")
COMMANDS_RE = re.compile(r"(?i)exact commands run|commands run|verification commands")
COMMAND_EVIDENCE_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?Command evidence:\s*`?([^`\s]+)`?\s*$"
)
BLOCKING_NONE_RE = re.compile(
    r"(?im)^\s*(blocking findings|blocking issues|blockers)\s*:\s*"
    r"(none|no blocking findings|no blockers)\s*$"
)


TreeEntry = dict[str, str] | None


def _required_surface_retention(path: str) -> str:
    if path in APPEND_ONLY_SURFACES:
        return "append_only"
    if path == SLICE_ENTRY_STABLE_SURFACE:
        return "slice_entry_stable"
    if path == RUNTIME_STATE_SURFACE:
        return "runtime_state"
    return "immutable"


class DuplicateJsonKeyError(ValueError):
    pass


def _strict_json_loads(text: str, description: str) -> tuple[Any | None, str | None]:
    def no_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise DuplicateJsonKeyError(f"duplicate object key: {key}")
            result[key] = value
        return result

    try:
        return json.loads(text, object_pairs_hook=no_duplicates), None
    except (json.JSONDecodeError, DuplicateJsonKeyError) as exc:
        return None, f"{description} is not valid strict JSON: {exc}"


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _run_git(
    root: Path,
    *argv: str,
    text: bool = True,
) -> subprocess.CompletedProcess[Any]:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    return subprocess.run(
        ["git", *argv],
        cwd=root,
        env=env,
        text=text,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def validate_repository(
    root: Path,
    *,
    expected_main_commit: str | None = None,
) -> list[str]:
    """Fail closed when Git can resolve objects outside the expected repository.

    Git redirecting environment variables are scrubbed by :func:`_run_git`.
    This check additionally pins discovery to ``root`` and rejects object graph
    mechanisms that could make a receipt depend on hidden or mutable history.
    """

    root = root.resolve()
    errors: list[str] = []
    canonical_git_dir = root / ".git"
    if not canonical_git_dir.exists():
        return [f"expected repository metadata is absent: {root / '.git'}"]
    if canonical_git_dir.is_symlink() or not canonical_git_dir.is_dir():
        return [
            "production integration verification requires the canonical root/.git directory"
        ]

    top = _run_git(root, "rev-parse", "--show-toplevel")
    if top.returncode != 0:
        return [f"could not resolve expected repository root: {top.stderr.strip()}"]
    try:
        resolved_top = Path(top.stdout.strip()).resolve()
    except OSError as exc:
        return [f"could not resolve expected repository root: {exc}"]
    if resolved_top != root:
        errors.append(f"Git repository root mismatch: {resolved_top} != {root}")

    git_dir_result = _run_git(root, "rev-parse", "--absolute-git-dir")
    common_dir_result = _run_git(root, "rev-parse", "--git-common-dir")
    if git_dir_result.returncode != 0:
        errors.append(f"could not resolve pinned Git directory: {git_dir_result.stderr.strip()}")
        return errors
    if common_dir_result.returncode != 0:
        errors.append(
            f"could not resolve pinned Git common directory: {common_dir_result.stderr.strip()}"
        )
        return errors
    git_dir = Path(git_dir_result.stdout.strip()).resolve()
    common_raw = Path(common_dir_result.stdout.strip())
    common_dir = (
        common_raw.resolve()
        if common_raw.is_absolute()
        else (root / common_raw).resolve()
    )
    canonical_git_dir = canonical_git_dir.resolve()
    if not git_dir.is_dir() or not common_dir.is_dir():
        errors.append("pinned Git directory or common directory is not a directory")
    if git_dir != canonical_git_dir or common_dir != canonical_git_dir:
        errors.append(
            "production integration verification rejects separate/external Git directories: "
            f"git_dir={git_dir}; common_dir={common_dir}; expected={canonical_git_dir}"
        )

    symbolic_head = _run_git(root, "symbolic-ref", "-q", "HEAD")
    if symbolic_head.returncode != 0:
        errors.append("production integration verification rejects detached HEAD")
    elif symbolic_head.stdout.strip() != "refs/heads/main":
        errors.append(
            "production integration verification requires symbolic HEAD refs/heads/main"
        )
    main_commit, main_error = resolve_commit(root, "refs/heads/main")
    if main_error or main_commit is None:
        errors.append(main_error or "could not resolve refs/heads/main")
    elif expected_main_commit is not None and main_commit != expected_main_commit:
        errors.append(
            "refs/heads/main does not match the expected verification commit: "
            f"{main_commit} != {expected_main_commit}"
        )

    shallow = _run_git(root, "rev-parse", "--is-shallow-repository")
    if shallow.returncode != 0:
        errors.append(f"could not inspect shallow repository state: {shallow.stderr.strip()}")
    elif shallow.stdout.strip() != "false":
        errors.append("anchored integration verification rejects shallow repositories")

    for path, label in (
        (git_dir / "info" / "grafts", "grafts"),
        (common_dir / "info" / "grafts", "common-dir grafts"),
        (common_dir / "objects" / "info" / "alternates", "object alternates"),
    ):
        try:
            if path.is_symlink() or (path.is_file() and path.stat().st_size > 0):
                errors.append(f"anchored integration verification rejects Git {label}: {path}")
        except OSError as exc:
            errors.append(f"could not inspect Git {label}: {path}: {exc}")

    replacements = _run_git(root, "for-each-ref", "--format=%(refname)", "refs/replace")
    if replacements.returncode != 0:
        errors.append(f"could not inspect Git replace refs: {replacements.stderr.strip()}")
    elif replacements.stdout.strip():
        errors.append("anchored integration verification rejects Git replace refs")
    return errors


def _safe_ref(ref: str) -> bool:
    return bool(ref) and not ref.startswith("-") and "\x00" not in ref and " " not in ref


def _resolve_branch_commit(root: Path, branch: str) -> tuple[str | None, str | None]:
    if not isinstance(branch, str) or not branch or branch.startswith("refs/"):
        return None, f"unsafe or non-local branch name: {branch!r}"
    check = _run_git(root, "check-ref-format", "--branch", branch)
    if check.returncode != 0:
        return None, f"invalid local branch name {branch!r}: {check.stderr.strip()}"
    return resolve_commit(root, f"refs/heads/{branch}")


def _safe_repo_path(value: Any) -> str | None:
    if not isinstance(value, str) or not value or "\x00" in value or ":" in value or "\\" in value:
        return None
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    if path.parts and path.parts[0] == ".git":
        return None
    return path.as_posix()


def _artifact_path(value: Any, *, directory: str, suffix: str) -> str | None:
    safe = _safe_repo_path(value)
    if safe is None:
        return None
    path = PurePosixPath(safe)
    if path.parent.as_posix() != directory or path.suffix != suffix or path.name == suffix:
        return None
    return safe


def _review_artifact_path(value: Any) -> str | None:
    return _artifact_path(value, directory="docs/reviews", suffix=".md")


def _evidence_artifact_path(value: Any) -> str | None:
    return _artifact_path(value, directory="docs/evidence", suffix=".json")


def resolve_commit(root: Path, ref: str) -> tuple[str | None, str | None]:
    if not _safe_ref(ref):
        return None, f"unsafe or empty Git ref: {ref!r}"
    result = _run_git(root, "rev-parse", "--verify", f"{ref}^{{commit}}")
    if result.returncode != 0:
        return None, f"could not resolve Git ref {ref}: {result.stderr.strip()}"
    return result.stdout.strip(), None


def committed_text(root: Path, commit: str, rel_path: str) -> tuple[str | None, str | None]:
    safe_path = _safe_repo_path(rel_path)
    if safe_path is None:
        return None, f"unsafe repository path: {rel_path!r}"
    result = _run_git(root, "show", f"{commit}:{safe_path}")
    if result.returncode != 0:
        return None, f"path is not committed at {commit}: {safe_path}"
    return result.stdout, None


def committed_bytes(root: Path, commit: str, rel_path: str) -> tuple[bytes | None, str | None]:
    safe_path = _safe_repo_path(rel_path)
    if safe_path is None:
        return None, f"unsafe repository path: {rel_path!r}"
    result = _run_git(root, "show", f"{commit}:{safe_path}", text=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        return None, f"path is not committed at {commit}: {safe_path}: {stderr}"
    return result.stdout, None


def _receipt_anchor_commit(
    root: Path,
    continuation_commit: str,
    receipt_path: str,
) -> tuple[str | None, str | None]:
    result = _run_git(
        root,
        "log",
        "--first-parent",
        "--format=%H",
        continuation_commit,
        "--",
        receipt_path,
    )
    if result.returncode != 0:
        return None, f"could not resolve receipt anchor commit: {result.stderr.strip()}"
    touches = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    if not touches:
        return None, f"could not resolve receipt anchor commit for {receipt_path}"
    if len(touches) != 1:
        return None, (
            f"anchored receipt path must have exactly one immutable history touch: "
            f"{receipt_path}: found {len(touches)}"
        )
    anchor_commit = touches[0]
    parents = _run_git(root, "rev-list", "--parents", "-n", "1", anchor_commit)
    if parents.returncode != 0:
        return None, f"could not inspect receipt anchor parents: {parents.stderr.strip()}"
    parent_commits = parents.stdout.strip().split()[1:]
    for parent in parent_commits:
        parent_entry, parent_error = git_tree_entry(root, parent, receipt_path)
        if parent_error:
            return None, parent_error
        if parent_entry is not None:
            return None, (
                f"anchored receipt must be created at its only history touch; "
                f"path already existed in parent {parent}: {receipt_path}"
            )
    return anchor_commit, None


def _discover_anchored_v2_receipts(
    root: Path,
    tip_commit: str,
    identity: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Discover every matching v2 receipt created on first-parent history."""

    errors: list[str] = []
    discovered: dict[str, dict[str, Any]] = {}
    history = _run_git(root, "rev-list", "--first-parent", tip_commit)
    if history.returncode != 0:
        return {}, [f"could not enumerate receipt registry history: {history.stderr.strip()}"]
    for commit in (line.strip() for line in history.stdout.splitlines() if line.strip()):
        additions = _run_git(
            root,
            "diff-tree",
            "--root",
            "--no-commit-id",
            "--name-only",
            "--diff-filter=A",
            "-r",
            "-z",
            commit,
            "--",
            "docs/evidence",
            text=False,
        )
        if additions.returncode != 0:
            stderr = additions.stderr.decode("utf-8", errors="replace").strip()
            errors.append(f"could not inspect receipt additions at {commit}: {stderr}")
            continue
        for raw_path in (value for value in additions.stdout.split(b"\x00") if value):
            path = raw_path.decode("utf-8", errors="surrogateescape")
            if _evidence_artifact_path(path) != path:
                continue
            text, read_error = committed_text(root, commit, path)
            if read_error or text is None:
                errors.append(read_error or f"could not read receipt candidate: {path}")
                continue
            payload, parse_error = _parse_json_object(
                text, f"receipt registry candidate {path} at {commit}"
            )
            if parse_error or payload is None:
                continue
            if payload.get("schema_version") != ANCHORED_RECEIPT_SCHEMA_VERSION:
                continue
            if payload.get("slice") != identity.get("slice"):
                continue
            drifted = [
                field
                for field, expected in identity.items()
                if payload.get(field) != expected
            ]
            if drifted:
                errors.append(
                    "receipt registry detected slice identity drift: "
                    f"{path}: {', '.join(drifted)}"
                )
            if path in discovered:
                errors.append(f"receipt registry repeats an added receipt path: {path}")
                continue
            actual_anchor, anchor_error = _receipt_anchor_commit(root, tip_commit, path)
            if anchor_error:
                errors.append(anchor_error)
            elif actual_anchor != commit:
                errors.append(
                    f"receipt registry creation anchor mismatch: {path}: {actual_anchor} != {commit}"
                )
            entry, entry_error = git_tree_entry(root, commit, path)
            if entry_error:
                errors.append(entry_error)
            discovered[path] = {
                "anchor_commit": commit,
                "receipt": payload,
                "tree_entry": entry,
            }
    return discovered, errors


def _receipt_registry_topology(
    receipts: dict[str, dict[str, Any]],
    *,
    expected_tip: str | None = None,
) -> tuple[str | None, list[str]]:
    if not receipts:
        return None, ([] if expected_tip is None else ["receipt registry is empty"])
    errors: list[str] = []
    predecessors: dict[str, str | None] = {}
    for path, record in receipts.items():
        payload = record.get("receipt")
        supersedes = payload.get("supersedes_receipt") if isinstance(payload, dict) else None
        if supersedes is None:
            predecessors[path] = None
        elif isinstance(supersedes, dict):
            predecessor = _evidence_artifact_path(supersedes.get("path"))
            predecessors[path] = predecessor
            if predecessor is None or predecessor not in receipts:
                errors.append(
                    f"receipt registry predecessor is absent or invalid: {path}: {predecessor!r}"
                )
        else:
            predecessors[path] = None
            errors.append(f"receipt registry has malformed supersedes_receipt: {path}")
    referenced = {path for path in predecessors.values() if path is not None}
    tips = sorted(set(receipts) - referenced)
    roots = sorted(path for path, predecessor in predecessors.items() if predecessor is None)
    if len(tips) != 1:
        errors.append(f"receipt registry must have exactly one chain tip; found {tips}")
    if len(roots) != 1:
        errors.append(f"receipt registry must have exactly one root; found {roots}")
    tip = tips[0] if len(tips) == 1 else None
    if expected_tip is not None and tip != expected_tip:
        errors.append(
            f"configured receipt is not the durable registry chain tip: {expected_tip} != {tip}"
        )
    if tip is not None:
        visited: set[str] = set()
        cursor: str | None = tip
        while cursor is not None:
            if cursor in visited:
                errors.append(f"receipt registry contains a cycle at {cursor}")
                break
            visited.add(cursor)
            cursor = predecessors.get(cursor)
        missing = sorted(set(receipts) - visited)
        if missing:
            errors.append(
                "receipt registry contains disconnected/reset history: " + ", ".join(missing)
            )
    return tip, errors


def _validate_receipt_registry(
    root: Path,
    *,
    tip_commit: str,
    current_receipt_path: str,
    identity: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    receipts, errors = _discover_anchored_v2_receipts(root, tip_commit, identity)
    _, topology_errors = _receipt_registry_topology(
        receipts, expected_tip=current_receipt_path
    )
    errors.extend(topology_errors)
    return receipts, errors


def git_tree_entry(root: Path, commit: str, rel_path: str) -> tuple[TreeEntry, str | None]:
    """Return the exact committed mode/type/object tuple for one path."""

    safe_path = _safe_repo_path(rel_path)
    if safe_path is None:
        return None, f"unsafe repository path: {rel_path!r}"
    result = _run_git(root, "ls-tree", "-z", commit, "--", safe_path, text=False)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        return None, f"could not inspect tree entry {safe_path} at {commit}: {stderr}"
    if not result.stdout:
        return None, None
    records = [row for row in result.stdout.split(b"\x00") if row]
    if len(records) != 1:
        return None, f"expected one tree entry for {safe_path} at {commit}, found {len(records)}"
    try:
        metadata, actual_path = records[0].split(b"\t", 1)
        mode, object_type, object_id = metadata.decode("ascii").split(" ", 2)
        decoded_path = actual_path.decode("utf-8", errors="surrogateescape")
    except (ValueError, UnicodeDecodeError) as exc:
        return None, f"could not parse tree entry for {safe_path} at {commit}: {exc}"
    if decoded_path != safe_path:
        return None, f"tree entry path mismatch at {commit}: {decoded_path!r} != {safe_path!r}"
    return {"mode": mode, "type": object_type, "object": object_id}, None


def _changed_paths(root: Path, merge_base: str, ship_commit: str) -> tuple[list[str], str | None]:
    result = _run_git(
        root,
        "diff",
        "--no-renames",
        "--name-only",
        "-z",
        merge_base,
        ship_commit,
        text=False,
    )
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        return [], f"could not enumerate ship-changed paths: {stderr}"
    paths: list[str] = []
    for raw_path in result.stdout.split(b"\x00"):
        if not raw_path:
            continue
        path = raw_path.decode("utf-8", errors="surrogateescape")
        if _safe_repo_path(path) is None:
            return [], f"ship range contains an unsafe path: {path!r}"
        paths.append(path)
    return sorted(paths), None


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> tuple[bool, str | None]:
    result = _run_git(root, "merge-base", "--is-ancestor", ancestor, descendant)
    if result.returncode == 0:
        return True, None
    if result.returncode == 1:
        return False, None
    return False, f"could not evaluate ancestry: {result.stderr.strip()}"


def _first_parent_transitions(
    root: Path,
    anchor_commit: str,
    continuation_commit: str,
) -> tuple[list[tuple[str, str]], list[str]]:
    """Return every adjacent first-parent edge from anchor to continuation."""

    if anchor_commit == continuation_commit:
        return [], []
    result = _run_git(
        root,
        "rev-list",
        "--first-parent",
        "--reverse",
        f"{anchor_commit}..{continuation_commit}",
    )
    if result.returncode != 0:
        return [], [f"could not enumerate first-parent receipt history: {result.stderr.strip()}"]
    commits = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    transitions: list[tuple[str, str]] = []
    parent = anchor_commit
    for commit in commits:
        row = _run_git(root, "rev-list", "--parents", "-n", "1", commit)
        if row.returncode != 0:
            return [], [f"could not inspect first parent for {commit}: {row.stderr.strip()}"]
        parents = row.stdout.strip().split()[1:]
        if not parents or parents[0] != parent:
            return [], [
                "receipt anchor is not on the continuation first-parent chain: "
                f"expected {parent} as first parent of {commit}"
            ]
        transitions.append((parent, commit))
        parent = commit
    if parent != continuation_commit:
        return [], [
            "receipt anchor is not on the continuation first-parent chain: "
            f"history ended at {parent}, expected {continuation_commit}"
        ]
    return transitions, []


def _is_regular_blob(entry: TreeEntry) -> bool:
    return bool(
        isinstance(entry, dict)
        and entry.get("mode") == "100644"
        and entry.get("type") == "blob"
        and isinstance(entry.get("object"), str)
        and re.fullmatch(r"[0-9a-fA-F]{40,64}", entry["object"]) is not None
    )


def _surface_entries(
    root: Path,
    change_base: str,
    ship_commit: str,
    continuation_commit: str,
    paths: list[str],
) -> tuple[dict[str, dict[str, TreeEntry]], list[str]]:
    entries: dict[str, dict[str, TreeEntry]] = {}
    errors: list[str] = []
    for path in paths:
        base_entry, base_error = git_tree_entry(root, change_base, path)
        ship_entry, ship_error = git_tree_entry(root, ship_commit, path)
        continuation_entry, continuation_error = git_tree_entry(root, continuation_commit, path)
        if base_error:
            errors.append(base_error)
        if ship_error:
            errors.append(ship_error)
        if continuation_error:
            errors.append(continuation_error)
        entries[path] = {
            "base": base_entry,
            "ship": ship_entry,
            "continuation": continuation_entry,
        }
    return entries, errors


def _surface_retention(
    root: Path,
    change_base: str,
    ship_commit: str,
    continuation_commit: str,
    surface_entries: dict[str, dict[str, TreeEntry]],
) -> tuple[bool, dict[str, str], list[str]]:
    """Classify whether every ship-changed path still retains its ship state.

    Only exact object identity is self-proving. A later blob can contain the
    shipped text while restoring the old behavior elsewhere, so neither textual
    hunk presence nor arbitrary object inequality proves semantic retention.
    Every non-identical surface therefore requires the committed, object-hash-
    bound reconciliation receipt handled below.
    """

    dispositions: dict[str, str] = {}
    errors: list[str] = []
    for path, row in surface_entries.items():
        base_entry = row["base"]
        ship_entry = row["ship"]
        continuation_entry = row["continuation"]
        if continuation_entry == ship_entry:
            disposition = "identical"
        elif continuation_entry == base_entry:
            disposition = "reverted-to-pre-ship"
        elif ship_entry is not None and continuation_entry is None:
            disposition = "removed-after-ship"
        elif ship_entry is None and continuation_entry is not None:
            disposition = "restored-after-ship-deletion"
        else:
            disposition = "ambiguous-evolution"
        dispositions[path] = disposition
    retained = not errors and all(value == "identical" for value in dispositions.values())
    return retained, dispositions, errors


def _patch_equivalence(root: Path, continuation_commit: str, ship_commit: str) -> tuple[dict[str, Any], str | None]:
    merges = _run_git(root, "rev-list", "--merges", f"{continuation_commit}..{ship_commit}")
    if merges.returncode != 0:
        return {}, f"could not enumerate ship-exclusive merge commits: {merges.stderr.strip()}"
    exclusive_merges = [line for line in merges.stdout.splitlines() if line.strip()]

    cherry = _run_git(root, "cherry", continuation_commit, ship_commit)
    if cherry.returncode != 0:
        return {}, f"could not compare patch identities: {cherry.stderr.strip()}"
    rows = [line.strip() for line in cherry.stdout.splitlines() if line.strip()]
    equivalent = [line.split()[1] for line in rows if line.startswith("-") and len(line.split()) >= 2]
    unmatched = [line.split()[1] for line in rows if line.startswith("+") and len(line.split()) >= 2]
    malformed = [line for line in rows if not line.startswith(("+", "-")) or len(line.split()) < 2]
    return {
        "exclusive_merge_commits": exclusive_merges,
        "equivalent_commits": equivalent,
        "unmatched_commits": unmatched,
        "rows": rows,
        "malformed_rows": malformed,
    }, None


def _parse_json_object(text: str, description: str) -> tuple[dict[str, Any] | None, str | None]:
    payload, parse_error = _strict_json_loads(text, description)
    if parse_error:
        return None, parse_error
    if not isinstance(payload, dict):
        return None, f"{description} must contain a JSON object"
    return payload, None


def _validate_command_evidence_payload(payload: Any, path: str) -> tuple[set[str], list[str]]:
    errors: list[str] = []
    successful: set[str] = set()
    if not isinstance(payload, dict):
        return successful, [f"review command evidence must be a JSON object: {path}"]
    commands = payload.get("commands")
    if not isinstance(commands, list) or not commands:
        return successful, [f"review command evidence must contain commands: {path}"]
    for index, row in enumerate(commands, start=1):
        if not isinstance(row, dict):
            errors.append(f"review command evidence row {index} is not an object: {path}")
            continue
        command = row.get("command")
        exit_code = row.get("exit_code")
        if not isinstance(command, str) or not command.strip():
            errors.append(f"review command evidence row {index} has no command: {path}")
            continue
        if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code != 0:
            errors.append(f"review command evidence command did not pass: {command}: {path}")
            continue
        row_status = row.get("status")
        if row_status is not None and row_status not in {"ok", "pass", "passed"}:
            errors.append(
                f"review command evidence command has non-passing status: "
                f"{command}: {row_status!r}: {path}"
            )
            continue
        successful.add(command)
    return successful, errors


def _validate_committed_review(
    root: Path,
    continuation_commit: str,
    review_path: str,
) -> tuple[set[str], list[str], list[str]]:
    text, read_error = committed_text(root, continuation_commit, review_path)
    if read_error or text is None:
        return set(), [read_error or f"could not read review artifact: {review_path}"], []
    errors: list[str] = []
    stripped = text.strip()
    if len(stripped) < 120:
        errors.append(f"review artifact too short to be meaningful: {review_path}")
    if not PROVENANCE_RE.search(stripped):
        errors.append(f"review artifact lacks autonomous reviewer provenance: {review_path}")
    if FAIL_RE.search(stripped):
        errors.append(f"review artifact contains a failing verdict: {review_path}")
    if not PASS_RE.search(stripped):
        errors.append(f"review artifact lacks Verdict: pass: {review_path}")
    if not EVIDENCE_RE.search(stripped):
        errors.append(f"review artifact does not identify evidence checked: {review_path}")
    if not COMMANDS_RE.search(stripped):
        errors.append(f"review artifact does not identify verification commands: {review_path}")
    if not BLOCKING_NONE_RE.search(stripped):
        errors.append(f"review artifact does not state that blocking findings are none: {review_path}")

    evidence_paths = COMMAND_EVIDENCE_RE.findall(stripped)
    if not evidence_paths:
        errors.append(f"review artifact has no Command evidence path: {review_path}")
    successful_commands: set[str] = set()
    for raw_path in evidence_paths:
        evidence_path = _safe_repo_path(raw_path)
        if evidence_path is None:
            errors.append(f"review artifact contains unsafe command evidence path: {raw_path!r}")
            continue
        evidence_text, evidence_error = committed_text(root, continuation_commit, evidence_path)
        if evidence_error or evidence_text is None:
            errors.append(evidence_error or f"could not read command evidence: {evidence_path}")
            continue
        evidence_payload, evidence_parse_error = _strict_json_loads(
            evidence_text, f"review command evidence {evidence_path}"
        )
        if evidence_parse_error:
            errors.append(evidence_parse_error)
            continue
        passed, evidence_errors = _validate_command_evidence_payload(evidence_payload, evidence_path)
        successful_commands.update(passed)
        errors.extend(evidence_errors)
    return successful_commands, errors, evidence_paths


def _validate_anchored_command_evidence(
    root: Path,
    *,
    path: str,
    payload: Any,
    slice_id: str,
    verification_commit: str,
    anchor_commit: str,
    continuation_commit: str,
) -> tuple[set[str], list[str]]:
    successful, errors = _validate_command_evidence_payload(payload, path)
    if not isinstance(payload, dict):
        return successful, errors
    if payload.get("slice") != slice_id:
        errors.append(f"command evidence slice mismatch: {path}")
    if payload.get("status") != "ok":
        errors.append(f"command evidence status is not ok: {path}")
    if payload.get("tested_commit") != verification_commit:
        errors.append(f"command evidence tested_commit mismatch: {path}")

    reviewed_files = payload.get("reviewed_files")
    if not isinstance(reviewed_files, list) or not reviewed_files:
        errors.append(f"anchored command evidence needs reviewed_files: {path}")
        return successful, errors
    seen: set[str] = set()
    reviewed_paths: list[str] = []
    for index, row in enumerate(reviewed_files, start=1):
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            errors.append(f"reviewed_files row {index} has invalid fields: {path}")
            continue
        reviewed_path = _safe_repo_path(row.get("path"))
        expected_sha = row.get("sha256")
        if reviewed_path is None:
            errors.append(f"reviewed_files row {index} has unsafe path: {path}")
            continue
        if reviewed_path in seen:
            errors.append(f"reviewed_files repeats path: {reviewed_path}: {path}")
            continue
        seen.add(reviewed_path)
        reviewed_paths.append(reviewed_path)
        if not isinstance(expected_sha, str) or re.fullmatch(r"[0-9a-f]{64}", expected_sha) is None:
            errors.append(f"reviewed_files row {index} has invalid sha256: {path}")
            continue
        tested_entry, tested_error = git_tree_entry(root, verification_commit, reviewed_path)
        anchor_entry, anchor_error = git_tree_entry(root, anchor_commit, reviewed_path)
        current_entry, current_error = git_tree_entry(root, continuation_commit, reviewed_path)
        for entry_error in (tested_error, anchor_error, current_error):
            if entry_error:
                errors.append(entry_error)
        if not _is_regular_blob(tested_entry):
            errors.append(
                f"reviewed file is not a regular 100644 committed blob: {reviewed_path}"
            )
            continue
        if anchor_entry != tested_entry or current_entry != tested_entry:
            errors.append(f"reviewed file changed after tested commit: {reviewed_path}")
        blob, blob_error = committed_bytes(root, verification_commit, reviewed_path)
        if blob_error:
            errors.append(blob_error)
        elif blob is not None and hashlib.sha256(blob).hexdigest() != expected_sha:
            errors.append(f"reviewed_files sha256 mismatch: {reviewed_path}")
    if reviewed_paths != sorted(reviewed_paths):
        errors.append(f"reviewed_files paths must be sorted: {path}")
    return successful, errors


def _unique_slice_row(
    text: str,
    slice_id: str,
    description: str,
) -> tuple[dict[str, Any] | None, list[str]]:
    payload, parse_error = _strict_json_loads(text, description)
    if parse_error:
        return None, [parse_error]
    if not isinstance(payload, list):
        return None, [f"{description} must contain a list"]
    matches = [
        row
        for row in payload
        if isinstance(row, dict) and row.get("id") == slice_id
    ]
    if len(matches) != 1:
        return None, [
            f"{description} must contain exactly one {slice_id} row; found {len(matches)}"
        ]
    return matches[0], []


def _slice_inventory(
    text: str,
    description: str,
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    payload, parse_error = _strict_json_loads(text, description)
    if parse_error:
        return {}, [parse_error]
    if not isinstance(payload, list):
        return {}, [f"{description} must contain a list"]
    rows: dict[str, dict[str, Any]] = {}
    errors: list[str] = []
    for index, row in enumerate(payload, start=1):
        if not isinstance(row, dict):
            errors.append(f"{description} row {index} is not an object")
            continue
        row_id = row.get("id")
        if not isinstance(row_id, str) or not row_id:
            errors.append(f"{description} row {index} has no string id")
            continue
        if row_id in rows:
            errors.append(f"{description} contains duplicate slice id: {row_id}")
            continue
        rows[row_id] = row
    return rows, errors


def _event_log_max_id(text: str, description: str) -> tuple[int | None, list[str]]:
    errors: list[str] = []
    maximum = 0
    for line_number, line in enumerate(text.splitlines(), start=1):
        if not line.strip():
            continue
        row, parse_error = _strict_json_loads(
            line, f"{description} line {line_number}"
        )
        if parse_error:
            errors.append(parse_error)
            continue
        if not isinstance(row, dict):
            errors.append(f"{description} line {line_number} is not an object")
            continue
        event_id = row.get("event_id")
        if type(event_id) is not int or event_id <= 0:
            errors.append(
                f"{description} line {line_number} has invalid event_id: {event_id!r}"
            )
            continue
        maximum = max(maximum, event_id)
    return (None if errors else maximum), errors


def _validate_anchored_surface_retention(
    root: Path,
    *,
    slice_id: str,
    path: str,
    retention: Any,
    anchor_commit: str,
    continuation_commit: str,
    anchor_entry: TreeEntry,
    current_entry: TreeEntry,
) -> list[str]:
    errors: list[str] = []
    allowed = {"immutable", "append_only", "slice_entry_stable", "runtime_state"}
    if retention not in allowed:
        return [f"anchored receipt surface has invalid retention mode: {path}: {retention!r}"]

    if retention == "immutable":
        if current_entry != anchor_entry:
            errors.append(f"anchored immutable surface changed after receipt: {path}")
        return errors

    if retention == "append_only":
        if path not in APPEND_ONLY_SURFACES:
            return [f"append_only retention is not allowed for surface: {path}"]
        if anchor_entry is None or current_entry is None:
            return [f"append_only surface must exist at anchor and continuation: {path}"]
        if (
            anchor_entry.get("type") != "blob"
            or current_entry.get("type") != "blob"
            or anchor_entry.get("mode") != current_entry.get("mode")
        ):
            return [f"append_only surface changed type or mode after receipt: {path}"]
        anchor_bytes, anchor_error = committed_bytes(root, anchor_commit, path)
        current_bytes, current_error = committed_bytes(root, continuation_commit, path)
        if anchor_error:
            errors.append(anchor_error)
        if current_error:
            errors.append(current_error)
        if not errors and anchor_bytes is not None and current_bytes is not None:
            if not current_bytes.startswith(anchor_bytes):
                errors.append(f"append_only surface rewrote or removed anchored bytes: {path}")
            elif anchor_bytes and not anchor_bytes.endswith(b"\n"):
                errors.append(f"append_only anchor does not end at a complete line: {path}")
            elif current_bytes and not current_bytes.endswith(b"\n"):
                errors.append(f"append_only continuation does not end at a complete line: {path}")
            else:
                try:
                    anchor_text = anchor_bytes.decode("utf-8")
                    current_text = current_bytes.decode("utf-8")
                    suffix_text = current_bytes[len(anchor_bytes) :].decode("utf-8")
                except UnicodeDecodeError as exc:
                    errors.append(f"append_only surface is not UTF-8 text: {path}: {exc}")
                    return errors
                if path == "ops/autonomy/events.jsonl":
                    anchor_max, anchor_event_errors = _event_log_max_id(
                        anchor_text, f"anchored {path}"
                    )
                    _, current_event_errors = _event_log_max_id(
                        current_text, f"continuation {path}"
                    )
                    errors.extend(anchor_event_errors)
                    errors.extend(current_event_errors)
                    prior = anchor_max or 0
                    for index, line in enumerate(suffix_text.splitlines(), start=1):
                        if not line.strip():
                            continue
                        row, parse_error = _strict_json_loads(
                            line, f"appended {path} row {index}"
                        )
                        if parse_error:
                            errors.append(parse_error)
                            continue
                        event_id = row.get("event_id") if isinstance(row, dict) else None
                        if type(event_id) is not int or event_id <= prior:
                            errors.append(
                                f"appended event ids must be strictly increasing after {prior}: "
                                f"{event_id!r}"
                            )
                            continue
                        prior = event_id
                elif path == "ops/autonomy/failure_ledger.jsonl":
                    for index, line in enumerate(current_text.splitlines(), start=1):
                        if not line.strip():
                            continue
                        row, parse_error = _strict_json_loads(
                            line, f"continuation {path} row {index}"
                        )
                        if parse_error:
                            errors.append(parse_error)
                        elif not isinstance(row, dict):
                            errors.append(f"continuation {path} row {index} is not an object")
                        elif "open" in row and type(row["open"]) is not bool:
                            errors.append(
                                f"continuation {path} row {index} has non-boolean open"
                            )
                elif path == "ops/autonomy/progress.md":
                    for index, line in enumerate(suffix_text.splitlines(), start=1):
                        if line and not line.startswith("- "):
                            errors.append(
                                f"appended progress row {index} is not a supervisor list row"
                            )
        return errors

    if retention == "slice_entry_stable":
        if path != SLICE_ENTRY_STABLE_SURFACE:
            return [f"slice_entry_stable retention is not allowed for surface: {path}"]
        if anchor_entry is None or current_entry is None:
            return [f"slice_entry_stable surface must exist at anchor and continuation: {path}"]
        if (
            anchor_entry.get("type") != "blob"
            or current_entry.get("type") != "blob"
            or anchor_entry.get("mode") != current_entry.get("mode")
        ):
            return [f"slice_entry_stable surface changed type or mode after receipt: {path}"]
        anchor_text, anchor_error = committed_text(root, anchor_commit, path)
        current_text, current_error = committed_text(root, continuation_commit, path)
        if anchor_error:
            errors.append(anchor_error)
        if current_error:
            errors.append(current_error)
        if errors or anchor_text is None or current_text is None:
            return errors
        anchor_rows, anchor_errors = _slice_inventory(anchor_text, f"anchored {path}")
        current_rows, current_errors = _slice_inventory(current_text, f"continuation {path}")
        errors.extend(anchor_errors)
        errors.extend(current_errors)
        anchor_row = anchor_rows.get(slice_id)
        current_row = current_rows.get(slice_id)
        if anchor_row is None or current_row is None:
            errors.append(f"slice entry retention requires exactly one {slice_id} row")
        elif _canonical_json(anchor_row) != _canonical_json(current_row):
            errors.append(f"historical slice row changed after receipt: {slice_id}: {path}")
        anchored_required_or_complete = {
            row_id
            for row_id, row in anchor_rows.items()
            if row.get("required") is True or row.get("status") == "complete"
        }
        missing_rows = sorted(anchored_required_or_complete - set(current_rows))
        if missing_rows:
            errors.append(
                "continuation slices removed anchored required/completed rows: "
                + ", ".join(missing_rows)
            )
        return errors

    if path != RUNTIME_STATE_SURFACE:
        return [f"runtime_state retention is not allowed for surface: {path}"]
    if anchor_entry is None or current_entry is None:
        return [f"runtime_state surface must exist at anchor and continuation: {path}"]
    if (
        anchor_entry.get("type") != "blob"
        or current_entry.get("type") != "blob"
        or anchor_entry.get("mode") != current_entry.get("mode")
    ):
        return [f"runtime_state surface changed type or mode after receipt: {path}"]
    anchor_text, anchor_error = committed_text(root, anchor_commit, path)
    current_text, current_error = committed_text(root, continuation_commit, path)
    if anchor_error:
        errors.append(anchor_error)
    if current_error:
        errors.append(current_error)
    if errors or anchor_text is None or current_text is None:
        return errors
    anchor_state, anchor_parse_error = _parse_json_object(anchor_text, f"anchored {path}")
    current_state, current_parse_error = _parse_json_object(current_text, f"continuation {path}")
    if anchor_parse_error:
        errors.append(anchor_parse_error)
    if current_parse_error:
        errors.append(current_parse_error)
    if errors or anchor_state is None or current_state is None:
        return errors

    dynamic_fields = {
        "active_run",
        "active_swr_run",
        "completed_slices",
        "current_slice",
        "last_event_id",
        "run_history",
        "v1_complete",
    }
    if set(anchor_state) != set(current_state):
        errors.append("runtime state keys changed after receipt; a new receipt is required")
    for key in sorted(set(anchor_state) - dynamic_fields):
        if current_state.get(key) != anchor_state.get(key):
            errors.append(f"runtime state stable field changed after receipt: {key}")

    anchor_completed = anchor_state.get("completed_slices")
    current_completed = current_state.get("completed_slices")
    if not isinstance(anchor_completed, list) or not isinstance(current_completed, list):
        errors.append("runtime state completed_slices must remain lists")
    else:
        if (
            not all(isinstance(value, str) and value for value in anchor_completed)
            or len(set(anchor_completed)) != len(anchor_completed)
            or not all(isinstance(value, str) and value for value in current_completed)
            or len(set(current_completed)) != len(current_completed)
        ):
            errors.append("runtime state completed_slices must contain unique string ids")
        if slice_id not in anchor_completed or slice_id not in current_completed:
            errors.append(f"runtime state no longer proves completed slice: {slice_id}")
        if current_completed[: len(anchor_completed)] != anchor_completed:
            errors.append("runtime state completed_slices rewrote anchored history")

    anchor_history = anchor_state.get("run_history")
    current_history = current_state.get("run_history")
    if not isinstance(anchor_history, list) or not isinstance(current_history, list):
        errors.append("runtime state run_history must remain lists")
    else:
        anchor_run_ids = [
            row.get("run_id") if isinstance(row, dict) else None for row in anchor_history
        ]
        current_run_ids = [
            row.get("run_id") if isinstance(row, dict) else None for row in current_history
        ]
        if (
            not all(isinstance(value, str) and value for value in anchor_run_ids)
            or len(set(anchor_run_ids)) != len(anchor_run_ids)
            or not all(isinstance(value, str) and value for value in current_run_ids)
            or len(set(current_run_ids)) != len(current_run_ids)
        ):
            errors.append("runtime state run_history must contain unique string run_id values")
        if _canonical_json(current_history[: len(anchor_history)]) != _canonical_json(anchor_history):
            errors.append("runtime state run_history rewrote anchored history")

        slices_text, slices_error = committed_text(
            root, anchor_commit, SLICE_ENTRY_STABLE_SURFACE
        )
        if slices_error:
            errors.append(slices_error)
        elif slices_text is not None:
            slice_rows, slice_errors = _slice_inventory(
                slices_text, f"anchored {SLICE_ENTRY_STABLE_SURFACE}"
            )
            errors.extend(slice_errors)
            slice_row = slice_rows.get(slice_id)
            if slice_row is None:
                errors.append(f"anchored runtime state cannot resolve slice row: {slice_id}")
            else:
                run_id = slice_row.get("run_id")
                matching_runs = [
                    row
                    for row in anchor_history
                    if isinstance(row, dict) and row.get("run_id") == run_id
                ]
                if not isinstance(run_id, str) or not run_id or len(matching_runs) != 1:
                    errors.append(
                        f"anchored runtime state must contain one selected run record for {slice_id}"
                    )
                else:
                    run = matching_runs[0]
                    expected_terminal = {
                        "slice": slice_id,
                        "run_id": run_id,
                        "ship_branch": slice_row.get("ship_branch"),
                        "ship_commit": slice_row.get("ship_commit"),
                        "integration_base_commit": slice_row.get("integration_base_commit"),
                    }
                    for key, expected in expected_terminal.items():
                        if run.get(key) != expected:
                            errors.append(
                                f"anchored selected run record {key} mismatch: "
                                f"{run.get(key)!r} != {expected!r}"
                            )
                    if not isinstance(run.get("completed_at"), str) or not run["completed_at"]:
                        errors.append("anchored selected run record lacks completed_at")

    anchor_v1_complete = anchor_state.get("v1_complete")
    current_v1_complete = current_state.get("v1_complete")
    if type(anchor_v1_complete) is not bool or type(current_v1_complete) is not bool:
        errors.append("runtime state v1_complete must remain boolean")
    elif anchor_v1_complete and not current_v1_complete:
        errors.append("runtime state v1_complete regressed after receipt")

    anchor_event_id = anchor_state.get("last_event_id")
    current_event_id = current_state.get("last_event_id")
    if type(anchor_event_id) is not int or type(current_event_id) is not int:
        errors.append("runtime state last_event_id must remain an integer")
    elif current_event_id < anchor_event_id:
        errors.append("runtime state last_event_id regressed after receipt")
    events_text, events_error = committed_text(
        root, continuation_commit, "ops/autonomy/events.jsonl"
    )
    if events_error:
        errors.append(events_error)
    elif events_text is not None:
        maximum, event_errors = _event_log_max_id(
            events_text, "continuation ops/autonomy/events.jsonl"
        )
        errors.extend(event_errors)
        if maximum is not None and current_event_id != maximum:
            errors.append(
                "runtime state last_event_id does not match the continuation event-log maximum"
            )
    return errors


def _validate_anchored_surface_history(
    root: Path,
    *,
    slice_id: str,
    path: str,
    retention: Any,
    receipt_commit: str,
    continuation_commit: str,
    transitions: list[tuple[str, str]],
) -> list[str]:
    """Validate the anchor and every first-parent transition independently.

    Endpoint-only comparison is insufficient for durable logs/state: a commit
    can rewrite history and a later commit can restore the final bytes. Each
    adjacent transition is therefore checked from its actual parent object.
    """

    errors: list[str] = []
    anchor_entry, anchor_error = git_tree_entry(root, receipt_commit, path)
    if anchor_error:
        return [anchor_error]
    errors.extend(
        _validate_anchored_surface_retention(
            root,
            slice_id=slice_id,
            path=path,
            retention=retention,
            anchor_commit=receipt_commit,
            continuation_commit=receipt_commit,
            anchor_entry=anchor_entry,
            current_entry=anchor_entry,
        )
    )
    for parent, child in transitions:
        parent_entry, parent_error = git_tree_entry(root, parent, path)
        child_entry, child_error = git_tree_entry(root, child, path)
        if parent_error:
            errors.append(parent_error)
        if child_error:
            errors.append(child_error)
        if parent_error or child_error:
            continue
        transition_errors = _validate_anchored_surface_retention(
            root,
            slice_id=slice_id,
            path=path,
            retention=retention,
            anchor_commit=parent,
            continuation_commit=child,
            anchor_entry=parent_entry,
            current_entry=child_entry,
        )
        errors.extend(f"{parent}..{child}: {error}" for error in transition_errors)
    return errors


def _validate_supersedes_chain(
    root: Path,
    *,
    supersedes: Any,
    identity: dict[str, Any],
    child_verification_commit: str,
    continuation_commit: str,
    seen_paths: set[str],
    pending_successor_path: str | None = None,
    pending_successor_receipt: dict[str, Any] | None = None,
) -> list[str]:
    """Validate immutable predecessor receipts as one slice identity chain."""

    errors: list[str] = []
    if not isinstance(supersedes, dict) or set(supersedes) != {
        "path",
        "anchor_commit",
        "tree_entry",
    }:
        return ["supersedes_receipt must have exact path/anchor_commit/tree_entry fields"]
    prior_path = _evidence_artifact_path(supersedes.get("path"))
    prior_anchor = supersedes.get("anchor_commit")
    if prior_path is None:
        return [
            "supersedes_receipt must name a JSON file directly under docs/evidence/"
        ]
    if prior_path in seen_paths:
        return [f"supersedes_receipt chain repeats a receipt path: {prior_path}"]
    seen_paths.add(prior_path)
    if not isinstance(prior_anchor, str) or re.fullmatch(
        r"[0-9a-fA-F]{40,64}", prior_anchor
    ) is None:
        return ["supersedes_receipt anchor_commit must be a full commit id"]

    actual_prior_anchor, prior_anchor_error = _receipt_anchor_commit(
        root, continuation_commit, prior_path
    )
    if prior_anchor_error:
        errors.append(prior_anchor_error)
        return errors
    if actual_prior_anchor != prior_anchor:
        errors.append("supersedes_receipt anchor_commit mismatch")
        return errors
    prior_entry, prior_entry_error = git_tree_entry(root, prior_anchor, prior_path)
    current_prior_entry, current_prior_error = git_tree_entry(
        root, continuation_commit, prior_path
    )
    if prior_entry_error:
        errors.append(prior_entry_error)
    if current_prior_error:
        errors.append(current_prior_error)
    if not _is_regular_blob(prior_entry):
        errors.append("superseded receipt must be a regular 100644 blob")
    if supersedes.get("tree_entry") != prior_entry:
        errors.append("supersedes_receipt tree_entry mismatch")
    if current_prior_entry != prior_entry:
        errors.append("superseded receipt changed after its anchor")

    prior_text, prior_read_error = committed_text(root, prior_anchor, prior_path)
    if prior_read_error or prior_text is None:
        errors.append(prior_read_error or f"could not read superseded receipt: {prior_path}")
        return errors
    prior_receipt, prior_parse_error = _parse_json_object(
        prior_text, f"superseded receipt {prior_path}"
    )
    if prior_parse_error or prior_receipt is None:
        errors.append(prior_parse_error or f"could not parse superseded receipt: {prior_path}")
        return errors
    if prior_receipt.get("schema_version") != ANCHORED_RECEIPT_SCHEMA_VERSION:
        errors.append("supersedes_receipt chain may contain only anchored v2 receipts")
    for field, expected in identity.items():
        if prior_receipt.get(field) != expected:
            errors.append(
                f"superseded receipt changed slice identity field {field}: "
                f"{prior_receipt.get(field)!r} != {expected!r}"
            )

    chain_transitions, chain_transition_errors = _first_parent_transitions(
        root, prior_anchor, continuation_commit
    )
    errors.extend(chain_transition_errors)
    for field, path_validator, label in (
        ("review_artifacts", _review_artifact_path, "superseded review artifact"),
        ("command_evidence", _evidence_artifact_path, "superseded command evidence"),
    ):
        rows = prior_receipt.get(field)
        if not isinstance(rows, list):
            errors.append(f"superseded receipt {field} must be a list")
            continue
        for index, row in enumerate(rows, start=1):
            if not isinstance(row, dict):
                errors.append(f"superseded receipt {field} row {index} is not an object")
                continue
            artifact_path = path_validator(row.get("path"))
            expected_entry = row.get("tree_entry")
            if artifact_path is None or not _is_regular_blob(expected_entry):
                errors.append(
                    f"superseded receipt {field} row {index} has an invalid path/tree entry"
                )
                continue
            current_entry, current_error = git_tree_entry(
                root, continuation_commit, artifact_path
            )
            if current_error:
                errors.append(current_error)
            elif current_entry != expected_entry:
                errors.append(f"{label} changed after its receipt anchor: {artifact_path}")
            errors.extend(
                _validate_immutable_path_history(
                    root,
                    path=artifact_path,
                    expected_entry=expected_entry,
                    transitions=chain_transitions,
                    label=label,
                )
            )

    registry_receipts, registry_discovery_errors = _discover_anchored_v2_receipts(
        root, continuation_commit, identity
    )
    errors.extend(registry_discovery_errors)
    if pending_successor_path is not None and pending_successor_receipt is not None:
        if pending_successor_path in registry_receipts:
            errors.append(
                f"pending successor path already exists in the durable registry: {pending_successor_path}"
            )
        else:
            registry_receipts[pending_successor_path] = {
                "anchor_commit": None,
                "receipt": pending_successor_receipt,
                "tree_entry": None,
            }
    expected_registry_tip = pending_successor_path
    if expected_registry_tip is None:
        configured_text, configured_error = committed_text(
            root, continuation_commit, DEFAULT_SLICES_PATH
        )
        if configured_error:
            errors.append(configured_error)
        elif configured_text is not None:
            configured_rows, configured_errors = _slice_inventory(
                configured_text, f"successor-tip slices at {continuation_commit}"
            )
            errors.extend(configured_errors)
            configured_row = configured_rows.get(str(identity.get("slice") or ""))
            if configured_row is not None:
                expected_registry_tip = _evidence_artifact_path(
                    configured_row.get("integration_receipt")
                )
    _, registry_topology_errors = _receipt_registry_topology(
        registry_receipts, expected_tip=expected_registry_tip
    )
    errors.extend(registry_topology_errors)

    direct_successors: list[tuple[str, dict[str, Any]]] = []
    for successor_path, successor_record in registry_receipts.items():
        payload = successor_record.get("receipt")
        supersedes_payload = (
            payload.get("supersedes_receipt") if isinstance(payload, dict) else None
        )
        if (
            isinstance(supersedes_payload, dict)
            and _evidence_artifact_path(supersedes_payload.get("path")) == prior_path
        ):
            direct_successors.append((successor_path, successor_record))
    direct_transitions: list[tuple[str, str]] = []
    recertification_transition: tuple[str, str] | None = None
    retention_tip = prior_anchor
    if len(direct_successors) != 1:
        errors.append(
            f"superseded receipt must have exactly one direct successor; found "
            f"{[path for path, _ in direct_successors]}"
        )
    else:
        successor_path, direct_successor = direct_successors[0]
        successor_payload = direct_successor.get("receipt")
        successor_verification = (
            successor_payload.get("verification_commit")
            if isinstance(successor_payload, dict)
            else None
        )
        successor_anchor = direct_successor.get("anchor_commit")
        if not isinstance(successor_verification, str) or re.fullmatch(
            r"[0-9a-fA-F]{40,64}", successor_verification
        ) is None:
            errors.append("direct successor verification_commit must be a full commit id")
        else:
            retention_tip = (
                successor_anchor if isinstance(successor_anchor, str) else successor_verification
            )
            direct_transitions, direct_transition_errors = _first_parent_transitions(
                root, prior_anchor, retention_tip
            )
            errors.extend(direct_transition_errors)
            freeze_edges = [
                edge for edge in direct_transitions if edge[1] == successor_verification
            ]
            if len(freeze_edges) != 1:
                errors.append(
                    "direct successor history must contain exactly one final recertification "
                    f"transition into {successor_verification}"
                )
            else:
                recertification_transition = freeze_edges[0]
                freeze_index = direct_transitions.index(freeze_edges[0])
                errors.extend(
                    _validate_successor_freeze_pointer(
                        root,
                        slice_id=str(identity.get("slice") or ""),
                        parent_commit=freeze_edges[0][0],
                        verification_commit=successor_verification,
                        predecessor_path=prior_path,
                        successor_path=successor_path,
                        successor_receipt=successor_payload,
                        pre_freeze_transitions=direct_transitions[:freeze_index],
                        post_freeze_transitions=direct_transitions[freeze_index + 1 :],
                    )
                )
    surface_rows = prior_receipt.get("surfaces")
    if not isinstance(surface_rows, list):
        errors.append("superseded receipt surfaces must be a list")
    else:
        for index, row in enumerate(surface_rows, start=1):
            if not isinstance(row, dict):
                errors.append(f"superseded receipt surface row {index} is not an object")
                continue
            surface_path = _safe_repo_path(row.get("path"))
            if surface_path is None:
                errors.append(f"superseded receipt surface row {index} has an unsafe path")
                continue
            retention = row.get("retention")
            # The successor may recertify an immutable product/control surface
            # at its exact verification boundary. Durable logs and runtime state
            # are never resettable, including at that boundary. The slice row is
            # handled separately there by _validate_successor_freeze_pointer,
            # which permits only the receipt/review pointer transition.
            surface_transitions = direct_transitions
            if retention in {"immutable", "slice_entry_stable"}:
                surface_transitions = [
                    edge
                    for edge in direct_transitions
                    if edge != recertification_transition
                ]
            errors.extend(
                _validate_anchored_surface_history(
                    root,
                    slice_id=str(identity.get("slice") or ""),
                    path=surface_path,
                    retention=retention,
                    receipt_commit=prior_anchor,
                    continuation_commit=retention_tip,
                    transitions=surface_transitions,
                )
            )

    prior_verification = prior_receipt.get("verification_commit")
    if not isinstance(prior_verification, str) or re.fullmatch(
        r"[0-9a-fA-F]{40,64}", prior_verification
    ) is None:
        errors.append("superseded receipt verification_commit must be a full commit id")
    else:
        parent_row = _run_git(root, "rev-list", "--parents", "-n", "1", prior_anchor)
        if parent_row.returncode != 0:
            errors.append(
                f"could not inspect superseded receipt parent: {parent_row.stderr.strip()}"
            )
        elif parent_row.stdout.strip().split()[1:] != [prior_verification]:
            errors.append(
                "superseded receipt anchor must be a single-parent audit commit whose "
                "parent is its verification_commit"
            )
        prior_before_child, ancestry_error = _is_ancestor(
            root, prior_anchor, child_verification_commit
        )
        if ancestry_error:
            errors.append(ancestry_error)
        elif not prior_before_child:
            errors.append(
                "superseded receipt anchor is not ancestral to successor verification_commit"
            )
        prior_slices_text, prior_slices_error = committed_text(
            root, prior_anchor, DEFAULT_SLICES_PATH
        )
        if prior_slices_error or prior_slices_text is None:
            errors.append(
                prior_slices_error
                or f"could not read predecessor slice state at {prior_anchor}"
            )
        else:
            prior_slices, prior_slice_errors = _slice_inventory(
                prior_slices_text, f"predecessor slices at {prior_anchor}"
            )
            errors.extend(prior_slice_errors)
            prior_slice = prior_slices.get(str(identity.get("slice") or ""))
            if prior_slice is None:
                errors.append("superseded receipt anchor does not contain its slice row")
            elif prior_slice.get("integration_receipt") != prior_path:
                errors.append(
                    "superseded receipt was not the configured receipt at its own anchor"
                )
            else:
                prior_changed_paths, paths_error = _changed_paths(
                    root,
                    str(identity.get("merge_base") or ""),
                    str(identity.get("ship_commit") or ""),
                )
                if paths_error:
                    errors.append(paths_error)
                else:
                    prior_surface_entries, surface_errors = _surface_entries(
                        root,
                        str(identity.get("merge_base") or ""),
                        str(identity.get("ship_commit") or ""),
                        prior_anchor,
                        prior_changed_paths,
                    )
                    errors.extend(surface_errors)
                    predecessor_errors = _validate_receipt(
                        root,
                        receipt_path=prior_path,
                        receipt=prior_receipt,
                        slice_=prior_slice,
                        continuation_ref="main",
                        continuation_commit=prior_anchor,
                        receipt_commit=prior_anchor,
                        ship_branch=str(identity.get("ship_branch") or ""),
                        ship_commit=str(identity.get("ship_commit") or ""),
                        merge_base=str(identity.get("merge_base") or ""),
                        changed_paths=prior_changed_paths,
                        surface_entries=prior_surface_entries,
                        current_surface_entries=prior_surface_entries,
                        supersedes_seen_paths=seen_paths,
                        supersedes_chain_tip_commit=continuation_commit,
                        pending_successor_path=pending_successor_path,
                        pending_successor_receipt=pending_successor_receipt,
                    )
                    errors.extend(
                        f"superseded receipt {prior_path}: {error}"
                        for error in predecessor_errors
                    )
    return errors


def _validate_immutable_path_history(
    root: Path,
    *,
    path: str,
    expected_entry: TreeEntry,
    transitions: list[tuple[str, str]],
    label: str,
) -> list[str]:
    errors: list[str] = []
    prior_expected = expected_entry
    for parent, child in transitions:
        parent_entry, parent_error = git_tree_entry(root, parent, path)
        child_entry, child_error = git_tree_entry(root, child, path)
        if parent_error:
            errors.append(parent_error)
        if child_error:
            errors.append(child_error)
        if parent_error or child_error:
            continue
        if parent_entry != prior_expected or child_entry != prior_expected:
            errors.append(
                f"{label} changed on first-parent transition {parent}..{child}: {path}"
            )
    return errors


def _validate_successor_freeze_pointer(
    root: Path,
    *,
    slice_id: str,
    parent_commit: str,
    verification_commit: str,
    predecessor_path: str,
    successor_path: str,
    successor_receipt: dict[str, Any],
    pre_freeze_transitions: list[tuple[str, str]],
    post_freeze_transitions: list[tuple[str, str]],
) -> list[str]:
    """Require the registry pointer switch at the exact recertification boundary."""

    errors: list[str] = []
    parent_text, parent_error = committed_text(
        root, parent_commit, SLICE_ENTRY_STABLE_SURFACE
    )
    child_text, child_error = committed_text(
        root, verification_commit, SLICE_ENTRY_STABLE_SURFACE
    )
    if parent_error:
        errors.append(parent_error)
    if child_error:
        errors.append(child_error)
    if errors or parent_text is None or child_text is None:
        return errors
    parent_rows, parent_errors = _slice_inventory(
        parent_text, f"predecessor pointer state at {parent_commit}"
    )
    child_rows, child_errors = _slice_inventory(
        child_text, f"successor pointer state at {verification_commit}"
    )
    errors.extend(parent_errors)
    errors.extend(child_errors)
    parent_row = parent_rows.get(slice_id)
    child_row = child_rows.get(slice_id)
    if parent_row is None or child_row is None:
        errors.append(f"successor freeze must retain exactly one {slice_id} row")
        return errors
    if parent_row.get("integration_receipt") != predecessor_path:
        errors.append(
            "successor receipt pointer changed before the exact verification commit: "
            f"{parent_row.get('integration_receipt')!r} != {predecessor_path!r}"
        )
    if child_row.get("integration_receipt") != successor_path:
        errors.append(
            "successor verification commit does not configure its new receipt path: "
            f"{child_row.get('integration_receipt')!r} != {successor_path!r}"
        )
    pointer_fields = {"integration_receipt", "integration_review_artifacts"}
    parent_stable = {
        key: value for key, value in parent_row.items() if key not in pointer_fields
    }
    child_stable = {
        key: value for key, value in child_row.items() if key not in pointer_fields
    }
    if _canonical_json(parent_stable) != _canonical_json(child_stable):
        errors.append("successor freeze changed non-pointer fields in the historical slice row")
    review_rows = successor_receipt.get("review_artifacts")
    expected_reviews = (
        [row.get("path") for row in review_rows if isinstance(row, dict)]
        if isinstance(review_rows, list)
        else None
    )
    if child_row.get("integration_review_artifacts") != expected_reviews:
        errors.append(
            "successor freeze integration_review_artifacts do not exactly match its receipt"
        )

    for transitions, expected_path, label in (
        (pre_freeze_transitions, predecessor_path, "before"),
        (post_freeze_transitions, successor_path, "after"),
    ):
        for transition_parent, transition_child in transitions:
            for commit in (transition_parent, transition_child):
                text, read_error = committed_text(
                    root, commit, SLICE_ENTRY_STABLE_SURFACE
                )
                if read_error or text is None:
                    errors.append(read_error or f"could not read slice pointer state at {commit}")
                    continue
                rows, row_errors = _slice_inventory(
                    text, f"successor pointer history at {commit}"
                )
                errors.extend(row_errors)
                row = rows.get(slice_id)
                if row is None or row.get("integration_receipt") != expected_path:
                    errors.append(
                        f"successor receipt pointer changed {label} the exact verification "
                        f"boundary at {commit}"
                    )
    anchored_required_or_complete = {
        row_id
        for row_id, row in parent_rows.items()
        if row.get("required") is True or row.get("status") == "complete"
    }
    missing = sorted(anchored_required_or_complete - set(child_rows))
    if missing:
        errors.append(
            "successor freeze removed required/completed slices: " + ", ".join(missing)
        )
    return errors


def _validate_receipt(
    root: Path,
    *,
    receipt_path: str,
    receipt: dict[str, Any],
    slice_: dict[str, Any],
    continuation_ref: str,
    continuation_commit: str,
    receipt_commit: str,
    ship_branch: str,
    ship_commit: str,
    merge_base: str,
    changed_paths: list[str],
    surface_entries: dict[str, dict[str, TreeEntry]],
    current_surface_entries: dict[str, dict[str, TreeEntry]],
    supersedes_seen_paths: set[str] | None = None,
    supersedes_chain_tip_commit: str | None = None,
    pending_successor_path: str | None = None,
    pending_successor_receipt: dict[str, Any] | None = None,
) -> list[str]:
    errors: list[str] = []
    slice_id = str(slice_.get("id") or "")

    schema_version = receipt.get("schema_version")
    if schema_version not in SUPPORTED_RECEIPT_SCHEMA_VERSIONS:
        errors.append(f"unsupported integration receipt schema_version: {schema_version!r}")
    verification_commit = continuation_commit
    first_parent_transitions: list[tuple[str, str]] = []
    if schema_version == ANCHORED_RECEIPT_SCHEMA_VERSION:
        schema_text, schema_read_error = committed_text(
            root,
            receipt_commit,
            "ops/autonomy/schemas/slice_integration_receipt_v2.schema.json",
        )
        if schema_read_error or schema_text is None:
            errors.append(schema_read_error or "anchored receipt schema is not committed")
        else:
            schema_payload, schema_parse_error = _strict_json_loads(
                schema_text, "anchored receipt JSON schema"
            )
            if schema_parse_error:
                errors.append(schema_parse_error)
            else:
                try:
                    from jsonschema import Draft202012Validator

                    schema_errors = sorted(
                        Draft202012Validator(schema_payload).iter_errors(receipt),
                        key=lambda item: list(item.absolute_path),
                    )
                    errors.extend(
                        "anchored receipt schema violation at "
                        + ("/".join(str(part) for part in item.absolute_path) or "<root>")
                        + f": {item.message}"
                        for item in schema_errors
                    )
                except Exception as exc:
                    errors.append(f"could not validate anchored receipt schema: {exc}")
        required_fields = {
            "schema_version",
            "slice",
            "continuation_ref",
            "ship_branch",
            "ship_commit",
            "merge_base",
            "verification_commit",
            "command_evidence_trust_boundary",
            "audit_delta",
            "surfaces",
            "verification_commands",
            "review_artifacts",
            "command_evidence",
        }
        optional_fields = {"supersedes_receipt"}
        unknown_fields = sorted(set(receipt) - required_fields - optional_fields)
        missing_fields = sorted(required_fields - set(receipt))
        if unknown_fields:
            errors.append(f"anchored receipt has unknown fields: {unknown_fields}")
        if missing_fields:
            errors.append(f"anchored receipt is missing fields: {missing_fields}")
        if receipt.get("command_evidence_trust_boundary") != COMMAND_EVIDENCE_TRUST_BOUNDARY:
            errors.append(
                "anchored receipt must explicitly label command evidence as structurally/hash-bound "
                "self-attestation without cryptographic runner proof"
            )
        verification_value = receipt.get("verification_commit")
        if not isinstance(verification_value, str) or re.fullmatch(
            r"[0-9a-fA-F]{40,64}", verification_value
        ) is None:
            errors.append("anchored receipt verification_commit must be a full commit id")
        else:
            resolved_verification, verification_error = resolve_commit(root, verification_value)
            if verification_error or resolved_verification != verification_value:
                errors.append(
                    verification_error
                    or "anchored receipt verification_commit did not resolve to itself"
                )
            else:
                verification_commit = verification_value
        anchor_parents = _run_git(root, "rev-list", "--parents", "-n", "1", receipt_commit)
        if anchor_parents.returncode != 0:
            errors.append(f"could not inspect anchored receipt parent: {anchor_parents.stderr.strip()}")
        else:
            parent_ids = anchor_parents.stdout.strip().split()[1:]
            if parent_ids != [verification_commit]:
                errors.append(
                    "anchored receipt must be created by a single-parent audit commit "
                    "whose parent is verification_commit"
                )

        first_parent_transitions, transition_errors = _first_parent_transitions(
            root, receipt_commit, continuation_commit
        )
        errors.extend(transition_errors)

        receipt_entry, receipt_entry_error = git_tree_entry(root, receipt_commit, receipt_path)
        if receipt_entry_error:
            errors.append(receipt_entry_error)
        elif not _is_regular_blob(receipt_entry):
            errors.append("anchored integration receipt must be a regular 100644 blob")

        _, registry_errors = _validate_receipt_registry(
            root,
            tip_commit=receipt_commit,
            current_receipt_path=receipt_path,
            identity={
                "slice": slice_id,
                "ship_branch": ship_branch,
                "ship_commit": ship_commit,
                "merge_base": merge_base,
            },
        )
        errors.extend(registry_errors)

        supersedes = receipt.get("supersedes_receipt")
        if supersedes is not None:
            errors.extend(
                _validate_supersedes_chain(
                    root,
                    supersedes=supersedes,
                    identity={
                        "slice": slice_id,
                        "ship_branch": ship_branch,
                        "ship_commit": ship_commit,
                        "merge_base": merge_base,
                    },
                    child_verification_commit=verification_commit,
                    continuation_commit=(
                        supersedes_chain_tip_commit or continuation_commit
                    ),
                    seen_paths=(
                        supersedes_seen_paths
                        if supersedes_seen_paths is not None
                        else {receipt_path}
                    ),
                    pending_successor_path=pending_successor_path,
                    pending_successor_receipt=pending_successor_receipt,
                )
            )

    expected_scalars = {
        "slice": slice_id,
        "continuation_ref": continuation_ref,
        "ship_branch": ship_branch,
        "ship_commit": ship_commit,
        "merge_base": merge_base,
    }
    for field, expected in expected_scalars.items():
        if receipt.get(field) != expected:
            errors.append(f"integration receipt {field} mismatch: {receipt.get(field)!r} != {expected!r}")

    surfaces = receipt.get("surfaces")
    if not isinstance(surfaces, list):
        errors.append("integration receipt surfaces must be a list")
        surfaces = []
    receipt_paths: list[str] = []
    seen_paths: set[str] = set()
    for index, row in enumerate(surfaces, start=1):
        if not isinstance(row, dict):
            errors.append(f"integration receipt surface {index} is not an object")
            continue
        path = _safe_repo_path(row.get("path"))
        if path is None:
            errors.append(f"integration receipt surface {index} has an unsafe path")
            continue
        receipt_paths.append(path)
        if path in seen_paths:
            errors.append(f"integration receipt repeats ship-changed path: {path}")
        seen_paths.add(path)
        actual = surface_entries.get(path)
        if actual is None:
            errors.append(f"integration receipt contains a non-ship path: {path}")
            continue
        if row.get("ship_entry") != actual["ship"]:
            errors.append(f"integration receipt ship tree entry mismatch: {path}")
        if row.get("continuation_entry") != actual["continuation"]:
            errors.append(f"integration receipt continuation tree entry mismatch: {path}")

        if schema_version == ANCHORED_RECEIPT_SCHEMA_VERSION:
            required_surface_fields = {
                "path",
                "disposition",
                "retention",
                "ship_entry",
                "continuation_entry",
            }
            allowed_surface_fields = required_surface_fields | {"reason"}
            if set(row) - allowed_surface_fields or required_surface_fields - set(row):
                errors.append(f"anchored receipt surface has invalid fields: {path}")
            if actual["ship"] is not None and actual["continuation"] is None:
                errors.append(f"anchored receipt may not retire a ship surface: {path}")
            if actual["ship"] != actual["base"] and actual["continuation"] == actual["base"]:
                errors.append(f"anchored receipt surface reverted to pre-ship state: {path}")
            expected_retention = _required_surface_retention(path)
            if row.get("retention") != expected_retention:
                errors.append(
                    "anchored receipt surface retention mismatch: "
                    f"{path}: {row.get('retention')!r} != {expected_retention!r}"
                )

        disposition = row.get("disposition")
        reason = row.get("reason")
        if disposition == "identical":
            if actual["ship"] != actual["continuation"]:
                errors.append(f"identical receipt surface differs on continuation: {path}")
        elif disposition == "evolved":
            if actual["ship"] == actual["continuation"]:
                errors.append(f"evolved receipt surface is actually identical: {path}")
            if actual["continuation"] is None:
                errors.append(f"evolved receipt surface is absent; use retired: {path}")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"evolved receipt surface requires a reason: {path}")
        elif disposition == "retired":
            if actual["ship"] is None or actual["continuation"] is not None:
                errors.append(f"retired receipt surface must be present on ship and absent on continuation: {path}")
            if not isinstance(reason, str) or not reason.strip():
                errors.append(f"retired receipt surface requires a reason: {path}")
        else:
            errors.append(f"integration receipt surface has invalid disposition: {path}: {disposition!r}")

        if schema_version == ANCHORED_RECEIPT_SCHEMA_VERSION:
            current = current_surface_entries.get(path)
            if current is None:
                errors.append(f"anchored receipt is missing current surface state: {path}")
            else:
                errors.extend(
                    _validate_anchored_surface_history(
                        root,
                        slice_id=slice_id,
                        path=path,
                        retention=row.get("retention"),
                        receipt_commit=receipt_commit,
                        continuation_commit=continuation_commit,
                        transitions=first_parent_transitions,
                    )
                )

    if receipt_paths != changed_paths:
        missing = sorted(set(changed_paths) - set(receipt_paths))
        extra = sorted(set(receipt_paths) - set(changed_paths))
        errors.append(
            "integration receipt surfaces are not the exhaustive sorted ship-change set"
            f"; missing={missing}; extra={extra}"
        )

    configured_commands = slice_.get("acceptance")
    if not isinstance(configured_commands, list) or not configured_commands or not all(
        isinstance(command, str) and command.strip() for command in configured_commands
    ):
        errors.append("completed reconciled slice must have a non-empty committed acceptance contract")
        configured_commands = []
    verification_rows = receipt.get("verification_commands")
    if not isinstance(verification_rows, list):
        errors.append("integration receipt verification_commands must be a list")
        verification_rows = []
    receipt_commands: list[str] = []
    verification_evidence_bindings: list[tuple[str, str]] = []
    for index, row in enumerate(verification_rows, start=1):
        if not isinstance(row, dict):
            errors.append(f"integration receipt verification row {index} is not an object")
            continue
        if schema_version == ANCHORED_RECEIPT_SCHEMA_VERSION and set(row) != {
            "command",
            "evidence_path",
            "exit_code",
            "status",
        }:
            errors.append(f"anchored receipt verification row {index} has invalid fields")
        command = row.get("command")
        exit_code = row.get("exit_code")
        if not isinstance(command, str) or not command.strip():
            errors.append(f"integration receipt verification row {index} has no command")
            continue
        receipt_commands.append(command)
        if schema_version == ANCHORED_RECEIPT_SCHEMA_VERSION:
            evidence_path = _safe_repo_path(row.get("evidence_path"))
            if evidence_path is None:
                errors.append(
                    f"anchored receipt verification row {index} has unsafe evidence_path"
                )
            else:
                verification_evidence_bindings.append((command, evidence_path))
        if not isinstance(exit_code, int) or isinstance(exit_code, bool) or exit_code != 0:
            errors.append(f"integration receipt verification command did not pass: {command}")
        status = row.get("status")
        if status is not None and status not in {"ok", "pass", "passed"}:
            errors.append(f"integration receipt verification command has non-passing status: {command}: {status!r}")
    if receipt_commands != configured_commands:
        errors.append(
            "integration receipt verification commands do not exactly match committed slice acceptance"
        )

    configured_reviews = slice_.get("review_artifacts")
    if schema_version == ANCHORED_RECEIPT_SCHEMA_VERSION:
        configured_reviews = slice_.get(
            "integration_review_artifacts", configured_reviews
        )
    review_path_validator = (
        _review_artifact_path
        if schema_version == ANCHORED_RECEIPT_SCHEMA_VERSION
        else _safe_repo_path
    )
    if not isinstance(configured_reviews, list) or not configured_reviews or not all(
        isinstance(path, str) and review_path_validator(path) is not None
        for path in configured_reviews
    ):
        errors.append("reconciled slice must configure at least one committed autonomous review artifact")
        configured_reviews = []
    review_rows = receipt.get("review_artifacts")
    if not isinstance(review_rows, list):
        errors.append("integration receipt review_artifacts must be a list")
        review_rows = []
    receipt_reviews: list[str] = []
    review_successful_commands: set[str] = set()
    referenced_evidence_paths: list[str] = []
    for index, row in enumerate(review_rows, start=1):
        if not isinstance(row, dict):
            errors.append(f"integration receipt review row {index} is not an object")
            continue
        if schema_version == ANCHORED_RECEIPT_SCHEMA_VERSION and set(row) != {
            "path",
            "tree_entry",
        }:
            errors.append(f"anchored receipt review row {index} has invalid fields")
        path = (
            _review_artifact_path(row.get("path"))
            if schema_version == ANCHORED_RECEIPT_SCHEMA_VERSION
            else _safe_repo_path(row.get("path"))
        )
        if path is None:
            errors.append(
                f"integration receipt review row {index} must be a Markdown file directly "
                "under docs/reviews/"
            )
            continue
        receipt_reviews.append(path)
        actual_entry, entry_error = git_tree_entry(root, receipt_commit, path)
        if entry_error:
            errors.append(entry_error)
            continue
        if actual_entry is None:
            errors.append(f"integration receipt review artifact is absent: {path}")
            continue
        if schema_version == ANCHORED_RECEIPT_SCHEMA_VERSION and not _is_regular_blob(
            actual_entry
        ):
            errors.append(
                f"anchored review artifact must be a regular 100644 blob: {path}"
            )
        if row.get("tree_entry") != actual_entry:
            errors.append(f"integration receipt review tree entry mismatch: {path}")
        successful, review_errors, evidence_paths = _validate_committed_review(
            root, receipt_commit, path
        )
        review_successful_commands.update(successful)
        errors.extend(review_errors)
        for evidence_path in evidence_paths:
            if (
                schema_version == ANCHORED_RECEIPT_SCHEMA_VERSION
                and _evidence_artifact_path(evidence_path) is None
            ):
                errors.append(
                    "anchored review command evidence must be a JSON file directly under "
                    f"docs/evidence/: {evidence_path}"
                )
            if evidence_path not in referenced_evidence_paths:
                referenced_evidence_paths.append(evidence_path)
        if schema_version == ANCHORED_RECEIPT_SCHEMA_VERSION:
            current_entry, current_entry_error = git_tree_entry(root, continuation_commit, path)
            if current_entry_error:
                errors.append(current_entry_error)
            else:
                if not _is_regular_blob(current_entry):
                    errors.append(
                        f"anchored review artifact must be a regular 100644 blob: {path}"
                    )
                if current_entry != actual_entry:
                    errors.append(f"anchored review artifact changed after receipt: {path}")
            errors.extend(
                _validate_immutable_path_history(
                    root,
                    path=path,
                    expected_entry=actual_entry,
                    transitions=first_parent_transitions,
                    label="anchored review artifact",
                )
            )
    if receipt_reviews != configured_reviews:
        errors.append("integration receipt reviews do not exactly match committed slice review_artifacts")
    missing_review_commands = [
        command for command in configured_commands if command not in review_successful_commands
    ]
    if missing_review_commands:
        errors.append(
            "committed review command evidence does not cover slice acceptance: "
            + ", ".join(missing_review_commands)
        )

    if schema_version == ANCHORED_RECEIPT_SCHEMA_VERSION:
        evidence_rows = receipt.get("command_evidence")
        if not isinstance(evidence_rows, list) or not evidence_rows:
            errors.append("anchored receipt command_evidence must be a non-empty list")
            evidence_rows = []
        receipt_evidence_paths: list[str] = []
        anchored_successful_commands: set[str] = set()
        successful_commands_by_evidence: dict[str, set[str]] = {}
        for index, row in enumerate(evidence_rows, start=1):
            if not isinstance(row, dict) or set(row) != {"path", "tree_entry"}:
                errors.append(f"anchored receipt command evidence row {index} has invalid fields")
                continue
            path = _evidence_artifact_path(row.get("path"))
            if path is None:
                errors.append(
                    f"anchored receipt command evidence row {index} must be a JSON file "
                    "directly under docs/evidence/"
                )
                continue
            if path == receipt_path:
                errors.append("anchored receipt and command evidence paths must be distinct")
            receipt_evidence_paths.append(path)
            anchor_entry, anchor_error = git_tree_entry(root, receipt_commit, path)
            current_entry, current_error = git_tree_entry(root, continuation_commit, path)
            if anchor_error:
                errors.append(anchor_error)
            if current_error:
                errors.append(current_error)
            if anchor_entry is None or anchor_entry.get("type") != "blob":
                errors.append(f"anchored command evidence is not a committed blob: {path}")
                continue
            if not _is_regular_blob(anchor_entry):
                errors.append(
                    f"anchored command evidence must be a regular 100644 blob: {path}"
                )
            if not _is_regular_blob(current_entry):
                errors.append(
                    f"anchored command evidence must be a regular 100644 blob: {path}"
                )
            if row.get("tree_entry") != anchor_entry:
                errors.append(f"anchored command evidence tree entry mismatch: {path}")
            if current_entry != anchor_entry:
                errors.append(f"anchored command evidence changed after receipt: {path}")
            errors.extend(
                _validate_immutable_path_history(
                    root,
                    path=path,
                    expected_entry=anchor_entry,
                    transitions=first_parent_transitions,
                    label="anchored command evidence",
                )
            )
            evidence_text, evidence_error = committed_text(root, receipt_commit, path)
            if evidence_error or evidence_text is None:
                errors.append(evidence_error or f"could not read anchored command evidence: {path}")
                continue
            evidence_payload, parse_error = _strict_json_loads(
                evidence_text, f"anchored command evidence {path}"
            )
            if parse_error:
                errors.append(parse_error)
                continue
            successful, evidence_errors = _validate_anchored_command_evidence(
                root,
                path=path,
                payload=evidence_payload,
                slice_id=slice_id,
                verification_commit=verification_commit,
                anchor_commit=receipt_commit,
                continuation_commit=continuation_commit,
            )
            anchored_successful_commands.update(successful)
            successful_commands_by_evidence[path] = successful
            errors.extend(evidence_errors)
        if receipt_evidence_paths != referenced_evidence_paths:
            errors.append(
                "anchored receipt command_evidence paths do not exactly match review references"
            )
        missing_anchored_commands = [
            command for command in configured_commands if command not in anchored_successful_commands
        ]
        if missing_anchored_commands:
            errors.append(
                "anchored command evidence does not cover slice acceptance: "
                + ", ".join(missing_anchored_commands)
            )
        for command, evidence_path in verification_evidence_bindings:
            if evidence_path not in receipt_evidence_paths:
                errors.append(
                    "anchored verification command references unlisted command evidence: "
                    f"{command}: {evidence_path}"
                )
            elif command not in successful_commands_by_evidence.get(evidence_path, set()):
                errors.append(
                    "anchored verification command is not proven by its command evidence: "
                    f"{command}: {evidence_path}"
                )

        audit_delta = receipt.get("audit_delta")
        audit_paths: list[str] = []
        if not isinstance(audit_delta, list) or not audit_delta:
            errors.append("anchored receipt audit_delta must be a non-empty list")
        else:
            for index, value in enumerate(audit_delta, start=1):
                audit_path = _safe_repo_path(value)
                if audit_path is None:
                    errors.append(
                        f"anchored receipt audit_delta row {index} has an unsafe path"
                    )
                    continue
                audit_paths.append(audit_path)
            if audit_paths != sorted(set(audit_paths)):
                errors.append("anchored receipt audit_delta paths must be unique and sorted")

        actual_audit_delta, audit_delta_error = _changed_paths(
            root, verification_commit, receipt_commit
        )
        if audit_delta_error:
            errors.append(audit_delta_error)
        else:
            if audit_paths != actual_audit_delta:
                errors.append(
                    "anchored receipt audit_delta does not exactly bind the verification-to-anchor "
                    f"change set; declared={audit_paths}; actual={actual_audit_delta}"
                )
            allowed_audit_paths = {
                receipt_path,
                *receipt_reviews,
                *receipt_evidence_paths,
            }
            forbidden_audit_paths = sorted(set(actual_audit_delta) - allowed_audit_paths)
            if forbidden_audit_paths:
                errors.append(
                    "anchored audit commit contains implementation/control changes: "
                    + ", ".join(forbidden_audit_paths)
                )
            if receipt_path not in actual_audit_delta:
                errors.append("anchored audit commit did not create the configured receipt path")

    if schema_version == ANCHORED_RECEIPT_SCHEMA_VERSION:
        if _evidence_artifact_path(receipt_path) != receipt_path:
            errors.append(
                "anchored integration receipt must be a JSON file directly under docs/evidence/"
            )
    elif not receipt_path.startswith("docs/evidence/"):
        errors.append("integration receipt must be committed under docs/evidence/")
    return errors


def verify_slice_integration(
    root: Path,
    slice_id: str,
    *,
    continuation_ref: str = "main",
    receipt_path: str | None = None,
) -> dict[str, Any]:
    """Classify one completed slice using only objects committed on Git refs."""

    root = root.resolve()
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}

    continuation_commit, continuation_error = _resolve_branch_commit(root, continuation_ref)
    if continuation_error or continuation_commit is None:
        return {
            "status": "error",
            "classification": None,
            "slice": slice_id,
            "continuation_ref": continuation_ref,
            "errors": [continuation_error or "could not resolve continuation ref"],
            "warnings": [],
            "checks": checks,
        }
    checks["continuation_commit"] = continuation_commit

    slices_text, slices_error = committed_text(root, continuation_commit, DEFAULT_SLICES_PATH)
    if slices_error or slices_text is None:
        errors.append(slices_error or "could not read committed slices")
        slice_rows: dict[str, dict[str, Any]] = {}
    else:
        slice_rows, slice_errors = _slice_inventory(
            slices_text, "committed continuation slices file"
        )
        errors.extend(slice_errors)
    slice_ = slice_rows.get(slice_id)
    if slice_ is None:
        errors.append(f"slice is absent from committed continuation state: {slice_id}")
    elif slice_.get("status") != "complete":
        errors.append(f"slice is not complete on committed continuation state: {slice_id}")
    if errors or slice_ is None:
        return {
            "status": "error",
            "classification": None,
            "slice": slice_id,
            "continuation_ref": continuation_ref,
            "errors": errors,
            "warnings": warnings,
            "checks": checks,
        }

    committed_configured_receipt: str | None = None
    for field in ("integration_receipt", "integration_evidence"):
        candidate = slice_.get(field)
        if isinstance(candidate, str) and candidate:
            committed_configured_receipt = candidate
            break
    if receipt_path is not None and receipt_path != committed_configured_receipt:
        errors.append(
            "receipt_path override differs from the committed configured integration receipt: "
            f"{receipt_path!r} != {committed_configured_receipt!r}"
        )
        return {
            "status": "error",
            "classification": None,
            "slice": slice_id,
            "continuation_ref": continuation_ref,
            "errors": errors,
            "warnings": warnings,
            "checks": checks,
        }
    configured_receipt = committed_configured_receipt or receipt_path
    checks["receipt_path"] = configured_receipt

    ship_branch = slice_.get("ship_branch")
    ship_commit = slice_.get("ship_commit")
    integration_base_commit = slice_.get("integration_base_commit")
    if not isinstance(ship_branch, str) or not ship_branch or ship_branch.startswith("-"):
        errors.append("completed slice has no safe ship_branch")
    if not isinstance(ship_commit, str) or not re.fullmatch(r"[0-9a-fA-F]{40,64}", ship_commit):
        errors.append("completed slice has no full hexadecimal ship_commit")
    if not isinstance(integration_base_commit, str) or not re.fullmatch(
        r"[0-9a-fA-F]{40,64}", integration_base_commit
    ):
        errors.append("completed slice has no full hexadecimal integration_base_commit")
    if errors:
        return {
            "status": "error",
            "classification": None,
            "slice": slice_id,
            "continuation_ref": continuation_ref,
            "errors": errors,
            "warnings": warnings,
            "checks": checks,
        }

    recorded_ship_commit, ship_error = resolve_commit(root, ship_commit)
    recorded_base_commit, base_error = resolve_commit(root, integration_base_commit)
    branch_head, branch_error = _resolve_branch_commit(root, ship_branch)
    if ship_error or recorded_ship_commit is None:
        errors.append(ship_error or "could not resolve ship_commit")
    if branch_error or branch_head is None:
        errors.append(branch_error or "could not resolve ship_branch")
    if base_error or recorded_base_commit is None:
        errors.append(base_error or "could not resolve integration_base_commit")
    if recorded_ship_commit is not None and recorded_ship_commit != ship_commit:
        errors.append(f"ship_commit did not resolve to itself: {recorded_ship_commit} != {ship_commit}")
    if branch_head is not None and branch_head != ship_commit:
        errors.append(f"ship_branch head differs from ship_commit: {branch_head} != {ship_commit}")
    if recorded_base_commit is not None and recorded_base_commit != integration_base_commit:
        errors.append(
            "integration_base_commit did not resolve to itself: "
            f"{recorded_base_commit} != {integration_base_commit}"
        )
    base_is_ancestor = False
    if recorded_base_commit is not None and recorded_ship_commit is not None:
        base_is_ancestor, base_ancestor_error = _is_ancestor(
            root, integration_base_commit, ship_commit
        )
        if base_ancestor_error:
            errors.append(base_ancestor_error)
        elif not base_is_ancestor or integration_base_commit == ship_commit:
            errors.append("integration_base_commit must be a strict ancestor of ship_commit")
    checks["ship_branch"] = ship_branch
    checks["ship_commit"] = ship_commit
    checks["ship_branch_head"] = branch_head
    checks["integration_base_commit"] = integration_base_commit
    checks["integration_base_is_ancestor"] = base_is_ancestor
    if errors:
        return {
            "status": "error",
            "classification": None,
            "slice": slice_id,
            "continuation_ref": continuation_ref,
            "errors": errors,
            "warnings": warnings,
            "checks": checks,
        }

    ancestor, ancestor_error = _is_ancestor(root, ship_commit, continuation_commit)
    if ancestor_error:
        errors.append(ancestor_error)
    checks["ancestor"] = ancestor
    if errors:
        return {
            "status": "error",
            "classification": None,
            "slice": slice_id,
            "continuation_ref": continuation_ref,
            "errors": errors,
            "warnings": warnings,
            "checks": checks,
        }

    merge_base = integration_base_commit
    change_base_details: dict[str, Any] = {"strategy": "recorded-integration-base"}
    changed_paths, paths_error = _changed_paths(root, merge_base, ship_commit)
    if paths_error:
        errors.append(paths_error)
    checks["merge_base"] = merge_base
    checks["change_base"] = change_base_details
    checks["changed_paths"] = changed_paths
    if errors:
        return {
            "status": "error",
            "classification": None,
            "slice": slice_id,
            "continuation_ref": continuation_ref,
            "errors": errors,
            "warnings": warnings,
            "checks": checks,
        }

    surface_entries, surface_errors = _surface_entries(
        root, merge_base, ship_commit, continuation_commit, changed_paths
    )
    errors.extend(surface_errors)
    checks["surface_entries"] = surface_entries
    surfaces_retained, retention_dispositions, retention_errors = _surface_retention(
        root,
        merge_base,
        ship_commit,
        continuation_commit,
        surface_entries,
    )
    errors.extend(retention_errors)
    checks["surface_retention"] = {
        "retained": surfaces_retained,
        "dispositions": retention_dispositions,
    }
    if configured_receipt is None and ancestor and surfaces_retained and not errors:
        return {
            "status": "ok",
            "classification": "ancestor",
            "slice": slice_id,
            "continuation_ref": continuation_ref,
            "errors": [],
            "warnings": warnings,
            "checks": checks,
        }

    surface_equivalent = not surface_errors and all(
        row["ship"] == row["continuation"] for row in surface_entries.values()
    )
    checks["surface_equivalent"] = surface_equivalent
    if configured_receipt is None and surface_equivalent and not errors:
        return {
            "status": "ok",
            "classification": "surface-equivalent",
            "slice": slice_id,
            "continuation_ref": continuation_ref,
            "errors": [],
            "warnings": warnings,
            "checks": checks,
        }

    patch_check, patch_error = _patch_equivalence(root, continuation_commit, ship_commit)
    if patch_error:
        errors.append(patch_error)
    checks["patch_equivalence"] = patch_check
    patch_equivalent = bool(patch_check.get("rows")) and not any(
        (
            patch_check.get("exclusive_merge_commits"),
            patch_check.get("unmatched_commits"),
            patch_check.get("malformed_rows"),
        )
    )
    checks["patch_equivalent"] = patch_equivalent
    if configured_receipt is None and patch_equivalent and surfaces_retained and not errors:
        return {
            "status": "ok",
            "classification": "patch-equivalent",
            "slice": slice_id,
            "continuation_ref": continuation_ref,
            "errors": [],
            "warnings": warnings,
            "checks": checks,
        }

    if configured_receipt is None:
        if ancestor and not surfaces_retained:
            not_retained = sorted(
                path
                for path, disposition in retention_dispositions.items()
                if disposition not in {"identical", "evolved-patch-retained"}
            )
            errors.append(
                "ancestral ship commit has reverted, removed, or restored ship-changed "
                "surfaces and no committed reconciliation receipt is configured: "
                + ", ".join(not_retained)
            )
        else:
            errors.append(
                "ship commit is not ancestral with retained surfaces, surface-equivalent, "
                "or retained patch-equivalent, and no committed reconciliation receipt is configured"
            )
    else:
        safe_receipt_path = _safe_repo_path(configured_receipt)
        if safe_receipt_path is None:
            errors.append(f"unsafe integration receipt path: {configured_receipt!r}")
        elif _evidence_artifact_path(safe_receipt_path) != safe_receipt_path:
            errors.append(
                "configured integration receipt must be a JSON file directly under docs/evidence/"
            )
        else:
            receipt_text, receipt_read_error = committed_text(
                root, continuation_commit, safe_receipt_path
            )
            if receipt_read_error or receipt_text is None:
                errors.append(receipt_read_error or "could not read integration receipt")
            else:
                receipt, receipt_json_error = _parse_json_object(
                    receipt_text, f"integration receipt {safe_receipt_path}"
                )
                if receipt_json_error or receipt is None:
                    errors.append(receipt_json_error or "could not parse integration receipt")
                else:
                    receipt_commit = continuation_commit
                    receipt_surface_entries = surface_entries
                    if receipt.get("schema_version") == ANCHORED_RECEIPT_SCHEMA_VERSION:
                        errors.extend(
                            validate_repository(
                                root, expected_main_commit=continuation_commit
                            )
                        )
                        anchor_commit, anchor_error = _receipt_anchor_commit(
                            root, continuation_commit, safe_receipt_path
                        )
                        if anchor_error or anchor_commit is None:
                            errors.append(anchor_error or "could not resolve receipt anchor")
                        else:
                            receipt_commit = anchor_commit
                            checks["receipt_anchor_commit"] = anchor_commit
                            anchor_is_ancestor, anchor_ancestor_error = _is_ancestor(
                                root, anchor_commit, continuation_commit
                            )
                            if anchor_ancestor_error:
                                errors.append(anchor_ancestor_error)
                            elif not anchor_is_ancestor:
                                errors.append(
                                    "anchored integration receipt commit is not ancestral to continuation"
                                )
                            anchor_receipt_entry, anchor_receipt_error = git_tree_entry(
                                root, anchor_commit, safe_receipt_path
                            )
                            current_receipt_entry, current_receipt_error = git_tree_entry(
                                root, continuation_commit, safe_receipt_path
                            )
                            if anchor_receipt_error:
                                errors.append(anchor_receipt_error)
                            if current_receipt_error:
                                errors.append(current_receipt_error)
                            if (
                                not anchor_receipt_error
                                and not current_receipt_error
                                and anchor_receipt_entry != current_receipt_entry
                            ):
                                errors.append(
                                    "anchored integration receipt changed after its anchor commit"
                                )
                            receipt_surface_entries, anchor_surface_errors = _surface_entries(
                                root,
                                merge_base,
                                ship_commit,
                                anchor_commit,
                                changed_paths,
                            )
                            errors.extend(anchor_surface_errors)
                    receipt_errors = _validate_receipt(
                        root,
                        receipt_path=safe_receipt_path,
                        receipt=receipt,
                        slice_=slice_,
                        continuation_ref=continuation_ref,
                        continuation_commit=continuation_commit,
                        receipt_commit=receipt_commit,
                        ship_branch=ship_branch,
                        ship_commit=ship_commit,
                        merge_base=merge_base,
                        changed_paths=changed_paths,
                        surface_entries=receipt_surface_entries,
                        current_surface_entries=surface_entries,
                    )
                    errors.extend(receipt_errors)
                    if not receipt_errors and not errors:
                        return {
                            "status": "ok",
                            "classification": "reconciled",
                            "slice": slice_id,
                            "continuation_ref": continuation_ref,
                            "errors": [],
                            "warnings": warnings,
                            "checks": checks,
                        }

    return {
        "status": "error",
        "classification": None,
        "slice": slice_id,
        "continuation_ref": continuation_ref,
        "errors": errors,
        "warnings": warnings,
        "checks": checks,
    }


__all__ = [
    "ANCHORED_RECEIPT_SCHEMA_VERSION",
    "COMMAND_EVIDENCE_TRUST_BOUNDARY",
    "RECEIPT_SCHEMA_VERSION",
    "SUPPORTED_RECEIPT_SCHEMA_VERSIONS",
    "committed_bytes",
    "committed_text",
    "git_tree_entry",
    "resolve_commit",
    "verify_slice_integration",
]

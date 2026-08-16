#!/usr/bin/env python3
"""Build a v2 integration receipt from a frozen verification commit.

The builder refuses to overwrite receipts. The output is intended to be the
only new receipt path in a single-parent audit commit whose parent is the
verified commit.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.slice_integration import (
    ANCHORED_RECEIPT_SCHEMA_VERSION,
    COMMAND_EVIDENCE_TRUST_BOUNDARY,
    _discover_anchored_v2_receipts,
    _evidence_artifact_path,
    _receipt_anchor_commit,
    _review_artifact_path,
    _receipt_registry_topology,
    _strict_json_loads,
    _validate_supersedes_chain,
    committed_bytes,
    committed_text,
    git_tree_entry,
    resolve_commit,
    validate_repository,
)


COMMAND_EVIDENCE_RE = re.compile(
    r"(?im)^\s*(?:[-*]\s*)?Command evidence:\s*`?([^`\s]+)`?\s*$"
)
RETENTION_BY_PATH = {
    "ops/autonomy/autonomy_state.json": "runtime_state",
    "ops/autonomy/events.jsonl": "append_only",
    "ops/autonomy/failure_ledger.jsonl": "append_only",
    "ops/autonomy/progress.md": "append_only",
    "ops/autonomy/slices.json": "slice_entry_stable",
}


def _git(root: Path, *argv: str) -> str:
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("GIT_")
    }
    env["GIT_NO_REPLACE_OBJECTS"] = "1"
    result = subprocess.run(
        ["git", *argv],
        cwd=root,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if result.returncode != 0:
        raise ValueError(f"git {' '.join(argv)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def _safe_path(root: Path, value: Any) -> tuple[str, Path]:
    if not isinstance(value, str) or not value or "\x00" in value or "\\" in value or ":" in value:
        raise ValueError(f"unsafe repository path: {value!r}")
    rel = Path(value)
    if rel.is_absolute() or ".." in rel.parts or rel.parts[0] == ".git":
        raise ValueError(f"unsafe repository path: {value!r}")
    resolved = (root / rel).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise ValueError(f"repository path escapes root: {value!r}") from exc
    return rel.as_posix(), resolved


def _worktree_blob_entry(root: Path, rel_path: str) -> dict[str, str]:
    safe, path = _safe_path(root, rel_path)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"audit artifact must be a regular file: {safe}")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o111:
        raise ValueError(f"audit artifact must not be executable: {safe}")
    object_id = _git(root, "hash-object", "--", safe)
    return {"mode": "100644", "type": "blob", "object": object_id}


def _load_slices_at(root: Path, commit: str) -> list[dict[str, Any]]:
    text = _git(root, "show", f"{commit}:ops/autonomy/slices.json")
    payload, parse_error = _strict_json_loads(text, "committed slices")
    if parse_error:
        raise ValueError(parse_error)
    if not isinstance(payload, list):
        raise ValueError("committed slices must contain a list")
    return [row for row in payload if isinstance(row, dict)]


def _load_worktree_json(path: Path, description: str) -> dict[str, Any]:
    payload, parse_error = _strict_json_loads(path.read_text(encoding="utf-8"), description)
    if parse_error:
        raise ValueError(parse_error)
    if not isinstance(payload, dict):
        raise ValueError(f"{description} must contain a JSON object")
    return payload


def _dirty_paths(root: Path) -> set[str]:
    paths: set[str] = set()
    for argv in (
        ("diff", "--name-only", "-z", "HEAD"),
        ("diff", "--cached", "--name-only", "-z", "HEAD"),
        ("ls-files", "--others", "--exclude-standard", "-z"),
    ):
        output = _git(root, *argv)
        paths.update(value for value in output.split("\x00") if value)
    return paths


def _validate_reviewed_files(
    root: Path,
    payload: dict[str, Any],
    *,
    evidence_path: str,
    verification_commit: str,
) -> None:
    reviewed = payload.get("reviewed_files")
    if not isinstance(reviewed, list) or not reviewed:
        raise ValueError(f"command evidence needs reviewed_files: {evidence_path}")
    paths: list[str] = []
    for index, row in enumerate(reviewed, start=1):
        if not isinstance(row, dict) or set(row) != {"path", "sha256"}:
            raise ValueError(
                f"reviewed_files row {index} has invalid fields: {evidence_path}"
            )
        reviewed_path, _ = _safe_path(root, row.get("path"))
        if reviewed_path in paths:
            raise ValueError(f"reviewed_files repeats path: {reviewed_path}: {evidence_path}")
        paths.append(reviewed_path)
        expected = row.get("sha256")
        if not isinstance(expected, str) or re.fullmatch(r"[0-9a-f]{64}", expected) is None:
            raise ValueError(
                f"reviewed_files row {index} has invalid sha256: {evidence_path}"
            )
        entry, entry_error = git_tree_entry(root, verification_commit, reviewed_path)
        if entry_error:
            raise ValueError(entry_error)
        if (
            entry is None
            or entry.get("mode") != "100644"
            or entry.get("type") != "blob"
        ):
            raise ValueError(
                "reviewed file is not a regular 100644 committed blob at "
                f"verification_commit: {reviewed_path}"
            )
        content, content_error = committed_bytes(root, verification_commit, reviewed_path)
        if content_error or content is None:
            raise ValueError(content_error or f"could not read reviewed file: {reviewed_path}")
        if hashlib.sha256(content).hexdigest() != expected:
            raise ValueError(f"reviewed_files sha256 mismatch: {reviewed_path}")
    if paths != sorted(paths):
        raise ValueError(f"reviewed_files paths must be sorted: {evidence_path}")


def _real_verification_rows(
    root: Path,
    *,
    slice_id: str,
    verification_commit: str,
    acceptance: list[str],
    evidence_paths: list[str],
) -> list[dict[str, Any]]:
    proven: dict[str, tuple[str, str]] = {}
    for evidence_path in evidence_paths:
        _, evidence_file = _safe_path(root, evidence_path)
        payload = _load_worktree_json(
            evidence_file, f"command evidence {evidence_path}"
        )
        if payload.get("slice") != slice_id:
            raise ValueError(f"command evidence slice mismatch: {evidence_path}")
        if payload.get("status") != "ok":
            raise ValueError(f"command evidence status is not ok: {evidence_path}")
        if payload.get("tested_commit") != verification_commit:
            raise ValueError(f"command evidence tested_commit mismatch: {evidence_path}")
        _validate_reviewed_files(
            root,
            payload,
            evidence_path=evidence_path,
            verification_commit=verification_commit,
        )
        commands = payload.get("commands")
        if not isinstance(commands, list) or not commands:
            raise ValueError(f"command evidence must contain commands: {evidence_path}")
        for index, row in enumerate(commands, start=1):
            if not isinstance(row, dict):
                raise ValueError(
                    f"command evidence row {index} is not an object: {evidence_path}"
                )
            command = row.get("command")
            exit_code = row.get("exit_code")
            if not isinstance(command, str) or not command.strip():
                raise ValueError(
                    f"command evidence row {index} has no command: {evidence_path}"
                )
            if type(exit_code) is not int or exit_code != 0:
                raise ValueError(
                    f"command evidence command did not pass: {command}: {evidence_path}"
                )
            row_status = row.get("status", payload["status"])
            if row_status not in {"ok", "pass", "passed"}:
                raise ValueError(
                    f"command evidence command has non-passing status: {command}: {evidence_path}"
                )
            if command not in acceptance:
                continue
            if command in proven:
                raise ValueError(
                    f"acceptance command is proven more than once: {command}: {evidence_path}"
                )
            proven[command] = (evidence_path, row_status)
    missing = [command for command in acceptance if command not in proven]
    if missing:
        raise ValueError(
            "real command evidence does not cover committed acceptance; "
            f"missing={missing}"
        )
    return [
        {
            "command": command,
            "evidence_path": proven[command][0],
            "exit_code": 0,
            "status": proven[command][1],
        }
        for command in acceptance
    ]


def _validate_generated_schema(
    root: Path,
    verification_commit: str,
    receipt: dict[str, Any],
) -> None:
    schema_text, schema_error = committed_text(
        root,
        verification_commit,
        "ops/autonomy/schemas/slice_integration_receipt_v2.schema.json",
    )
    if schema_error or schema_text is None:
        raise ValueError(schema_error or "anchored receipt schema is not committed")
    schema, parse_error = _strict_json_loads(schema_text, "anchored receipt JSON schema")
    if parse_error or not isinstance(schema, dict):
        raise ValueError(parse_error or "anchored receipt JSON schema must be an object")
    try:
        from jsonschema import Draft202012Validator

        violations = sorted(
            Draft202012Validator(schema).iter_errors(receipt),
            key=lambda item: list(item.absolute_path),
        )
    except Exception as exc:
        raise ValueError(f"could not validate generated anchored receipt: {exc}") from exc
    if violations:
        rendered = "; ".join(
            ("/".join(str(part) for part in item.absolute_path) or "<root>")
            + f": {item.message}"
            for item in violations
        )
        raise ValueError(f"generated anchored receipt violates committed schema: {rendered}")


def build_receipt(
    root: Path,
    slice_id: str,
    verification_ref: str,
    supersedes_receipt_path: str | None = None,
) -> tuple[dict[str, Any], str]:
    repository_errors = validate_repository(root)
    if repository_errors:
        raise ValueError("; ".join(repository_errors))
    verification_commit, resolve_error = resolve_commit(root, verification_ref)
    if resolve_error or verification_commit is None:
        raise ValueError(resolve_error or "could not resolve verification commit")
    pinned_errors = validate_repository(
        root, expected_main_commit=verification_commit
    )
    if pinned_errors:
        raise ValueError("; ".join(pinned_errors))
    head = _git(root, "rev-parse", "HEAD")
    if head != verification_commit:
        raise ValueError(
            f"verification commit must be current HEAD before the audit commit: {verification_commit} != {head}"
        )

    slices = _load_slices_at(root, verification_commit)
    matches = [row for row in slices if row.get("id") == slice_id]
    if len(matches) != 1:
        raise ValueError(f"expected exactly one committed {slice_id} row; found {len(matches)}")
    slice_ = matches[0]
    if slice_.get("status") != "complete":
        raise ValueError(f"slice is not complete: {slice_id}")
    receipt_path = slice_.get("integration_receipt")
    safe_receipt, output_path = _safe_path(root, receipt_path)
    if _evidence_artifact_path(safe_receipt) != safe_receipt:
        raise ValueError(
            "integration receipt must be a JSON file directly under docs/evidence/"
        )
    if output_path.exists():
        raise ValueError(f"refusing to overwrite immutable receipt path: {safe_receipt}")

    ship_branch = str(slice_.get("ship_branch") or "")
    ship_commit = str(slice_.get("ship_commit") or "")
    merge_base = str(slice_.get("integration_base_commit") or "")
    if _git(root, "rev-parse", f"refs/heads/{ship_branch}^{{commit}}") != ship_commit:
        raise ValueError("ship branch does not match recorded ship commit")
    identity = {
        "slice": slice_id,
        "ship_branch": ship_branch,
        "ship_commit": ship_commit,
        "merge_base": merge_base,
    }
    earlier_receipts, discovery_errors = _discover_anchored_v2_receipts(
        root, verification_commit, identity
    )
    earlier_tip, topology_errors = _receipt_registry_topology(earlier_receipts)
    registry_errors = [*discovery_errors, *topology_errors]
    if registry_errors:
        raise ValueError("; ".join(registry_errors))
    if safe_receipt in earlier_receipts:
        raise ValueError("successor receipt must use a new configured receipt path")
    if earlier_tip is None and supersedes_receipt_path is not None:
        raise ValueError("a root v2 receipt cannot declare a predecessor")
    if earlier_tip is not None:
        if supersedes_receipt_path is None:
            raise ValueError(
                "an earlier anchored receipt exists for this slice/ship identity; "
                "a new root v2 receipt is forbidden"
            )
        requested_prior, _ = _safe_path(root, supersedes_receipt_path)
        if requested_prior != earlier_tip:
            raise ValueError(
                "successor must exactly supersede the durable receipt registry chain tip: "
                f"{requested_prior} != {earlier_tip}"
            )
    changed_paths = sorted(
        line
        for line in _git(
            root,
            "diff",
            "--no-renames",
            "--name-only",
            merge_base,
            ship_commit,
        ).splitlines()
        if line
    )
    if not changed_paths:
        raise ValueError("ship range has no changed paths")

    surfaces: list[dict[str, Any]] = []
    for path in changed_paths:
        base_entry, base_error = git_tree_entry(root, merge_base, path)
        ship_entry, ship_error = git_tree_entry(root, ship_commit, path)
        continuation_entry, continuation_error = git_tree_entry(
            root, verification_commit, path
        )
        for error in (base_error, ship_error, continuation_error):
            if error:
                raise ValueError(error)
        if ship_entry is not None and continuation_entry is None:
            raise ValueError(f"v2 recovery may not retire a ship surface: {path}")
        if ship_entry != base_entry and continuation_entry == base_entry:
            raise ValueError(f"v2 recovery may not restore a pre-ship surface: {path}")
        disposition = "identical" if ship_entry == continuation_entry else "evolved"
        row: dict[str, Any] = {
            "path": path,
            "disposition": disposition,
            "retention": RETENTION_BY_PATH.get(path, "immutable"),
            "ship_entry": ship_entry,
            "continuation_entry": continuation_entry,
        }
        if disposition == "evolved":
            row["reason"] = (
                f"The {slice_id} post-completion reconciliation review confirms that this exact "
                "verified continuation object preserves or strengthens the historical slice contract."
            )
        surfaces.append(row)

    configured_reviews = slice_.get(
        "integration_review_artifacts", slice_.get("review_artifacts")
    )
    if not isinstance(configured_reviews, list) or not configured_reviews:
        raise ValueError("slice must configure at least one reconciliation review")
    review_rows: list[dict[str, Any]] = []
    evidence_paths: list[str] = []
    for value in configured_reviews:
        review_path, review_file = _safe_path(root, value)
        if _review_artifact_path(review_path) != review_path:
            raise ValueError(
                "integration review must be a Markdown file directly under docs/reviews/: "
                f"{review_path}"
            )
        review_text = review_file.read_text(encoding="utf-8")
        review_rows.append(
            {"path": review_path, "tree_entry": _worktree_blob_entry(root, review_path)}
        )
        for evidence_path in COMMAND_EVIDENCE_RE.findall(review_text):
            safe_evidence, _ = _safe_path(root, evidence_path)
            if _evidence_artifact_path(safe_evidence) != safe_evidence:
                raise ValueError(
                    "command evidence must be a JSON file directly under docs/evidence/: "
                    f"{safe_evidence}"
                )
            if safe_evidence == safe_receipt:
                raise ValueError("integration receipt and command evidence paths must be distinct")
            if safe_evidence not in evidence_paths:
                evidence_paths.append(safe_evidence)
    if not evidence_paths:
        raise ValueError("reconciliation review has no command-evidence reference")
    evidence_rows = [
        {"path": path, "tree_entry": _worktree_blob_entry(root, path)}
        for path in evidence_paths
    ]

    acceptance = slice_.get("acceptance")
    if not isinstance(acceptance, list) or not acceptance:
        raise ValueError("slice has no acceptance commands")
    if not all(isinstance(command, str) and command.strip() for command in acceptance):
        raise ValueError("slice acceptance commands must be non-empty strings")
    verification_rows = _real_verification_rows(
        root,
        slice_id=slice_id,
        verification_commit=verification_commit,
        acceptance=acceptance,
        evidence_paths=evidence_paths,
    )

    allowed_dirty_paths = {
        *(row["path"] for row in review_rows),
        *(row["path"] for row in evidence_rows),
    }
    forbidden_dirty_paths = sorted(_dirty_paths(root) - allowed_dirty_paths)
    if forbidden_dirty_paths:
        raise ValueError(
            "audit worktree contains implementation/control changes: "
            + ", ".join(forbidden_dirty_paths)
        )
    audit_delta = [safe_receipt]
    for row in [*review_rows, *evidence_rows]:
        prior_entry, prior_error = git_tree_entry(
            root, verification_commit, str(row["path"])
        )
        if prior_error:
            raise ValueError(prior_error)
        if prior_entry != row["tree_entry"]:
            audit_delta.append(str(row["path"]))
    audit_delta = sorted(set(audit_delta))

    receipt = {
        "schema_version": ANCHORED_RECEIPT_SCHEMA_VERSION,
        "slice": slice_id,
        "continuation_ref": "main",
        "ship_branch": ship_branch,
        "ship_commit": ship_commit,
        "merge_base": merge_base,
        "verification_commit": verification_commit,
        "command_evidence_trust_boundary": COMMAND_EVIDENCE_TRUST_BOUNDARY,
        "audit_delta": audit_delta,
        "surfaces": surfaces,
        "verification_commands": verification_rows,
        "review_artifacts": review_rows,
        "command_evidence": evidence_rows,
    }
    if supersedes_receipt_path is not None:
        prior_path, _ = _safe_path(root, supersedes_receipt_path)
        if (
            prior_path == safe_receipt
            or _evidence_artifact_path(prior_path) != prior_path
        ):
            raise ValueError(
                "superseded receipt must be a different JSON path directly under docs/evidence/"
            )
        prior_anchor, prior_anchor_error = _receipt_anchor_commit(
            root, verification_commit, prior_path
        )
        if prior_anchor_error or prior_anchor is None:
            raise ValueError(prior_anchor_error or "could not resolve superseded receipt anchor")
        prior_entry, prior_entry_error = git_tree_entry(root, prior_anchor, prior_path)
        current_prior_entry, current_prior_error = git_tree_entry(
            root, verification_commit, prior_path
        )
        for error in (prior_entry_error, current_prior_error):
            if error:
                raise ValueError(error)
        if (
            prior_entry is None
            or prior_entry.get("mode") != "100644"
            or prior_entry.get("type") != "blob"
        ):
            raise ValueError("superseded receipt must be a regular 100644 blob")
        if current_prior_entry != prior_entry:
            raise ValueError("superseded receipt changed after its anchor")
        prior_text, prior_read_error = committed_text(root, prior_anchor, prior_path)
        if prior_read_error or prior_text is None:
            raise ValueError(prior_read_error or "could not read superseded receipt")
        prior_receipt, prior_parse_error = _strict_json_loads(
            prior_text, f"superseded receipt {prior_path}"
        )
        if prior_parse_error or not isinstance(prior_receipt, dict):
            raise ValueError(prior_parse_error or "superseded receipt must be an object")
        if prior_receipt.get("schema_version") != ANCHORED_RECEIPT_SCHEMA_VERSION:
            raise ValueError("only anchored v2 receipts may be superseded")
        for field in ("slice", "ship_branch", "ship_commit", "merge_base"):
            if prior_receipt.get(field) != receipt[field]:
                raise ValueError(f"superseded receipt identity mismatch: {field}")
        _git(
            root,
            "merge-base",
            "--is-ancestor",
            prior_anchor,
            verification_commit,
        )
        receipt["supersedes_receipt"] = {
            "path": prior_path,
            "anchor_commit": prior_anchor,
            "tree_entry": prior_entry,
        }
        successor_errors = _validate_supersedes_chain(
            root,
            supersedes=receipt["supersedes_receipt"],
            identity={
                "slice": slice_id,
                "ship_branch": ship_branch,
                "ship_commit": ship_commit,
                "merge_base": merge_base,
            },
            child_verification_commit=verification_commit,
            continuation_commit=verification_commit,
            seen_paths={safe_receipt},
            pending_successor_path=safe_receipt,
            pending_successor_receipt=receipt,
        )
        if successor_errors:
            raise ValueError("; ".join(successor_errors))
    _validate_generated_schema(root, verification_commit, receipt)
    return receipt, safe_receipt


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build an immutable v2 slice integration receipt")
    parser.add_argument("slice_id")
    parser.add_argument("--root", default=".")
    parser.add_argument("--verification-commit", default="HEAD")
    parser.add_argument("--supersedes-receipt")
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    try:
        receipt, receipt_path = build_receipt(
            root,
            args.slice_id.upper(),
            args.verification_commit,
            args.supersedes_receipt,
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "error", "errors": [str(exc)]}, indent=2, sort_keys=True))
        return 1
    text = json.dumps(receipt, indent=2, sort_keys=True) + "\n"
    if args.write:
        _, output = _safe_path(root, receipt_path)
        output.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        print(json.dumps({"status": "ok", "receipt": receipt_path}, indent=2, sort_keys=True))
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

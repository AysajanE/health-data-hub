from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from scripts.build_slice_integration_receipt import _real_verification_rows, build_receipt
from scripts.slice_integration import (
    COMMAND_EVIDENCE_TRUST_BOUNDARY,
    _validate_supersedes_chain,
    committed_bytes,
    git_tree_entry,
    validate_repository,
    verify_slice_integration,
)


ACCEPTANCE = [
    "python -m pytest tests/test_feature.py -q",
    "python scripts/check_no_tracked_data.py",
]
REVIEW_PATH = "docs/reviews/s04-autonomous-feature-review.md"
EVIDENCE_PATH = "docs/evidence/s04-integration-command-evidence.json"
RECEIPT_PATH = "docs/evidence/s04-canonical-integration.json"
PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _git(root: Path, *argv: str) -> str:
    result = subprocess.run(
        ["git", *argv],
        cwd=root,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, (argv, result.stdout, result.stderr)
    return result.stdout.strip()


def _write(root: Path, rel_path: str, text: str) -> Path:
    path = root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _write_json(root: Path, rel_path: str, payload: object) -> Path:
    return _write(root, rel_path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def _init_repo(root: Path) -> str:
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "tests@example.com")
    _git(root, "config", "user.name", "Tests")
    _write(root, "feature.py", "VALUE = 'base'\n")
    _git(root, "add", "feature.py")
    _git(root, "commit", "-m", "base")
    return _git(root, "rev-parse", "HEAD")


def _commit_all(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-m", message)
    return _git(root, "rev-parse", "HEAD")


def _slice_payload(
    ship_commit: str,
    *,
    integration_base_commit: str | None = None,
    receipt: str | None = None,
    acceptance: list[str] | None = None,
    reviews: list[str] | None = None,
    integration_reviews: list[str] | None = None,
) -> list[dict[str, object]]:
    row: dict[str, object] = {
        "id": "S04",
        "status": "complete",
        "ship_branch": "ship/s04",
        "ship_commit": ship_commit,
        "run_id": "RUN_S04",
        "acceptance": ACCEPTANCE if acceptance is None else acceptance,
        "review_artifacts": [] if reviews is None else reviews,
    }
    if receipt is not None:
        row["integration_receipt"] = receipt
    if integration_base_commit is not None:
        row["integration_base_commit"] = integration_base_commit
    if integration_reviews is not None:
        row["integration_review_artifacts"] = integration_reviews
    return [row]


def _write_slices(root: Path, payload: list[dict[str, object]]) -> None:
    for row in payload:
        if row.get("status") != "complete" or row.get("integration_base_commit") is not None:
            continue
        ship_commit = row.get("ship_commit")
        assert isinstance(ship_commit, str)
        parents = _git(root, "rev-list", "--parents", "-n", "1", ship_commit).split()
        assert len(parents) >= 2, "test completed slices require an explicit ship base"
        row["integration_base_commit"] = parents[1]
    _write_json(root, "ops/autonomy/slices.json", payload)


def _worktree_blob_entry(root: Path, rel_path: str) -> dict[str, str]:
    object_id = _git(root, "hash-object", rel_path)
    return {"mode": "100644", "type": "blob", "object": object_id}


def _make_ship_commit(root: Path, files: dict[str, str]) -> tuple[str, str]:
    base = _git(root, "rev-parse", "main")
    _git(root, "checkout", "-b", "ship/s04")
    for path, text in files.items():
        _write(root, path, text)
    ship_commit = _commit_all(root, "ship S04")
    _git(root, "checkout", "main")
    return base, ship_commit


def _write_review_bundle(root: Path) -> None:
    _write_json(
        root,
        EVIDENCE_PATH,
        {
            "schema_version": "autokeel_command_evidence_v1",
            "slice": "S04",
            "status": "ok",
            "commands": [
                {"command": command, "exit_code": 0, "stdout_tail": "pass", "stderr_tail": ""}
                for command in ACCEPTANCE
            ],
        },
    )
    _write(
        root,
        REVIEW_PATH,
        """# S04 Canonical Integration Review

This is an autonomous slice review performed by an independent reviewer.

Verdict: pass

Evidence files checked:
- feature.py
- stable.py
- the committed integration receipt and command evidence

Exact commands run:
- python -m pytest tests/test_feature.py -q
- python scripts/check_no_tracked_data.py

Command evidence: docs/evidence/s04-integration-command-evidence.json

Blocking findings: none

The reconciled continuation keeps the slice contract while recording every
source surface and the exact committed object used for this determination.
""",
    )


def _reconciled_repo(root: Path, mutate: str | None = None) -> None:
    merge_base = _init_repo(root)
    _, ship_commit = _make_ship_commit(
        root,
        {
            "feature.py": "VALUE = 'ship'\n",
            "obsolete.py": "OBSOLETE = True\n",
            "stable.py": "STABLE = True\n",
        },
    )

    _write(root, "feature.py", "VALUE = 'reconciled and safer'\n")
    _write(root, "stable.py", "STABLE = True\n")
    _write_review_bundle(root)
    _write_slices(
        root,
        _slice_payload(
            ship_commit,
            receipt=RECEIPT_PATH,
            reviews=[REVIEW_PATH],
        ),
    )

    surfaces: list[dict[str, object]] = []
    for path, disposition, reason in (
        ("feature.py", "evolved", "Preserves the feature contract with a safer continuation implementation."),
        ("obsolete.py", "retired", "The obsolete ship-only helper is unnecessary after reconciliation."),
        ("stable.py", "identical", None),
    ):
        ship_entry, error = git_tree_entry(root, ship_commit, path)
        assert error is None
        continuation_entry = None if path == "obsolete.py" else _worktree_blob_entry(root, path)
        row: dict[str, object] = {
            "path": path,
            "disposition": disposition,
            "ship_entry": ship_entry,
            "continuation_entry": continuation_entry,
        }
        if reason is not None:
            row["reason"] = reason
        surfaces.append(row)

    verification_commands = [
        {"command": command, "exit_code": 0, "status": "pass"}
        for command in ACCEPTANCE
    ]
    review_rows: list[dict[str, object]] = [
        {"path": REVIEW_PATH, "tree_entry": _worktree_blob_entry(root, REVIEW_PATH)}
    ]
    receipt: dict[str, object] = {
        "schema_version": "autokeel.slice_integration.v1",
        "slice": "S04",
        "continuation_ref": "main",
        "ship_branch": "ship/s04",
        "ship_commit": ship_commit,
        "merge_base": merge_base,
        "surfaces": surfaces,
        "verification_commands": verification_commands,
        "review_artifacts": review_rows,
    }

    if mutate == "missing_surface":
        surfaces.pop()
    elif mutate == "missing_reason":
        surfaces[0].pop("reason")
    elif mutate == "failed_command":
        verification_commands[0]["exit_code"] = 1
    elif mutate == "missing_review":
        review_rows.clear()
    elif mutate == "wrong_ship_commit":
        receipt["ship_commit"] = merge_base
    elif mutate == "wrong_tree_entry":
        surfaces[0]["continuation_entry"] = surfaces[0]["ship_entry"]

    _write_json(root, RECEIPT_PATH, receipt)
    _commit_all(root, "reconcile S04 onto main")


def test_classifies_ancestor(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _, ship_commit = _make_ship_commit(tmp_path, {"landed.py": "LANDED = True\n"})
    _git(tmp_path, "merge", "--no-ff", "--no-edit", "ship/s04")
    _write_slices(tmp_path, _slice_payload(ship_commit))
    _commit_all(tmp_path, "record completed slice")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "ok", report
    assert report["classification"] == "ancestor"
    assert report["checks"]["surface_retention"]["retained"] is True


def test_rejects_multi_commit_fast_forward_with_unproven_ship_base(tmp_path: Path) -> None:
    integration_base = _init_repo(tmp_path)
    _git(tmp_path, "checkout", "-b", "ship/s04")
    _write(tmp_path, "first.py", "FIRST = True\n")
    _commit_all(tmp_path, "first slice commit")
    _write(tmp_path, "second.py", "SECOND = True\n")
    ship_commit = _commit_all(tmp_path, "second slice commit")
    _git(tmp_path, "checkout", "main")
    _git(tmp_path, "merge", "--ff-only", "ship/s04")
    _git(tmp_path, "rm", "first.py")
    _commit_all(tmp_path, "remove an earlier ship surface")
    _write_slices(
        tmp_path,
        _slice_payload(ship_commit, integration_base_commit=integration_base),
    )
    _commit_all(tmp_path, "record completed fast-forward slice")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error", report
    assert report["classification"] is None
    assert report["checks"]["changed_paths"] == ["first.py", "second.py"]
    assert report["checks"]["surface_retention"]["dispositions"]["first.py"] in {
        "reverted-to-pre-ship",
        "removed-after-ship",
    }


def test_explicit_base_covers_ship_work_partially_fast_forwarded_before_merge(
    tmp_path: Path,
) -> None:
    integration_base = _init_repo(tmp_path)
    _git(tmp_path, "checkout", "-b", "ship/s04")
    _write(tmp_path, "first.py", "FIRST = True\n")
    _commit_all(tmp_path, "first slice commit")
    _git(tmp_path, "checkout", "main")
    _git(tmp_path, "merge", "--ff-only", "ship/s04")
    _git(tmp_path, "checkout", "ship/s04")
    _write(tmp_path, "second.py", "SECOND = True\n")
    ship_commit = _commit_all(tmp_path, "second slice commit")
    _git(tmp_path, "checkout", "main")
    _git(tmp_path, "merge", "--no-ff", "--no-edit", "ship/s04")
    _git(tmp_path, "rm", "first.py")
    _commit_all(tmp_path, "remove the partially fast-forwarded surface")
    _write_slices(
        tmp_path,
        _slice_payload(ship_commit, integration_base_commit=integration_base),
    )
    _commit_all(tmp_path, "record completed partially fast-forwarded slice")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error", report
    assert report["checks"]["changed_paths"] == ["first.py", "second.py"]
    assert report["checks"]["surface_retention"]["retained"] is False


def test_rejects_ancestor_after_complete_merge_revert(tmp_path: Path) -> None:
    base = _init_repo(tmp_path)
    _, ship_commit = _make_ship_commit(tmp_path, {"landed.py": "LANDED = True\n"})
    _git(tmp_path, "merge", "--no-ff", "--no-edit", "ship/s04")
    landing_merge = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "revert", "-m", "1", "--no-edit", landing_merge)
    _write_slices(tmp_path, _slice_payload(ship_commit))
    _commit_all(tmp_path, "record completed slice after revert")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert report["classification"] is None
    assert report["checks"]["ancestor"] is True
    assert report["checks"]["merge_base"] == base
    assert report["checks"]["surface_retention"] == {
        "retained": False,
        "dispositions": {"landed.py": "reverted-to-pre-ship"},
    }
    assert any("no committed reconciliation receipt" in error for error in report["errors"])


def test_rejects_ancestor_when_one_of_multiple_ship_surfaces_is_removed(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _, ship_commit = _make_ship_commit(
        tmp_path,
        {"kept.py": "KEPT = True\n", "removed.py": "REMOVED = True\n"},
    )
    _git(tmp_path, "merge", "--no-ff", "--no-edit", "ship/s04")
    _git(tmp_path, "rm", "removed.py")
    _commit_all(tmp_path, "remove one landed surface")
    _write_slices(tmp_path, _slice_payload(ship_commit))
    _commit_all(tmp_path, "record completed slice after partial removal")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert report["checks"]["surface_retention"] == {
        "retained": False,
        "dispositions": {
            "kept.py": "identical",
            "removed.py": "reverted-to-pre-ship",
        },
    }


def test_requires_receipt_when_ancestral_landed_surface_later_evolves(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _, ship_commit = _make_ship_commit(tmp_path, {"feature.py": "VALUE = 'ship'\n"})
    _git(tmp_path, "merge", "--no-ff", "--no-edit", "ship/s04")
    _write(tmp_path, "feature.py", "VALUE = 'ship'\nEXTRA = 'later evolution'\n")
    _commit_all(tmp_path, "evolve landed surface")
    _write_slices(tmp_path, _slice_payload(ship_commit))
    _commit_all(tmp_path, "record evolved completed slice")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error", report
    assert report["classification"] is None
    assert report["checks"]["surface_retention"] == {
        "retained": False,
        "dispositions": {"feature.py": "ambiguous-evolution"},
    }


def test_rejects_revert_disguised_by_unrelated_third_blob(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _, ship_commit = _make_ship_commit(tmp_path, {"feature.py": "VALUE = 'ship'\n"})
    _git(tmp_path, "merge", "--no-ff", "--no-edit", "ship/s04")
    _write(tmp_path, "feature.py", "VALUE = 'base'\n# unrelated third blob\n")
    _commit_all(tmp_path, "disguise the revert")
    _write_slices(tmp_path, _slice_payload(ship_commit))
    _commit_all(tmp_path, "record completed slice after disguised revert")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert report["checks"]["surface_retention"] == {
        "retained": False,
        "dispositions": {"feature.py": "ambiguous-evolution"},
    }


def test_allows_ancestor_revert_only_with_hash_bound_reconciliation_receipt(
    tmp_path: Path,
) -> None:
    base = _init_repo(tmp_path)
    _, ship_commit = _make_ship_commit(tmp_path, {"landed.py": "LANDED = True\n"})
    _git(tmp_path, "merge", "--no-ff", "--no-edit", "ship/s04")
    landing_merge = _git(tmp_path, "rev-parse", "HEAD")
    _git(tmp_path, "revert", "-m", "1", "--no-edit", landing_merge)
    _write_review_bundle(tmp_path)
    _write_slices(
        tmp_path,
        _slice_payload(
            ship_commit,
            receipt=RECEIPT_PATH,
            reviews=[REVIEW_PATH],
        ),
    )
    ship_entry, error = git_tree_entry(tmp_path, ship_commit, "landed.py")
    assert error is None
    _write_json(
        tmp_path,
        RECEIPT_PATH,
        {
            "schema_version": "autokeel.slice_integration.v1",
            "slice": "S04",
            "continuation_ref": "main",
            "ship_branch": "ship/s04",
            "ship_commit": ship_commit,
            "merge_base": base,
            "surfaces": [
                {
                    "path": "landed.py",
                    "disposition": "retired",
                    "reason": "The landed surface was deliberately retired after integration.",
                    "ship_entry": ship_entry,
                    "continuation_entry": None,
                }
            ],
            "verification_commands": [
                {"command": command, "exit_code": 0, "status": "pass"}
                for command in ACCEPTANCE
            ],
            "review_artifacts": [
                {"path": REVIEW_PATH, "tree_entry": _worktree_blob_entry(tmp_path, REVIEW_PATH)}
            ],
        },
    )
    _commit_all(tmp_path, "reconcile intentionally retired landed surface")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "ok", report
    assert report["classification"] == "reconciled"


def test_classifies_squashed_final_surfaces_without_ancestry(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _, ship_commit = _make_ship_commit(
        tmp_path,
        {"one.py": "ONE = 1\n", "two.py": "TWO = 2\n"},
    )
    _write(tmp_path, "one.py", "ONE = 1\n")
    _write(tmp_path, "two.py", "TWO = 2\n")
    _write_slices(tmp_path, _slice_payload(ship_commit))
    _commit_all(tmp_path, "squash ship surfaces and record state")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "ok"
    assert report["classification"] == "surface-equivalent"


def test_requires_receipt_when_cherry_picked_surface_later_evolves(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _, ship_commit = _make_ship_commit(tmp_path, {"feature.py": "VALUE = 'ship'\n"})
    _write_slices(tmp_path, _slice_payload(ship_commit))
    _commit_all(tmp_path, "record ship identity")
    _git(tmp_path, "cherry-pick", ship_commit)
    _write(tmp_path, "feature.py", "VALUE = 'ship'\nEXTRA = 'later evolution'\n")
    _commit_all(tmp_path, "evolve landed feature")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error", report
    assert report["classification"] is None
    assert report["checks"]["patch_equivalent"] is True
    assert report["checks"]["surface_retention"] == {
        "retained": False,
        "dispositions": {"feature.py": "ambiguous-evolution"},
    }


def test_rejects_retained_ship_text_followed_by_behavioral_revert(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _, ship_commit = _make_ship_commit(tmp_path, {"feature.py": "VALUE = 'ship'\n"})
    _git(tmp_path, "merge", "--no-ff", "--no-edit", "ship/s04")
    _write(tmp_path, "feature.py", "VALUE = 'ship'\nVALUE = 'base'\n")
    _commit_all(tmp_path, "restore old behavior after a shipped textual decoy")
    _write_slices(tmp_path, _slice_payload(ship_commit))
    _commit_all(tmp_path, "record completed slice after behavioral revert")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error", report
    assert report["checks"]["surface_retention"] == {
        "retained": False,
        "dispositions": {"feature.py": "ambiguous-evolution"},
    }


def test_classifies_valid_committed_reconciliation_receipt(tmp_path: Path) -> None:
    _reconciled_repo(tmp_path)

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "ok", report
    assert report["classification"] == "reconciled"
    assert report["checks"]["changed_paths"] == ["feature.py", "obsolete.py", "stable.py"]


@pytest.mark.parametrize(
    ("mutation", "expected_error"),
    [
        ("missing_surface", "not the exhaustive sorted ship-change set"),
        ("missing_reason", "requires a reason"),
        ("failed_command", "verification command did not pass"),
        ("missing_review", "reviews do not exactly match"),
        ("wrong_ship_commit", "ship_commit mismatch"),
        ("wrong_tree_entry", "continuation tree entry mismatch"),
    ],
)
def test_rejects_incomplete_or_unbound_reconciliation_receipt(
    tmp_path: Path,
    mutation: str,
    expected_error: str,
) -> None:
    _reconciled_repo(tmp_path, mutate=mutation)

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert report["classification"] is None
    assert any(expected_error in error for error in report["errors"]), report


def test_uncommitted_receipt_and_product_changes_cannot_create_success(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _, ship_commit = _make_ship_commit(tmp_path, {"feature.py": "VALUE = 'ship'\n"})
    _write_slices(
        tmp_path,
        _slice_payload(ship_commit, receipt=RECEIPT_PATH, reviews=[REVIEW_PATH]),
    )
    _commit_all(tmp_path, "record incomplete canonical state")

    _write(tmp_path, "feature.py", "VALUE = 'ship'\n")
    _write_review_bundle(tmp_path)
    _write_json(
        tmp_path,
        RECEIPT_PATH,
        {
            "schema_version": "autokeel.slice_integration.v1",
            "slice": "S04",
            "continuation_ref": "main",
            "ship_branch": "ship/s04",
            "ship_commit": ship_commit,
            "merge_base": _git(tmp_path, "merge-base", "main", ship_commit),
            "surfaces": [],
            "verification_commands": [],
            "review_artifacts": [],
        },
    )

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert report["classification"] is None
    assert any("not committed" in error for error in report["errors"])


def test_dirty_worktree_cannot_change_a_durable_success_classification(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _, ship_commit = _make_ship_commit(tmp_path, {"same.py": "SAME = True\n"})
    _write(tmp_path, "same.py", "SAME = True\n")
    _write_slices(tmp_path, _slice_payload(ship_commit))
    _commit_all(tmp_path, "materialize exact surface")
    before = verify_slice_integration(tmp_path, "S04")

    _write(tmp_path, "same.py", "SAME = False  # dirty only\n")
    _write_slices(tmp_path, [{"id": "S04", "status": "pending"}])
    after = verify_slice_integration(tmp_path, "S04")

    assert before["classification"] == "surface-equivalent"
    assert after["status"] == "ok"
    assert after["classification"] == before["classification"]
    assert after["checks"]["continuation_commit"] == before["checks"]["continuation_commit"]


def test_receipt_becomes_stale_when_a_bound_tree_entry_changes(tmp_path: Path) -> None:
    _reconciled_repo(tmp_path)
    assert verify_slice_integration(tmp_path, "S04")["status"] == "ok"
    _write(tmp_path, "feature.py", "VALUE = 'changed after receipt'\n")
    _commit_all(tmp_path, "change a receipt-bound surface")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("continuation tree entry mismatch" in error for error in report["errors"])


def _anchored_v2_repo(
    root: Path,
    *,
    use_builder: bool = False,
    use_integration_reviews: bool = False,
    surface_retention_overrides: dict[str, str] | None = None,
    reviewed_symlink: bool = False,
) -> str:
    integration_base = _init_repo(root)
    _, ship_commit = _make_ship_commit(
        root,
        {
            "feature.py": "VALUE = 'ship'\n",
            "ops/autonomy/events.jsonl": '{"event_id":1}\n',
            "ops/autonomy/failure_ledger.jsonl": '{"failure_id":"F1"}\n',
            "ops/autonomy/progress.md": "- shipped\n",
            "ops/autonomy/autonomy_state.json": json.dumps(
                {
                    "active_run": None,
                    "active_swr_run": None,
                    "completed_slices": ["S04"],
                    "current_slice": None,
                    "last_event_id": 1,
                    "mode": "autonomous_zero_human",
                    "project": "test",
                    "run_history": [
                        {
                            "completed_at": "2026-01-01T00:00:00Z",
                            "integration_base_commit": integration_base,
                            "run_id": "RUN_S04",
                            "ship_branch": "ship/s04",
                            "ship_commit": "pending-test-value",
                            "slice": "S04",
                        }
                    ],
                    "schema_version": 1,
                    "v1_complete": False,
                },
                sort_keys=True,
            )
            + "\n",
            "ops/autonomy/slices.json": '[{"id":"S04","status":"complete"}]\n',
        },
    )

    _write(root, "feature.py", "VALUE = 'reconciled'\n")
    _write(root, "ops/autonomy/events.jsonl", '{"event_id":1}\n{"event_id":2}\n')
    _write(
        root,
        "ops/autonomy/failure_ledger.jsonl",
        '{"failure_id":"F1"}\n{"failure_id":"F2"}\n',
    )
    _write(root, "ops/autonomy/progress.md", "- shipped\n- reconciled\n")
    anchor_state = {
            "active_run": None,
            "active_swr_run": None,
            "completed_slices": ["S04"],
            "current_slice": None,
            "last_event_id": 2,
            "mode": "autonomous_zero_human",
            "project": "test",
            "run_history": [
                {
                    "completed_at": "2026-01-01T00:00:00Z",
                    "integration_base_commit": integration_base,
                    "run_id": "RUN_S04",
                    "ship_branch": "ship/s04",
                    "ship_commit": ship_commit,
                    "slice": "S04",
                }
            ],
            "schema_version": 1,
            "v1_complete": False,
        }
    _write_json(root, "ops/autonomy/autonomy_state.json", anchor_state)
    _write_review_bundle(root)
    _write(
        root,
        "ops/autonomy/schemas/slice_integration_receipt_v2.schema.json",
        (PROJECT_ROOT / "ops/autonomy/schemas/slice_integration_receipt_v2.schema.json").read_text(
            encoding="utf-8"
        ),
    )
    _write_slices(
        root,
        _slice_payload(
            ship_commit,
            integration_base_commit=integration_base,
            receipt=RECEIPT_PATH,
            reviews=(
                ["docs/reviews/s04-historical-review.md", REVIEW_PATH]
                if use_integration_reviews
                else [REVIEW_PATH]
            ),
            integration_reviews=[REVIEW_PATH] if use_integration_reviews else None,
        ),
    )
    reviewed_path = "feature.py"
    if reviewed_symlink:
        reviewed_path = "review-link.py"
        _write(root, "review-target.py", "REVIEWED = True\n")
        (root / reviewed_path).symlink_to("review-target.py")
    verification_commit = _commit_all(root, "freeze verified S04 continuation")
    evidence_payload = json.loads((root / EVIDENCE_PATH).read_text(encoding="utf-8"))
    evidence_payload["tested_commit"] = verification_commit
    reviewed_blob, reviewed_error = committed_bytes(
        root, verification_commit, reviewed_path
    )
    assert reviewed_error is None and reviewed_blob is not None
    evidence_payload["reviewed_files"] = [
        {
            "path": reviewed_path,
            "sha256": hashlib.sha256(reviewed_blob).hexdigest(),
        }
    ]
    _write_json(root, EVIDENCE_PATH, evidence_payload)

    if use_builder:
        receipt, receipt_path = build_receipt(root, "S04", verification_commit)
        assert receipt_path == RECEIPT_PATH
        _write_json(root, receipt_path, receipt)
        return _commit_all(root, "anchor builder-generated S04 receipt")

    changed_paths = _git(
        root,
        "diff",
        "--no-renames",
        "--name-only",
        integration_base,
        ship_commit,
    ).splitlines()
    retention_by_path = {
        "ops/autonomy/autonomy_state.json": "runtime_state",
        "ops/autonomy/events.jsonl": "append_only",
        "ops/autonomy/failure_ledger.jsonl": "append_only",
        "ops/autonomy/progress.md": "append_only",
        "ops/autonomy/slices.json": "slice_entry_stable",
    }
    surfaces: list[dict[str, object]] = []
    for path in sorted(changed_paths):
        ship_entry, error = git_tree_entry(root, ship_commit, path)
        assert error is None
        continuation_entry = _worktree_blob_entry(root, path)
        disposition = "identical" if ship_entry == continuation_entry else "evolved"
        row: dict[str, object] = {
            "path": path,
            "disposition": disposition,
            "retention": (surface_retention_overrides or {}).get(
                path, retention_by_path.get(path, "immutable")
            ),
            "ship_entry": ship_entry,
            "continuation_entry": continuation_entry,
        }
        if disposition == "evolved":
            row["reason"] = "The canonical continuation deliberately supersedes the ship blob."
        surfaces.append(row)
    _write_json(
        root,
        RECEIPT_PATH,
        {
            "schema_version": "autokeel.slice_integration.v2",
            "slice": "S04",
            "continuation_ref": "main",
            "ship_branch": "ship/s04",
            "ship_commit": ship_commit,
            "merge_base": integration_base,
            "verification_commit": verification_commit,
            "command_evidence_trust_boundary": COMMAND_EVIDENCE_TRUST_BOUNDARY,
            "audit_delta": sorted([EVIDENCE_PATH, RECEIPT_PATH]),
            "surfaces": surfaces,
            "verification_commands": [
                {
                    "command": command,
                    "evidence_path": EVIDENCE_PATH,
                    "exit_code": 0,
                    "status": "pass",
                }
                for command in ACCEPTANCE
            ],
            "review_artifacts": [
                {"path": REVIEW_PATH, "tree_entry": _worktree_blob_entry(root, REVIEW_PATH)}
            ],
            "command_evidence": [
                {"path": EVIDENCE_PATH, "tree_entry": _worktree_blob_entry(root, EVIDENCE_PATH)}
            ],
        },
    )
    return _commit_all(root, "anchor reconciled S04 receipt")


def _add_successor_receipt(
    root: Path,
    *,
    generation: int,
    prior_path: str,
    prior_anchor: str,
    link_predecessor: bool = True,
    strip_link_before_commit: bool = False,
    final_feature_text: str | None = None,
    final_events_text: str | None = None,
    final_runtime_last_event_id: int | None = None,
) -> tuple[str, str]:
    receipt_path = f"docs/evidence/s04-canonical-integration-v{generation}.json"
    review_path = f"docs/reviews/s04-autonomous-feature-review-v{generation}.md"
    evidence_path = f"docs/evidence/s04-integration-command-evidence-v{generation}.json"

    review_text = (root / REVIEW_PATH).read_text(encoding="utf-8")
    review_text = review_text.replace(EVIDENCE_PATH, evidence_path)
    _write(root, review_path, review_text)
    evidence = json.loads((root / EVIDENCE_PATH).read_text(encoding="utf-8"))
    evidence["tested_commit"] = "pending-successor-verification-commit"
    _write_json(root, evidence_path, evidence)
    slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
    slices[0]["integration_receipt"] = receipt_path
    slices[0]["integration_review_artifacts"] = [review_path]
    _write_json(root, "ops/autonomy/slices.json", slices)
    if final_feature_text is not None:
        _write(root, "feature.py", final_feature_text)
    if final_events_text is not None:
        _write(root, "ops/autonomy/events.jsonl", final_events_text)
    if final_runtime_last_event_id is not None:
        runtime_state = json.loads(
            (root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8")
        )
        runtime_state["last_event_id"] = final_runtime_last_event_id
        _write_json(root, "ops/autonomy/autonomy_state.json", runtime_state)
    verification_commit = _commit_all(root, f"freeze successor v{generation}")

    evidence["tested_commit"] = verification_commit
    evidence["reviewed_files"] = [
        {
            "path": "feature.py",
            "sha256": hashlib.sha256((root / "feature.py").read_bytes()).hexdigest(),
        }
    ]
    _write_json(root, evidence_path, evidence)
    receipt, generated_path = build_receipt(
        root,
        "S04",
        verification_commit,
        prior_path if link_predecessor else None,
    )
    assert generated_path == receipt_path
    assert receipt["supersedes_receipt"]["anchor_commit"] == prior_anchor
    if strip_link_before_commit:
        receipt.pop("supersedes_receipt")
    _write_json(root, receipt_path, receipt)
    anchor = _commit_all(root, f"anchor successor v{generation}")
    return receipt_path, anchor


def _advance_anchored_control_state(root: Path) -> None:
    events_path = root / "ops/autonomy/events.jsonl"
    events_path.write_text(events_path.read_text(encoding="utf-8") + '{"event_id":3}\n', encoding="utf-8")
    ledger_path = root / "ops/autonomy/failure_ledger.jsonl"
    ledger_path.write_text(
        ledger_path.read_text(encoding="utf-8") + '{"failure_id":"F3"}\n',
        encoding="utf-8",
    )
    progress_path = root / "ops/autonomy/progress.md"
    progress_path.write_text(
        progress_path.read_text(encoding="utf-8") + "- continued\n",
        encoding="utf-8",
    )
    state = json.loads((root / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
    state["completed_slices"].append("S05")
    state["last_event_id"] = 3
    state["run_history"].append({"run_id": "RUN_S05", "slice": "S05"})
    _write_json(root, "ops/autonomy/autonomy_state.json", state)
    slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
    slices.append({"id": "S05", "status": "complete"})
    _write_json(root, "ops/autonomy/slices.json", slices)
    _commit_all(root, "advance append-only control state")


def test_anchored_receipt_allows_only_declared_control_plane_evolution(tmp_path: Path) -> None:
    anchor = _anchored_v2_repo(tmp_path)
    initial = verify_slice_integration(tmp_path, "S04")
    _advance_anchored_control_state(tmp_path)

    report = verify_slice_integration(tmp_path, "S04")

    assert initial["status"] == "ok", initial
    assert report["status"] == "ok", report
    assert report["classification"] == "reconciled"
    assert report["checks"]["receipt_anchor_commit"] == anchor


def test_builder_creates_valid_evidence_bound_receipt(tmp_path: Path) -> None:
    anchor = _anchored_v2_repo(tmp_path, use_builder=True)

    report = verify_slice_integration(tmp_path, "S04")
    receipt = json.loads((tmp_path / RECEIPT_PATH).read_text(encoding="utf-8"))

    assert report["status"] == "ok", report
    assert report["checks"]["receipt_anchor_commit"] == anchor
    assert receipt["audit_delta"] == sorted([EVIDENCE_PATH, RECEIPT_PATH])
    assert all(
        row["evidence_path"] == EVIDENCE_PATH
        for row in receipt["verification_commands"]
    )


def test_anchored_receipt_uses_narrow_integration_review_artifacts(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path, use_integration_reviews=True)

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "ok", report
    assert report["classification"] == "reconciled"


def test_anchored_receipt_rejects_rewritten_append_only_prefix(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path)
    _write(tmp_path, "ops/autonomy/events.jsonl", '{"event_id":2}\n{"event_id":3}\n')
    state = json.loads((tmp_path / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
    state["last_event_id"] = 3
    _write_json(tmp_path, "ops/autonomy/autonomy_state.json", state)
    _commit_all(tmp_path, "rewrite event history")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("rewrote or removed anchored bytes" in error for error in report["errors"])


def test_anchored_receipt_rejects_historical_slice_row_mutation(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path)
    slices = json.loads((tmp_path / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
    slices[0]["name"] = "mutated after receipt"
    _write_json(tmp_path, "ops/autonomy/slices.json", slices)
    _commit_all(tmp_path, "rewrite historical slice")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("historical slice row changed" in error for error in report["errors"])


def test_anchored_receipt_rejects_later_immutable_product_change(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path)
    _write(tmp_path, "feature.py", "VALUE = 'changed after receipt'\n")
    _commit_all(tmp_path, "change immutable surface")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("immutable surface changed" in error for error in report["errors"])


@pytest.mark.parametrize("mutation", ["completed_slices", "run_history", "event_cursor"])
def test_anchored_receipt_rejects_runtime_history_regression(
    tmp_path: Path,
    mutation: str,
) -> None:
    _anchored_v2_repo(tmp_path)
    state = json.loads((tmp_path / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
    if mutation == "completed_slices":
        state["completed_slices"] = []
    elif mutation == "run_history":
        state["run_history"] = []
    else:
        state["last_event_id"] = 1
    _write_json(tmp_path, "ops/autonomy/autonomy_state.json", state)
    _commit_all(tmp_path, "regress runtime state")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    expected = {
        "completed_slices": "completed_slices",
        "run_history": "run_history",
        "event_cursor": "last_event_id",
    }[mutation]
    assert any(expected in error for error in report["errors"]), report


def test_anchored_receipt_path_cannot_be_rewritten(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path)
    receipt = json.loads((tmp_path / RECEIPT_PATH).read_text(encoding="utf-8"))
    receipt["verification_commands"][0]["status"] = "passed"
    _write_json(tmp_path, RECEIPT_PATH, receipt)
    _commit_all(tmp_path, "rewrite anchored receipt")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("exactly one immutable history touch" in error for error in report["errors"])


def test_anchored_receipt_rejects_changed_transitive_command_evidence(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path)
    evidence = json.loads((tmp_path / EVIDENCE_PATH).read_text(encoding="utf-8"))
    evidence["status"] = "error"
    _write_json(tmp_path, EVIDENCE_PATH, evidence)
    _commit_all(tmp_path, "rewrite command evidence")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("command evidence changed after receipt" in error for error in report["errors"])


def test_anchored_receipt_rejects_command_evidence_row_error_status(
    tmp_path: Path,
) -> None:
    _anchored_v2_repo(tmp_path)
    evidence = json.loads((tmp_path / EVIDENCE_PATH).read_text(encoding="utf-8"))
    evidence["commands"][0]["status"] = "error"
    _write_json(tmp_path, EVIDENCE_PATH, evidence)
    receipt = json.loads((tmp_path / RECEIPT_PATH).read_text(encoding="utf-8"))
    receipt["command_evidence"][0]["tree_entry"] = _worktree_blob_entry(
        tmp_path, EVIDENCE_PATH
    )
    _write_json(tmp_path, RECEIPT_PATH, receipt)
    _git(tmp_path, "add", EVIDENCE_PATH, RECEIPT_PATH)
    _git(tmp_path, "commit", "--amend", "--no-edit")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("non-passing status" in error for error in report["errors"]), report


def test_anchored_receipt_rejects_transient_command_evidence_rewrite(
    tmp_path: Path,
) -> None:
    _anchored_v2_repo(tmp_path)
    evidence_path = tmp_path / EVIDENCE_PATH
    anchored = evidence_path.read_text(encoding="utf-8")
    evidence = json.loads(anchored)
    evidence["status"] = "error"
    _write_json(tmp_path, EVIDENCE_PATH, evidence)
    _commit_all(tmp_path, "transiently rewrite command evidence")
    _write(tmp_path, EVIDENCE_PATH, anchored)
    _commit_all(tmp_path, "restore command evidence bytes")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("first-parent transition" in error for error in report["errors"])


def test_anchored_receipt_rejects_duplicate_slice_ids(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path)
    slices = json.loads((tmp_path / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
    slices.append(dict(slices[0]))
    _write_json(tmp_path, "ops/autonomy/slices.json", slices)
    _commit_all(tmp_path, "duplicate historical slice")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("duplicate slice id" in error for error in report["errors"])


def test_anchored_receipt_rejects_duplicate_json_key_in_appended_event(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path)
    events = tmp_path / "ops/autonomy/events.jsonl"
    events.write_text(
        events.read_text(encoding="utf-8") + '{"event_id":3,"event_id":4}\n',
        encoding="utf-8",
    )
    state = json.loads((tmp_path / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
    state["last_event_id"] = 4
    _write_json(tmp_path, "ops/autonomy/autonomy_state.json", state)
    _commit_all(tmp_path, "append malformed event")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("duplicate object key" in error for error in report["errors"])


def test_anchored_receipt_rejects_append_without_complete_final_line(
    tmp_path: Path,
) -> None:
    _anchored_v2_repo(tmp_path)
    events = tmp_path / "ops/autonomy/events.jsonl"
    events.write_bytes(events.read_bytes() + b'{"event_id":3}')
    state = json.loads((tmp_path / "ops/autonomy/autonomy_state.json").read_text(encoding="utf-8"))
    state["last_event_id"] = 3
    _write_json(tmp_path, "ops/autonomy/autonomy_state.json", state)
    _commit_all(tmp_path, "append incomplete event line")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("continuation does not end at a complete line" in error for error in report["errors"])


def test_anchored_receipt_rejects_append_only_mode_change(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path)
    (tmp_path / "ops/autonomy/events.jsonl").chmod(0o755)
    _commit_all(tmp_path, "make event log executable")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("changed type or mode" in error for error in report["errors"])


def test_configured_receipt_is_authoritative_before_ancestor_success(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _, ship_commit = _make_ship_commit(tmp_path, {"landed.py": "LANDED = True\n"})
    _git(tmp_path, "merge", "--no-ff", "--no-edit", "ship/s04")
    _write_slices(
        tmp_path,
        _slice_payload(
            ship_commit,
            receipt="docs/evidence/missing-authoritative-receipt.json",
            reviews=[REVIEW_PATH],
        ),
    )
    _commit_all(tmp_path, "configure authoritative missing receipt")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["checks"]["ancestor"] is True
    assert report["checks"]["surface_retention"]["retained"] is True
    assert report["status"] == "error"
    assert any("missing-authoritative-receipt" in error for error in report["errors"])


def test_configured_receipt_rejects_scripts_namespace(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    _, ship_commit = _make_ship_commit(tmp_path, {"landed.py": "LANDED = True\n"})
    _git(tmp_path, "merge", "--no-ff", "--no-edit", "ship/s04")
    _write_slices(
        tmp_path,
        _slice_payload(
            ship_commit,
            receipt="scripts/foo.json",
            reviews=[REVIEW_PATH],
        ),
    )
    _write(tmp_path, "scripts/foo.json", "{}\n")
    _commit_all(tmp_path, "configure receipt outside evidence namespace")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("directly under docs/evidence" in error for error in report["errors"])


def test_receipt_override_must_equal_committed_configuration(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path)

    report = verify_slice_integration(
        tmp_path,
        "S04",
        receipt_path="docs/evidence/unregistered-override.json",
    )

    assert report["status"] == "error"
    assert any("override differs" in error for error in report["errors"])


def test_receipt_override_is_rejected_when_committed_configuration_is_absent(
    tmp_path: Path,
) -> None:
    _init_repo(tmp_path)
    _, ship_commit = _make_ship_commit(tmp_path, {"landed.py": "LANDED = True\n"})
    _git(tmp_path, "merge", "--no-ff", "--no-edit", "ship/s04")
    _write_slices(tmp_path, _slice_payload(ship_commit))
    _commit_all(tmp_path, "record slice without integration receipt")

    report = verify_slice_integration(
        tmp_path,
        "S04",
        receipt_path="docs/evidence/unregistered-override.json",
    )

    assert report["status"] == "error"
    assert any("override differs" in error for error in report["errors"])


@pytest.mark.parametrize("artifact", ["review", "evidence"])
def test_anchored_receipt_rejects_scripts_artifact_namespace(
    tmp_path: Path,
    artifact: str,
) -> None:
    _anchored_v2_repo(tmp_path)
    receipt = json.loads((tmp_path / RECEIPT_PATH).read_text(encoding="utf-8"))
    if artifact == "review":
        receipt["review_artifacts"][0]["path"] = "scripts/foo.md"
    else:
        receipt["command_evidence"][0]["path"] = "scripts/foo.json"
        receipt["verification_commands"][0]["evidence_path"] = "scripts/foo.json"
    _write_json(tmp_path, RECEIPT_PATH, receipt)
    _git(tmp_path, "add", RECEIPT_PATH)
    _git(tmp_path, "commit", "--amend", "--no-edit")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    expected = "docs/reviews" if artifact == "review" else "docs/evidence"
    assert any(expected in error for error in report["errors"]), report


def test_anchored_receipt_and_command_evidence_must_be_distinct(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path)
    receipt = json.loads((tmp_path / RECEIPT_PATH).read_text(encoding="utf-8"))
    receipt["command_evidence"][0]["path"] = RECEIPT_PATH
    receipt["verification_commands"][0]["evidence_path"] = RECEIPT_PATH
    _write_json(tmp_path, RECEIPT_PATH, receipt)
    _git(tmp_path, "add", RECEIPT_PATH)
    _git(tmp_path, "commit", "--amend", "--no-edit")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("must be distinct" in error for error in report["errors"]), report


def test_anchored_receipt_rejects_transient_rewrite_then_restore(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path)
    events = tmp_path / "ops/autonomy/events.jsonl"
    anchored_events = events.read_text(encoding="utf-8")
    _write(tmp_path, "ops/autonomy/events.jsonl", '{"event_id":2}\n')
    _commit_all(tmp_path, "transiently rewrite event history")
    _write(tmp_path, "ops/autonomy/events.jsonl", anchored_events)
    _commit_all(tmp_path, "restore event history bytes")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("rewrote or removed anchored bytes" in error for error in report["errors"])


@pytest.mark.parametrize("surface", ["runtime", "slices"])
def test_anchored_receipt_rejects_transient_control_state_rewrite_then_restore(
    tmp_path: Path,
    surface: str,
) -> None:
    _anchored_v2_repo(tmp_path)
    if surface == "runtime":
        path = "ops/autonomy/autonomy_state.json"
        anchored = (tmp_path / path).read_text(encoding="utf-8")
        state = json.loads(anchored)
        state["completed_slices"] = []
        _write_json(tmp_path, path, state)
    else:
        path = "ops/autonomy/slices.json"
        anchored = (tmp_path / path).read_text(encoding="utf-8")
        slices = json.loads(anchored)
        slices[0]["name"] = "transient rewrite"
        _write_json(tmp_path, path, slices)
    _commit_all(tmp_path, f"transiently rewrite {surface} history")
    _write(tmp_path, path, anchored)
    _commit_all(tmp_path, f"restore {surface} history bytes")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    expected = "completed_slices" if surface == "runtime" else "historical slice row changed"
    assert any(expected in error for error in report["errors"]), report


def test_anchored_receipt_rejects_hidden_audit_commit_change(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path)
    _write(tmp_path, "hidden-control-change.py", "HIDDEN = True\n")
    _git(tmp_path, "add", "hidden-control-change.py")
    _git(tmp_path, "commit", "--amend", "--no-edit")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("audit_delta does not exactly bind" in error for error in report["errors"])
    assert any("implementation/control changes" in error for error in report["errors"])


def test_anchored_receipt_requires_regular_100644_receipt_blob(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path)
    (tmp_path / RECEIPT_PATH).chmod(0o755)
    _git(tmp_path, "add", RECEIPT_PATH)
    _git(tmp_path, "commit", "--amend", "--no-edit")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("receipt must be a regular 100644 blob" in error for error in report["errors"])


def test_anchored_receipt_requires_explicit_command_evidence_trust_boundary(
    tmp_path: Path,
) -> None:
    _anchored_v2_repo(tmp_path)
    receipt = json.loads((tmp_path / RECEIPT_PATH).read_text(encoding="utf-8"))
    receipt["command_evidence_trust_boundary"]["runner_attestation"] = "verified"
    _write_json(tmp_path, RECEIPT_PATH, receipt)
    _git(tmp_path, "add", RECEIPT_PATH)
    _git(tmp_path, "commit", "--amend", "--no-edit")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("without cryptographic runner proof" in error for error in report["errors"])


@pytest.mark.parametrize(
    ("path", "message"),
    [
        (REVIEW_PATH, "review artifact must be a regular 100644 blob"),
        (EVIDENCE_PATH, "command evidence must be a regular 100644 blob"),
    ],
)
def test_anchored_receipt_requires_regular_100644_audit_artifacts(
    tmp_path: Path,
    path: str,
    message: str,
) -> None:
    _anchored_v2_repo(tmp_path)
    (tmp_path / path).chmod(0o755)
    _commit_all(tmp_path, "make audit artifact executable")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any(message in error for error in report["errors"]), report


def test_anchored_verification_rejects_replace_refs(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path)
    slices = json.loads((tmp_path / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
    ship_commit = str(slices[0]["ship_commit"])
    integration_base = str(slices[0]["integration_base_commit"])
    _git(tmp_path, "replace", ship_commit, integration_base)

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("replace refs" in error for error in report["errors"])


@pytest.mark.parametrize("mechanism", ["grafts", "alternates"])
def test_anchored_verification_rejects_redirecting_object_graph_files(
    tmp_path: Path,
    mechanism: str,
) -> None:
    _anchored_v2_repo(tmp_path)
    if mechanism == "grafts":
        slices = json.loads(
            (tmp_path / "ops/autonomy/slices.json").read_text(encoding="utf-8")
        )
        path = tmp_path / ".git/info/grafts"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"{slices[0]['ship_commit']} {slices[0]['integration_base_commit']}\n",
            encoding="utf-8",
        )
    else:
        rogue = tmp_path / "rogue"
        rogue.mkdir()
        _init_repo(rogue)
        path = tmp_path / ".git/objects/info/alternates"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str((rogue / ".git/objects").resolve()) + "\n", encoding="utf-8")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any(mechanism.rstrip("s") in error for error in report["errors"]), report


def test_git_redirect_environment_is_scrubbed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    expected = tmp_path / "expected"
    rogue = tmp_path / "rogue"
    expected.mkdir()
    rogue.mkdir()
    _anchored_v2_repo(expected)
    _init_repo(rogue)
    monkeypatch.setenv("GIT_DIR", str(rogue / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(rogue))

    report = verify_slice_integration(expected, "S04")

    assert report["status"] == "ok", report


def test_repository_security_rejects_shallow_history(tmp_path: Path) -> None:
    source = tmp_path / "source"
    shallow = tmp_path / "shallow"
    source.mkdir()
    _init_repo(source)
    result = subprocess.run(
        ["git", "clone", "--depth=1", "--no-local", str(source), str(shallow)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    assert result.returncode == 0, result.stderr

    errors = validate_repository(shallow)

    assert any("shallow repositories" in error for error in errors), errors


@pytest.mark.parametrize("head_state", ["side_branch", "detached"])
def test_repository_security_requires_symbolic_main_head(
    tmp_path: Path,
    head_state: str,
) -> None:
    main_commit = _init_repo(tmp_path)
    if head_state == "side_branch":
        _git(tmp_path, "checkout", "-b", "side")
    else:
        _git(tmp_path, "checkout", "--detach", main_commit)

    errors = validate_repository(tmp_path, expected_main_commit=main_commit)

    expected = "refs/heads/main" if head_state == "side_branch" else "detached HEAD"
    assert any(expected in error for error in errors), errors


def test_repository_security_rejects_separate_worktree_gitdir(tmp_path: Path) -> None:
    source = tmp_path / "source"
    linked = tmp_path / "linked"
    source.mkdir()
    _init_repo(source)
    _git(source, "worktree", "add", "-b", "linked-main", str(linked))

    errors = validate_repository(linked)

    assert any("canonical root/.git directory" in error for error in errors), errors


def test_repository_security_pins_main_to_expected_commit(tmp_path: Path) -> None:
    verification_commit = _init_repo(tmp_path)
    _write(tmp_path, "later.py", "LATER = True\n")
    _commit_all(tmp_path, "advance main")

    errors = validate_repository(
        tmp_path, expected_main_commit=verification_commit
    )

    assert any("does not match the expected verification commit" in error for error in errors)


def test_anchored_verifier_rejects_side_branch_head(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path)
    _git(tmp_path, "checkout", "-b", "side")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("symbolic HEAD refs/heads/main" in error for error in report["errors"])


def test_builder_rejects_nonpassing_real_command_evidence(tmp_path: Path) -> None:
    verification_commit = _init_repo(tmp_path)
    feature_hash = hashlib.sha256((tmp_path / "feature.py").read_bytes()).hexdigest()
    _write_json(
        tmp_path,
        EVIDENCE_PATH,
        {
            "slice": "S04",
            "status": "ok",
            "tested_commit": verification_commit,
            "reviewed_files": [{"path": "feature.py", "sha256": feature_hash}],
            "commands": [{"command": ACCEPTANCE[0], "exit_code": 1}],
        },
    )

    with pytest.raises(ValueError, match="did not pass"):
        _real_verification_rows(
            tmp_path,
            slice_id="S04",
            verification_commit=verification_commit,
            acceptance=[ACCEPTANCE[0]],
            evidence_paths=[EVIDENCE_PATH],
        )


def test_builder_rejects_symlink_reviewed_file(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="regular 100644 committed blob"):
        _anchored_v2_repo(
            tmp_path,
            use_builder=True,
            reviewed_symlink=True,
        )


def test_anchored_verifier_rejects_symlink_reviewed_file(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path, reviewed_symlink=True)

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any(
        "reviewed file is not a regular 100644 committed blob" in error
        for error in report["errors"]
    ), report


def test_successor_chain_rejects_changed_slice_identity(tmp_path: Path) -> None:
    prior_anchor = _anchored_v2_repo(tmp_path)
    prior_receipt = json.loads((tmp_path / RECEIPT_PATH).read_text(encoding="utf-8"))
    prior_entry, entry_error = git_tree_entry(tmp_path, prior_anchor, RECEIPT_PATH)
    assert entry_error is None
    supersedes = {
        "path": RECEIPT_PATH,
        "anchor_commit": prior_anchor,
        "tree_entry": prior_entry,
    }
    identity = {
        field: prior_receipt[field]
        for field in ("slice", "ship_branch", "ship_commit", "merge_base")
    }

    mismatched_errors = _validate_supersedes_chain(
        tmp_path,
        supersedes=supersedes,
        identity={**identity, "ship_commit": identity["merge_base"]},
        child_verification_commit=prior_anchor,
        continuation_commit=prior_anchor,
        seen_paths={"docs/evidence/s04-successor.json"},
    )

    assert any("identity field ship_commit" in error for error in mismatched_errors)


def test_successor_chain_rejects_malformed_predecessor_full_schema(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path)
    prior_receipt = json.loads((tmp_path / RECEIPT_PATH).read_text(encoding="utf-8"))
    prior_receipt.pop("audit_delta")
    _write_json(tmp_path, RECEIPT_PATH, prior_receipt)
    _git(tmp_path, "add", RECEIPT_PATH)
    _git(tmp_path, "commit", "--amend", "--no-edit")
    prior_anchor = _git(tmp_path, "rev-parse", "HEAD")
    prior_entry, entry_error = git_tree_entry(tmp_path, prior_anchor, RECEIPT_PATH)
    assert entry_error is None
    identity = {
        field: prior_receipt[field]
        for field in ("slice", "ship_branch", "ship_commit", "merge_base")
    }

    errors = _validate_supersedes_chain(
        tmp_path,
        supersedes={
            "path": RECEIPT_PATH,
            "anchor_commit": prior_anchor,
            "tree_entry": prior_entry,
        },
        identity=identity,
        child_verification_commit=prior_anchor,
        continuation_commit=prior_anchor,
        seen_paths={"docs/evidence/s04-successor.json"},
    )

    assert any("audit_delta" in error for error in errors), errors


def test_multihop_successor_chain_validates_every_predecessor(tmp_path: Path) -> None:
    first_anchor = _anchored_v2_repo(tmp_path, use_builder=True)
    second_path, second_anchor = _add_successor_receipt(
        tmp_path,
        generation=3,
        prior_path=RECEIPT_PATH,
        prior_anchor=first_anchor,
    )
    third_path, third_anchor = _add_successor_receipt(
        tmp_path,
        generation=4,
        prior_path=second_path,
        prior_anchor=second_anchor,
    )

    report = verify_slice_integration(tmp_path, "S04")
    third_receipt = json.loads((tmp_path / third_path).read_text(encoding="utf-8"))

    assert report["status"] == "ok", report
    assert report["checks"]["receipt_anchor_commit"] == third_anchor
    assert third_receipt["supersedes_receipt"]["path"] == second_path


def test_multihop_successor_chain_cannot_bypass_deep_artifact_rewrite(
    tmp_path: Path,
) -> None:
    first_anchor = _anchored_v2_repo(tmp_path, use_builder=True)
    second_path, second_anchor = _add_successor_receipt(
        tmp_path,
        generation=3,
        prior_path=RECEIPT_PATH,
        prior_anchor=first_anchor,
    )
    _add_successor_receipt(
        tmp_path,
        generation=4,
        prior_path=second_path,
        prior_anchor=second_anchor,
    )
    evidence = json.loads((tmp_path / EVIDENCE_PATH).read_text(encoding="utf-8"))
    evidence["status"] = "error"
    _write_json(tmp_path, EVIDENCE_PATH, evidence)
    _commit_all(tmp_path, "tamper with first-generation evidence")

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any("superseded command evidence" in error for error in report["errors"]), report


def test_builder_refuses_new_root_when_prior_receipt_exists(tmp_path: Path) -> None:
    first_anchor = _anchored_v2_repo(tmp_path, use_builder=True)

    with pytest.raises(ValueError, match="new root v2 receipt is forbidden"):
        _add_successor_receipt(
            tmp_path,
            generation=3,
            prior_path=RECEIPT_PATH,
            prior_anchor=first_anchor,
            link_predecessor=False,
        )


def test_verifier_rejects_unlinked_successor_pointer_reset(tmp_path: Path) -> None:
    first_anchor = _anchored_v2_repo(tmp_path, use_builder=True)
    _add_successor_receipt(
        tmp_path,
        generation=3,
        prior_path=RECEIPT_PATH,
        prior_anchor=first_anchor,
        strip_link_before_commit=True,
    )

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any(
        "exactly one root" in error or "disconnected/reset history" in error
        for error in report["errors"]
    ), report


def test_successor_rejects_transient_predecessor_event_rewrite_restore(
    tmp_path: Path,
) -> None:
    first_anchor = _anchored_v2_repo(tmp_path, use_builder=True)
    events_path = tmp_path / "ops/autonomy/events.jsonl"
    anchored_events = events_path.read_text(encoding="utf-8")
    _write(tmp_path, "ops/autonomy/events.jsonl", '{"event_id":2}\n')
    _commit_all(tmp_path, "rewrite predecessor event prefix")
    _write(tmp_path, "ops/autonomy/events.jsonl", anchored_events)
    _commit_all(tmp_path, "restore predecessor event prefix")

    with pytest.raises(ValueError, match="rewrote or removed anchored bytes"):
        _add_successor_receipt(
            tmp_path,
            generation=3,
            prior_path=RECEIPT_PATH,
            prior_anchor=first_anchor,
        )


def test_successor_allows_final_reviewed_immutable_evolution(tmp_path: Path) -> None:
    first_anchor = _anchored_v2_repo(tmp_path, use_builder=True)
    _add_successor_receipt(
        tmp_path,
        generation=3,
        prior_path=RECEIPT_PATH,
        prior_anchor=first_anchor,
        final_feature_text="VALUE = 'successor recertified evolution'\n",
    )

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "ok", report


def test_successor_rejects_append_only_rewrite_at_final_freeze(tmp_path: Path) -> None:
    first_anchor = _anchored_v2_repo(tmp_path, use_builder=True)

    with pytest.raises(ValueError, match="rewrote or removed anchored bytes"):
        _add_successor_receipt(
            tmp_path,
            generation=3,
            prior_path=RECEIPT_PATH,
            prior_anchor=first_anchor,
            final_events_text='{"event_id":2}\n',
        )


def test_successor_rejects_runtime_regression_at_final_freeze(tmp_path: Path) -> None:
    first_anchor = _anchored_v2_repo(tmp_path, use_builder=True)

    with pytest.raises(ValueError, match="last_event_id regressed"):
        _add_successor_receipt(
            tmp_path,
            generation=3,
            prior_path=RECEIPT_PATH,
            prior_anchor=first_anchor,
            final_runtime_last_event_id=1,
        )


@pytest.mark.parametrize(
    ("path", "wrong_retention", "required_retention"),
    [
        ("ops/autonomy/events.jsonl", "immutable", "append_only"),
        ("ops/autonomy/autonomy_state.json", "immutable", "runtime_state"),
    ],
)
def test_anchored_receipt_rejects_mislabeled_control_surface_retention(
    tmp_path: Path,
    path: str,
    wrong_retention: str,
    required_retention: str,
) -> None:
    _anchored_v2_repo(
        tmp_path,
        surface_retention_overrides={path: wrong_retention},
    )

    report = verify_slice_integration(tmp_path, "S04")

    assert report["status"] == "error"
    assert any(
        f"surface retention mismatch: {path}" in error
        and required_retention in error
        for error in report["errors"]
    ), report


def test_successor_cannot_use_mislabeled_event_surface_to_reset_log(
    tmp_path: Path,
) -> None:
    first_anchor = _anchored_v2_repo(
        tmp_path,
        surface_retention_overrides={"ops/autonomy/events.jsonl": "immutable"},
    )

    with pytest.raises(ValueError, match="surface retention mismatch"):
        _add_successor_receipt(
            tmp_path,
            generation=3,
            prior_path=RECEIPT_PATH,
            prior_anchor=first_anchor,
            final_events_text='{"event_id":2}\n',
        )


def test_successor_rejects_immutable_evolution_before_freeze(tmp_path: Path) -> None:
    first_anchor = _anchored_v2_repo(tmp_path, use_builder=True)
    _write(tmp_path, "feature.py", "VALUE = 'too early'\n")
    _commit_all(tmp_path, "evolve immutable surface before successor freeze")

    with pytest.raises(ValueError, match="immutable surface changed"):
        _add_successor_receipt(
            tmp_path,
            generation=3,
            prior_path=RECEIPT_PATH,
            prior_anchor=first_anchor,
        )


def test_successor_rejects_long_dangling_pointer_before_freeze(tmp_path: Path) -> None:
    first_anchor = _anchored_v2_repo(tmp_path, use_builder=True)
    slices_path = tmp_path / "ops/autonomy/slices.json"
    anchored_slices = slices_path.read_text(encoding="utf-8")
    slices = json.loads(anchored_slices)
    slices[0]["integration_receipt"] = "docs/evidence/s04-canonical-integration-v3.json"
    _write_json(tmp_path, "ops/autonomy/slices.json", slices)
    _commit_all(tmp_path, "point at nonexistent future receipt")
    _write(tmp_path, "ops/autonomy/slices.json", anchored_slices)
    _commit_all(tmp_path, "restore predecessor pointer before freeze")

    with pytest.raises(ValueError, match="pointer changed before"):
        _add_successor_receipt(
            tmp_path,
            generation=3,
            prior_path=RECEIPT_PATH,
            prior_anchor=first_anchor,
        )


def test_builder_registry_rejects_reset_with_changed_ship_identity(tmp_path: Path) -> None:
    _anchored_v2_repo(tmp_path, use_builder=True)
    slices = json.loads((tmp_path / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
    integration_base = str(slices[0]["integration_base_commit"])
    _git(tmp_path, "checkout", "-b", "altered-ship", integration_base)
    _write(tmp_path, "altered.py", "ALTERED = True\n")
    altered_ship = _commit_all(tmp_path, "alternate ship identity")
    _git(tmp_path, "checkout", "main")
    _git(tmp_path, "branch", "-f", "ship/s04", altered_ship)
    slices[0]["ship_commit"] = altered_ship
    slices[0]["integration_receipt"] = "docs/evidence/s04-reset-root.json"
    _write_json(tmp_path, "ops/autonomy/slices.json", slices)
    reset_commit = _commit_all(tmp_path, "attempt receipt reset with changed ship identity")

    with pytest.raises(ValueError, match="slice identity drift"):
        build_receipt(tmp_path, "S04", reset_commit)

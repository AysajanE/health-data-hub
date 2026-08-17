from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path

import pytest

from ops.autonomy.autokeel import AutoKeel, AutoKeelError
from scripts.verify_failure_ledger import effective_failure_rows, verify_failure_ledger


def evidence(root: Path) -> tuple[str, str]:
    rel = "docs/evidence/owner-scope-decision.json"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"status":"ok"}\n', encoding="utf-8")
    return rel, hashlib.sha256(path.read_bytes()).hexdigest()


def base_row(rel: str, sha: str) -> dict[str, object]:
    return {
        "schema_version": "autokeel.failure_ledger.v2",
        "failure_id": "failure_original",
        "ts": "2026-08-16T16:00:00-04:00",
        "slice": "S12",
        "run_id": None,
        "failure_class": "provider_terms_conflict",
        "severity": "high",
        "description": "Historical project stop.",
        "action_taken": "Stopped under the former policy.",
        "evidence_path": rel,
        "evidence_sha256": sha,
        "root_cause_id": "S12-OURA-PROVIDER-TERMS-AUTHORITY",
        "failure_origin": "external_provider",
        "supersedes": [],
        "superseded_by": None,
        "false_positive": False,
        "closure_validation_command": "",
        "open": True,
    }


def successor(rel: str, sha: str) -> dict[str, object]:
    return {
        "schema_version": "autokeel.failure_ledger.v2",
        "failure_id": "failure_retired_by_owner_scope",
        "ts": "2026-08-17T06:00:00-04:00",
        "slice": "S12",
        "run_id": None,
        "failure_class": "provider_terms_conflict",
        "severity": "high",
        "description": "The owner retired the project-enforced outside stop.",
        "action_taken": "S12 returned to pending product work.",
        "evidence_path": rel,
        "evidence_sha256": sha,
        "root_cause_id": "S12-OURA-PROVIDER-TERMS-AUTHORITY",
        "failure_origin": "owner_scope_decision",
        "supersedes": ["failure_original"],
        "superseded_by": None,
        "false_positive": False,
        "closure_validation_command": "python scripts/verify_failure_ledger.py --json",
        "open": False,
        "closure_evidence": rel,
        "closure_note": "Former project policy retired; no provider authorization claim is made.",
    }


def write_ledger(root: Path, rows: list[dict[str, object]]) -> None:
    path = root / "ops/autonomy/failure_ledger.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(row, sort_keys=True) + "\n" for row in rows), encoding="utf-8")


def test_valid_closed_successor_retires_open_row_without_rewriting_history() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        rel, sha = evidence(root)
        original = base_row(rel, sha)
        closed = successor(rel, sha)
        write_ledger(root, [original, closed])

        report = verify_failure_ledger(root)
        effective, errors = effective_failure_rows(root, [original, closed])

    assert errors == []
    assert effective == [closed]
    assert report["status"] == "ok", report
    assert report["rows"] == 2
    assert report["effective_rows"] == 1


def test_successor_without_unique_nonempty_id_cannot_retire_row() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        rel, sha = evidence(root)
        original = base_row(rel, sha)
        closed = successor(rel, sha)
        closed["failure_id"] = ""

        effective, errors = effective_failure_rows(root, [original, closed])

    assert original in effective
    assert any("non-empty failure_id" in error for error in errors)


def test_mismatched_successor_identity_cannot_retire_row() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        rel, sha = evidence(root)
        original = base_row(rel, sha)
        closed = successor(rel, sha)
        closed["root_cause_id"] = "DIFFERENT"

        effective, errors = effective_failure_rows(root, [original, closed])

    assert original in effective
    assert any("root_cause_id does not match" in error for error in errors)


def test_two_successors_cannot_retire_one_row_ambiguously() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        rel, sha = evidence(root)
        original = base_row(rel, sha)
        first = successor(rel, sha)
        second = successor(rel, sha)
        second["failure_id"] = "failure_second_successor"

        effective, errors = effective_failure_rows(root, [original, first, second])

    assert original in effective
    assert any("multiple successors" in error for error in errors)


def test_unbound_closure_evidence_cannot_retire_row() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        rel, sha = evidence(root)
        other = root / "docs/evidence/other.json"
        other.write_text('{"status":"ok"}\n', encoding="utf-8")
        original = base_row(rel, sha)
        closed = successor(rel, sha)
        closed["closure_evidence"] = "docs/evidence/other.json"

        effective, errors = effective_failure_rows(root, [original, closed])

    assert original in effective
    assert any("must equal its hash-bound evidence_path" in error for error in errors)


def test_multi_target_successor_is_atomic_and_retires_nothing() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        rel, sha = evidence(root)
        first = base_row(rel, sha)
        second = base_row(rel, sha)
        second["failure_id"] = "failure_second"
        closed = successor(rel, sha)
        closed["supersedes"] = ["failure_original", "failure_second"]

        effective, errors = effective_failure_rows(root, [first, second, closed])

    assert first in effective
    assert second in effective
    assert any("must supersede exactly one failure" in error for error in errors)


def test_autokeel_stops_on_invalid_successor_history() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        rel, sha = evidence(root)
        first = base_row(rel, sha)
        second = base_row(rel, sha)
        second["failure_id"] = "failure_second"
        closed = successor(rel, sha)
        closed["supersedes"] = ["failure_original", "failure_second"]
        write_ledger(root, [first, second, closed])
        operator = AutoKeel.__new__(AutoKeel)
        operator.root = root
        operator.failure_path = root / "ops/autonomy/failure_ledger.jsonl"

        with pytest.raises(AutoKeelError, match="invalid failure-ledger successor history"):
            operator.effective_failure_rows()

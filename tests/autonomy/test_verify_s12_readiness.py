from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from scripts.verify_s12_readiness import (
    AUTOPLAN_REL,
    BRIEF_REL,
    POLICY_REL,
    SLICES_REL,
    TEST_REL,
    VERIFIER_REL,
    run_s12_sync_proof,
    verify_s12_continuity,
    verify_s12_readiness,
)


ROOT = Path(__file__).resolve().parents[2]


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def init_repo(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=root, check=True)
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "fixture"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def make_fixture(root: Path, *, s11_status: str = "complete") -> None:
    for rel in (BRIEF_REL, AUTOPLAN_REL, VERIFIER_REL, TEST_REL, POLICY_REL):
        source = ROOT / rel
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)

    source_slices = json.loads((ROOT / SLICES_REL).read_text(encoding="utf-8"))
    wanted = []
    for item in source_slices:
        if item.get("id") not in {"S03", "S04", "S05", "S11", "S12"}:
            continue
        copied = dict(item)
        if copied["id"] in {"S03", "S04", "S05"}:
            copied["status"] = "complete"
        elif copied["id"] == "S11":
            copied["status"] = s11_status
        else:
            copied["status"] = "pending"
        wanted.append(copied)
    write_json(root / SLICES_REL, wanted)
    init_repo(root)


def test_green_technical_readiness_uses_no_network_credentials_or_private_evidence() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root)

        report = verify_s12_readiness(root)

    assert report["status"] == "ok", report
    assert report["errors"] == []
    assert report["checks"]["network_accessed"] is False
    assert report["checks"]["credentials_inspected"] is False
    assert report["checks"]["private_evidence_read"] is False
    assert report["checks"]["lane"] == "compiler"


def test_incomplete_s11_dependency_blocks_launch() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root, s11_status="pending")

        report = verify_s12_readiness(root)

    assert report["status"] == "error"
    assert "S11 must be complete before S12 launch: pending" in report["errors"]


def test_dirty_or_untracked_control_input_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root)
        (root / BRIEF_REL).write_text((root / BRIEF_REL).read_text(encoding="utf-8") + "\nchanged\n", encoding="utf-8")

        report = verify_s12_readiness(root)

    assert report["status"] == "error"
    assert any(BRIEF_REL in error and "byte-identical to HEAD" in error for error in report["errors"])


def test_missing_contract_document_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root)
        (root / AUTOPLAN_REL).unlink()

        report = verify_s12_readiness(root)

    assert report["status"] == "error"
    assert any("missing S12 contract document" in error for error in report["errors"])


def test_registry_rejects_obsolete_external_blocker_shape() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root)
        slices = json.loads((root / SLICES_REL).read_text(encoding="utf-8"))
        s12 = next(item for item in slices if item["id"] == "S12")
        s12["lane"] = "compiler_external_evidence"
        s12["reason"] = "obsolete external blocker"
        write_json(root / SLICES_REL, slices)
        subprocess.run(["git", "add", SLICES_REL], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "old shape"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        report = verify_s12_readiness(root)

    assert report["status"] == "error"
    assert "S12 lane must be compiler" in report["errors"]
    assert "S12 must not retain obsolete blocker field: reason" in report["errors"]


def test_generated_files_scope_cannot_edit_readiness_controls() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root)
        autoplan = root / AUTOPLAN_REL
        autoplan.write_text(autoplan.read_text(encoding="utf-8") + f"\nFiles: `{VERIFIER_REL}`; `{TEST_REL}`\n", encoding="utf-8")
        subprocess.run(["git", "add", AUTOPLAN_REL], cwd=root, check=True)
        subprocess.run(["git", "commit", "-m", "unsafe scope"], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

        report = verify_s12_readiness(root)

    assert report["status"] == "error"
    assert any("readiness control appears in generated Files scope" in error for error in report["errors"])


def test_continuity_stops_before_sync_proof_when_readiness_fails() -> None:
    with patch(
        "scripts.verify_s12_readiness.verify_s12_readiness",
        return_value={"status": "error", "errors": ["S11 incomplete"], "checks": {}},
    ), patch("scripts.verify_s12_readiness.run_s12_sync_proof") as sync:
        report = verify_s12_continuity(Path("."))

    sync.assert_not_called()
    assert report["status"] == "error"
    assert report["checks"]["readiness_status"] == "error"
    assert report["checks"]["sync_proof_status"] == "not_run_readiness_failed"


def test_continuity_requires_current_sync_proof() -> None:
    with patch(
        "scripts.verify_s12_readiness.verify_s12_readiness",
        return_value={"status": "ok", "errors": [], "checks": {}},
    ), patch(
        "scripts.verify_s12_readiness.run_s12_sync_proof",
        return_value={"status": "blocked_external", "errors": ["aggregate evidence is stale"], "checks": {}},
    ):
        report = verify_s12_continuity(Path("."))

    assert report["status"] == "blocked_external"
    assert report["checks"]["readiness_status"] == "ok"
    assert report["checks"]["sync_proof_status"] == "blocked_external"
    assert report["errors"] == ["S12 current sync proof: aggregate evidence is stale"]


def test_sync_proof_does_not_execute_generated_code() -> None:
    report = run_s12_sync_proof(Path("."))

    assert report["status"] == "blocked_external"
    assert report["checks"]["generated_verifier_executed"] is False
    assert report["checks"]["network_accessed"] is False

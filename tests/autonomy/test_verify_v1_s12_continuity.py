from __future__ import annotations

import json
import tempfile
from pathlib import Path
from unittest.mock import patch

import scripts.verify_v1 as verifier


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload) + "\n", encoding="utf-8")


def make_fixture(root: Path, *, include_s12: bool = True) -> None:
    slices = []
    if include_s12:
        slices.append(
            {
                "id": "S12",
                "required": True,
                "status": "pending",
                "deliverables": [],
                "review_artifacts": [],
                "acceptance": [],
            }
        )
    write_json(root / "ops/autonomy/slices.json", slices)
    write_json(root / "ops/autonomy/autonomy_state.json", {"active_run": None})
    ledger = root / "ops/autonomy/failure_ledger.jsonl"
    ledger.parent.mkdir(parents=True, exist_ok=True)
    ledger.write_text("", encoding="utf-8")


def common_patches():
    return (
        patch.object(verifier, "validate_swr_lane_requirements", return_value=[]),
        patch.object(verifier, "check_review", return_value={"status": "ok", "errors": []}),
        patch.object(verifier, "check_no_tracked_data", return_value={"status": "ok", "errors": [], "warnings": []}),
        patch.object(verifier, "scan_ui_language", return_value=[]),
    )


def test_final_gate_revalidates_s12_readiness_even_when_acceptance_commands_are_skipped() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root)
        continuity = {
            "status": "error",
            "errors": ["S12 current readiness: S11 is incomplete"],
            "checks": {
                "readiness_status": "error",
                "sync_proof_status": "not_run_readiness_failed",
            },
        }
        patches = common_patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.object(verifier, "verify_s12_continuity", return_value=continuity) as current_gate,
        ):
            report = verifier.verify_v1(root, run_acceptance_commands=False)

    current_gate.assert_called_once_with(root)
    assert report["status"] == "error"
    assert "final production-sync continuity gate: S12 current readiness: S11 is incomplete" in report["errors"]
    assert report["s12_continuity"] == {
        "status": "error",
        "readiness_status": "error",
        "sync_proof_status": "not_run_readiness_failed",
    }


def test_final_gate_requires_current_activation_sync_proof() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root)
        continuity = {
            "status": "blocked_external",
            "errors": ["S12 current sync proof: production evidence is stale"],
            "checks": {
                "readiness_status": "ok",
                "sync_proof_status": "blocked_external",
            },
        }
        patches = common_patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.object(verifier, "verify_s12_continuity", return_value=continuity),
        ):
            report = verifier.verify_v1(root, run_acceptance_commands=False)

    assert "final production-sync continuity gate: S12 current sync proof: production evidence is stale" in report["errors"]
    assert report["s12_continuity"]["readiness_status"] == "ok"
    assert report["s12_continuity"]["sync_proof_status"] == "blocked_external"


def test_fixture_without_required_s12_does_not_invent_provider_gate() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root, include_s12=False)
        patches = common_patches()
        with (
            patches[0],
            patches[1],
            patches[2],
            patches[3],
            patch.object(verifier, "verify_s12_continuity") as current_gate,
        ):
            report = verifier.verify_v1(root, run_acceptance_commands=False)

    current_gate.assert_not_called()
    assert report["s12_continuity"] is None

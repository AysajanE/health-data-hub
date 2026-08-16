from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

import pytest

import scripts.verify_s12_readiness as readiness


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def make_control_fixture(root: Path) -> None:
    for rel in (
        readiness.SCHEMA_REL,
        readiness.BRIEF_REL,
        readiness.AUTOPLAN_REL,
        readiness.POLICY_REL,
        readiness.VERIFIER_REL,
        readiness.TEST_REL,
    ):
        source = readiness.ROOT / rel
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(source.read_bytes())
    slices = [
        {"id": dependency, "status": "complete"}
        for dependency in sorted(readiness.REQUIRED_DEPENDENCIES)
    ]
    slices.append(
        {
            "id": "S12",
            "status": "blocked_external",
            "lane": "compiler_external_evidence",
            "risk": "high",
            "required": True,
            "brief": readiness.BRIEF_REL,
            "autoplan": readiness.AUTOPLAN_REL,
            "depends_on": sorted(readiness.REQUIRED_DEPENDENCIES),
            "pre_po_commands": list(readiness.PRE_READINESS_COMMANDS),
            "pre_ship_commands": list(readiness.PRE_READINESS_COMMANDS),
            "activation_acceptance": dict(readiness.ACTIVATION_ACCEPTANCE),
            "evidence_dirs": [
                "private/evidence/S12/authority",
                "private/evidence/S12/oura_sync",
            ],
            "deliverables": sorted(readiness.REQUIRED_DELIVERABLES),
            "acceptance": [
                "python -m pytest tests/ingestion -q",
                "python scripts/check_no_tracked_data.py",
            ],
        }
    )
    write_json(root / readiness.SLICES_REL, slices)


def authority_decision(
    private_rel: str,
    private_bytes: bytes,
    *,
    basis_type: str = "oura_separate_written_agreement_superseding_ai_restrictions",
) -> dict[str, object]:
    if basis_type == "oura_separate_written_agreement_superseding_ai_restrictions":
        confirmed_by = "oura"
        document_type = "oura_separate_written_ai_data_agreement"
        uses_oura_api = True
        explicitly_supersedes = [
            "section_4d_ai_model_prohibition",
            "section_6g_user_data_training_prohibition",
        ]
        outside_restrictions = False
    else:
        confirmed_by = "qualified_legal_review"
        document_type = "legal_confirmation_of_non_api_route_outside_agreement_restrictions"
        uses_oura_api = False
        explicitly_supersedes = []
        outside_restrictions = True
    return {
        "schema_version": "health_data_hub.provider_authority.oura.v1",
        "created_at": "2026-08-16T16:00:00-04:00",
        "slice": "S12",
        "provider": "oura",
        "status": "authorized",
        "agreement": {
            "title": "Oura API/MCP Agreement",
            "effective_date": "2026-06-08",
            "section_4a_iii_rule": "prior_written_consent_exception_does_not_override_sections_4d_or_6g",
            "section_4d_rule": "oura_api_prohibited_for_ai_model_development_training_evaluation_or_input",
            "section_6g_rule": "user_data_training_prohibited",
            "retention_rule": "agreement_constrained",
        },
        "authority_basis": {
            "basis_type": basis_type,
            "confirmed_by": confirmed_by,
            "decision_date": "2026-08-16",
            "valid_from": "2026-08-16",
            "valid_through": "2099-12-31",
            "revoked": False,
            "uses_oura_api": uses_oura_api,
            "explicitly_supersedes": explicitly_supersedes,
            "outside_api_mcp_agreement_restrictions": outside_restrictions,
            "scope_reference": "sanitized-authority-scope-v1",
        },
        "provider_policy": {
            "active_sleep_source": "oura",
            "eight_sleep_status": "fallback_only",
            "provider_reopening": False,
        },
        "covered_uses": sorted(readiness.REQUIRED_COVERED_USES),
        "retention_controls": {
            "raw_provider_payload_max_days": 7,
            "raw_audit_max_days": 30,
            "derived_data_max_days": 365,
            "backup_max_days": 30,
            "purge_on_authority_revocation": True,
            "deletion_plan_reference": "sanitized-retention-plan-v1",
        },
        "source_evidence": {
            "path": private_rel,
            "document_type": document_type,
            "sha256": hashlib.sha256(private_bytes).hexdigest(),
            "size_bytes": len(private_bytes),
            "mode": "0o600",
        },
        "insufficient_bases": {
            "user_acknowledgement_only": False,
            "oauth_or_token_only": False,
            "historical_smoke_only": False,
            "account_or_membership_only": False,
        },
        "sanitized": True,
        "raw_payload_tracked": False,
        "secret_values_tracked": False,
    }


def make_authorized_fixture(
    root: Path,
    *,
    basis_type: str = "oura_separate_written_agreement_superseding_ai_restrictions",
    private_bytes: bytes = b"opaque private written authority\x00\xff\xfe",
) -> tuple[Path, dict[str, object]]:
    make_control_fixture(root)
    private_rel = "private/evidence/S12/authority/source.bin"
    private_path = root / private_rel
    private_path.parent.mkdir(parents=True, exist_ok=True)
    private_path.write_bytes(private_bytes)
    os.chmod(private_path, 0o600)
    decision = authority_decision(private_rel, private_bytes, basis_type=basis_type)
    write_json(root / readiness.DECISION_REL, decision)
    return private_path, decision


def run_with_tracked_decision(root: Path) -> dict[str, object]:
    with patch.object(
        readiness,
        "tracked_and_matches_head",
        return_value=(True, "tracked at HEAD and worktree bytes match"),
    ):
        return readiness.verify_s12_readiness(root)


def test_missing_authority_is_blocked_external_without_token_or_network_checks() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_control_fixture(root)

        report = run_with_tracked_decision(root)

    assert report["status"] == "blocked_external"
    assert any("missing sanitized tracked authority decision" in error for error in report["errors"])
    assert report["checks"]["network_accessed"] is False
    assert report["checks"]["oauth_or_token_inspected"] is False


def test_completed_s12_remains_eligible_for_downstream_authority_revalidation() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_control_fixture(root)
        slices = json.loads((root / readiness.SLICES_REL).read_text(encoding="utf-8"))
        next(item for item in slices if item.get("id") == "S12")["status"] = "complete"

        errors, checks = readiness.validate_slice_contract(slices)

    assert not any("lifecycle status" in error for error in errors)
    assert checks["slice_contract"]["status"] == "complete"


def test_separate_oura_agreement_self_attestation_remains_blocked_and_opaque() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _private_path, decision = make_authorized_fixture(root)

        report = run_with_tracked_decision(root)

    assert report["status"] == "blocked_external", report
    assert readiness.AUTHENTICATED_AUTHORITY_BLOCKER in report["errors"]
    assert report["checks"]["private_source_contents_parsed"] is False
    assert report["checks"]["private_source_contents_read"] is False
    assert report["checks"]["private_source"]["mode"] == "0o600"
    assert report["checks"]["private_source"]["sha256_validated"] is False
    assert report["checks"]["private_source"]["contents_read"] is False
    assert report["checks"]["private_source"]["size_bytes"] == decision["source_evidence"]["size_bytes"]  # type: ignore[index]
    assert report["checks"]["provider_policy"] == "oura_only_v1_not_reopened"


def test_legally_confirmed_non_api_route_self_attestation_remains_blocked() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_authorized_fixture(root, basis_type="legally_confirmed_non_api_acquisition_route")

        report = run_with_tracked_decision(root)

    assert report["status"] == "blocked_external", report
    assert readiness.AUTHENTICATED_AUTHORITY_BLOCKER in report["errors"]


def test_arbitrary_opaque_bytes_cannot_authorize_and_are_never_opened() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        private_path, _decision = make_authorized_fixture(
            root,
            private_bytes=b"arbitrary self-authored opaque bytes",
        )
        original_open = Path.open

        def deny_private_open(path: Path, *args: object, **kwargs: object):
            if path == private_path:
                raise AssertionError("readiness must not open private authority bytes")
            return original_open(path, *args, **kwargs)

        with (
            patch.object(Path, "open", deny_private_open),
            patch.object(
                readiness,
                "tracked_and_matches_head",
                return_value=(True, "tracked at HEAD and worktree bytes match"),
            ),
        ):
            report = readiness.verify_s12_readiness(root)

    assert report["status"] == "blocked_external", report
    assert readiness.AUTHENTICATED_AUTHORITY_BLOCKER in report["errors"]
    assert report["checks"]["private_source"]["contents_read"] is False


def test_generic_prior_written_consent_fails_closed() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _private_path, decision = make_authorized_fixture(root)
        authority = decision["authority_basis"]
        source = decision["source_evidence"]
        authority["basis_type"] = "oura_prior_written_consent"  # type: ignore[index]
        authority["explicitly_supersedes"] = []  # type: ignore[index]
        source["document_type"] = "oura_written_authorization"  # type: ignore[index]
        write_json(root / readiness.DECISION_REL, decision)

        report = run_with_tracked_decision(root)

    assert report["status"] == "blocked_external"
    assert any("ordinary prior written consent is insufficient" in error for error in report["errors"])


@pytest.mark.parametrize(
    "missing_supersession",
    [
        "section_4d_ai_model_prohibition",
        "section_6g_user_data_training_prohibition",
    ],
)
def test_api_route_must_supersede_both_independent_prohibitions(missing_supersession: str) -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _private_path, decision = make_authorized_fixture(root)
        decision["authority_basis"]["explicitly_supersedes"].remove(missing_supersession)  # type: ignore[index,union-attr]
        write_json(root / readiness.DECISION_REL, decision)

        report = run_with_tracked_decision(root)

    assert report["status"] == "blocked_external"
    assert any("explicitly supersede both Section 4(d) and Section 6(g)" in error for error in report["errors"])


def test_alternative_route_cannot_use_oura_api() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _private_path, decision = make_authorized_fixture(
            root,
            basis_type="legally_confirmed_non_api_acquisition_route",
        )
        decision["authority_basis"]["uses_oura_api"] = True  # type: ignore[index]
        write_json(root / readiness.DECISION_REL, decision)

        report = run_with_tracked_decision(root)

    assert report["status"] == "blocked_external"
    assert any("alternative authority route must be non-API" in error for error in report["errors"])


@pytest.mark.parametrize(
    "insufficient_field",
    [
        "user_acknowledgement_only",
        "oauth_or_token_only",
        "historical_smoke_only",
        "account_or_membership_only",
    ],
)
def test_non_authoritative_bases_never_pass(insufficient_field: str) -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _private_path, decision = make_authorized_fixture(root)
        decision["insufficient_bases"][insufficient_field] = True  # type: ignore[index]
        write_json(root / readiness.DECISION_REL, decision)

        report = run_with_tracked_decision(root)

    assert report["status"] == "blocked_external"
    assert any(insufficient_field in error for error in report["errors"])


def test_incomplete_use_scope_is_blocked_external() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _private_path, decision = make_authorized_fixture(root)
        decision["covered_uses"].remove("ridge_evaluation")  # type: ignore[union-attr]
        write_json(root / readiness.DECISION_REL, decision)

        report = run_with_tracked_decision(root)

    assert report["status"] == "blocked_external"
    assert any("ridge_evaluation" in error for error in report["errors"])


def test_expired_authority_is_blocked_external() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _private_path, decision = make_authorized_fixture(root)
        decision["authority_basis"]["valid_through"] = "2026-08-15"  # type: ignore[index]
        write_json(root / readiness.DECISION_REL, decision)

        report = run_with_tracked_decision(root)

    assert report["status"] == "blocked_external"
    assert any("authority has expired" in error for error in report["errors"])


def test_revoked_authority_is_blocked_external() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _private_path, decision = make_authorized_fixture(root)
        decision["authority_basis"]["revoked"] = True  # type: ignore[index]
        write_json(root / readiness.DECISION_REL, decision)

        report = run_with_tracked_decision(root)

    assert report["status"] == "blocked_external"
    assert any("must explicitly state that authority is not revoked" in error for error in report["errors"])


def test_missing_private_source_is_blocked_external_without_reading_payload() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        private_path, _decision = make_authorized_fixture(root)
        private_path.unlink()

        report = run_with_tracked_decision(root)

    assert report["status"] == "blocked_external"
    assert any("private authority source is missing" in error for error in report["errors"])
    assert report["checks"]["private_source"]["contents_read"] is False


def test_provider_reopening_is_blocked_external() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _private_path, decision = make_authorized_fixture(root)
        decision["provider_policy"]["provider_reopening"] = True  # type: ignore[index]
        write_json(root / readiness.DECISION_REL, decision)

        report = run_with_tracked_decision(root)

    assert report["status"] == "blocked_external"
    assert any("must not reopen provider policy" in error for error in report["errors"])


@pytest.mark.parametrize(
    ("binding", "expected"),
    [
        ((False, "not tracked at HEAD"), "not tracked at HEAD"),
        ((False, "worktree bytes do not match HEAD"), "worktree bytes do not match HEAD"),
    ],
)
def test_untracked_or_dirty_decision_is_blocked_external(
    binding: tuple[bool, str],
    expected: str,
) -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_authorized_fixture(root)
        sealed_state = {
            "tracked_at_head": True,
            "worktree_matches_head": True,
            "regular_file": True,
            "symlink": False,
        }
        with (
            patch.object(readiness, "committed_input_state", return_value=sealed_state),
            patch.object(readiness, "tracked_and_matches_head", return_value=binding),
        ):
            report = readiness.verify_s12_readiness(root)

    assert report["status"] == "blocked_external"
    assert any(expected in error for error in report["errors"])


def test_private_source_mode_must_match_without_reading_or_trusting_local_hash() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        private_path, decision = make_authorized_fixture(root)
        decision["source_evidence"]["sha256"] = "0" * 64  # type: ignore[index]
        write_json(root / readiness.DECISION_REL, decision)
        os.chmod(private_path, 0o644)

        report = run_with_tracked_decision(root)

    assert report["status"] == "blocked_external"
    assert any("mode must be 0o600" in error for error in report["errors"])
    assert not any("SHA-256 does not match" in error for error in report["errors"])
    assert report["checks"]["private_source"]["sha256_validated"] is False


def test_symlink_private_source_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_control_fixture(root)
        real_path = root / "opaque-source.bin"
        private_bytes = b"opaque"
        real_path.write_bytes(private_bytes)
        private_rel = "private/evidence/S12/authority/source.bin"
        private_path = root / private_rel
        private_path.parent.mkdir(parents=True, exist_ok=True)
        private_path.symlink_to(real_path)
        decision = authority_decision(private_rel, private_bytes)
        write_json(root / readiness.DECISION_REL, decision)

        report = run_with_tracked_decision(root)

    assert report["status"] == "blocked_external"
    assert any("must not contain symlinks" in error for error in report["errors"])


def test_tracked_private_source_is_rejected() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_authorized_fixture(root)
        with (
            patch.object(
                readiness,
                "tracked_and_matches_head",
                return_value=(True, "tracked at HEAD and worktree bytes match"),
            ),
            patch.object(readiness, "git_path_is_tracked", return_value=True),
        ):
            report = readiness.verify_s12_readiness(root)

    assert report["status"] == "blocked_external"
    assert any("must not be tracked by git" in error for error in report["errors"])


def test_missing_control_contract_is_error_not_external_authority_success() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_authorized_fixture(root)
        (root / readiness.AUTOPLAN_REL).unlink()

        report = run_with_tracked_decision(root)

    assert report["status"] == "error"
    assert any("missing S12 contract document" in error for error in report["errors"])


def test_missing_pre_ship_readiness_is_control_error() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_authorized_fixture(root)
        slices_path = root / readiness.SLICES_REL
        slices = json.loads(slices_path.read_text(encoding="utf-8"))
        slices[-1].pop("pre_ship_commands")
        write_json(slices_path, slices)

        report = run_with_tracked_decision(root)

    assert report["status"] == "error"
    assert any("pre_ship_commands must rerun" in error for error in report["errors"])


def test_generated_plan_cannot_put_authority_controls_in_po_files_scope() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_authorized_fixture(root)
        autoplan = root / readiness.AUTOPLAN_REL
        autoplan.write_text(
            autoplan.read_text(encoding="utf-8")
            + "\nFiles: `scripts/verify_s12_readiness.py`; `src/ingestion/oura_sync.py`\n",
            encoding="utf-8",
        )

        report = run_with_tracked_decision(root)

    assert report["status"] == "error"
    assert any("immutable authority input appears in PO Files scope" in error for error in report["errors"])


def test_unsealed_immutable_input_is_control_error() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        make_authorized_fixture(root)

        def binding(_root: Path, rel: str) -> tuple[bool, str]:
            if rel == readiness.VERIFIER_REL:
                return False, "worktree bytes do not match HEAD"
            return True, "tracked at HEAD and worktree bytes match"

        with patch.object(readiness, "tracked_and_matches_head", side_effect=binding):
            report = readiness.verify_s12_readiness(root)

    assert report["status"] == "error"
    assert any(readiness.VERIFIER_REL in error and "not sealed at HEAD" in error for error in report["errors"])


def test_continuity_does_not_run_sync_proof_until_current_authority_is_ok() -> None:
    authority = {
        "status": "blocked_external",
        "errors": ["authority has expired"],
        "checks": {},
    }
    with (
        patch.object(readiness, "verify_s12_readiness", return_value=authority),
        patch.object(readiness, "run_s12_sync_proof") as sync_proof,
    ):
        report = readiness.verify_s12_continuity(Path("/tmp/example"))

    sync_proof.assert_not_called()
    assert report["status"] == "blocked_external"
    assert report["checks"]["sync_proof_status"] == "not_run_authority_blocked"


def test_continuity_requires_current_sync_proof_after_current_authority() -> None:
    authority = {"status": "ok", "errors": [], "checks": {}}
    sync_result = {
        "status": "blocked_external",
        "errors": ["aggregate production evidence is stale"],
        "checks": {"read_only": True},
    }
    root = Path("/tmp/example")
    with (
        patch.object(readiness, "verify_s12_readiness", return_value=authority),
        patch.object(readiness, "run_s12_sync_proof", return_value=sync_result) as sync_proof,
    ):
        report = readiness.verify_s12_continuity(root)

    sync_proof.assert_called_once_with(root)
    assert report["status"] == "blocked_external"
    assert report["errors"] == ["S12 current sync proof: aggregate production evidence is stale"]
    assert report["checks"]["sync_proof_status"] == "blocked_external"


def test_sync_proof_never_executes_future_generated_verifier_without_trusted_receipt_validator() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        script = root / "scripts/verify_s12_sync.py"
        script.parent.mkdir(parents=True)
        script.write_text("raise RuntimeError('must never execute')\n", encoding="utf-8")

        report = readiness.run_s12_sync_proof(root)

    assert report["status"] == "blocked_external"
    assert report["checks"]["executed"] is False
    assert report["checks"]["network_accessed"] is False
    assert report["checks"]["runtime_written"] is False

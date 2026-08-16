from __future__ import annotations

import json
import subprocess
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

import scripts.verify_s11_readiness as readiness
from scripts.setup_permissions import setup_permissions


VALID_AUTOPLAN = f"""# S11 Mood Logging Recovery Autoplan

Deliverables and verification are frozen below. Manual gates are forbidden.
The autonomous_gate_review is required.

## Two-boundary runtime/evidence bridge

Hermetic ship acceptance runs first. A separate `activation_acceptance` runs
from ship code against the canonical runtime root selected only through
`HEALTH_HUB_RUNTIME_ROOT`. It may read aggregate/private runtime evidence and
the live warehouse read-only. This two-boundary contract must not complete S11
unless activation returns ok.

Exact command: `{readiness.ACTIVATION_ACCEPTANCE['command']}`

## Implementation Tasks

- [ ] Implement the S11 recovery deliverables.
  Files: `app/mood_form.py`; `scripts/verify_mood_logging_recovery.py`
  Verify: `python -m pytest tests/ui -q`
"""

VALID_BRIEF = """# S11 Autonomous Brief

The runtime/evidence bridge uses a two-boundary contract. Hermetic ship
acceptance is followed by activation_acceptance from ship code. The canonical
runtime root comes only from HEALTH_HUB_RUNTIME_ROOT. Only activation may read
aggregate/private runtime evidence and the live warehouse read-only. S11 must
not complete unless activation returns ok.
"""


def write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def write_json(path: Path, payload: Any) -> None:
    write(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")


def run_git(root: Path, *argv: str) -> None:
    subprocess.run(["git", *argv], cwd=root, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)


def valid_slices() -> list[dict[str, Any]]:
    return [
        {"id": "S01", "status": "complete"},
        {"id": "S02", "status": "complete"},
        {
            "id": "S11",
            "status": "pending",
            "required": True,
            "lane": "compiler",
            "risk": "high",
            "depends_on": ["S01", "S02"],
            "brief": readiness.S11_BRIEF,
            "autoplan": readiness.S11_AUTOPLAN,
            "deliverables": sorted(readiness.REQUIRED_DELIVERABLES),
            "evidence_dirs": ["private/evidence/S11"],
            "acceptance": [
                "python -m pytest tests/ui tests/warehouse -q",
                "python scripts/check_autonomous_review_exists.py S11",
                "python scripts/check_no_tracked_data.py",
            ],
            "activation_acceptance": dict(readiness.ACTIVATION_ACCEPTANCE),
        },
    ]


def valid_policy() -> str:
    return """lanes:
  compiler: keel-compile
tripwire_deadlines:
  on_mood_transport_failure_week_4:
    recovery_slice: S11
    evidence_kind: mood_transport_v1
    evidence: private/evidence/S11/mood_transport
  on_mood_compliance_failure_week_8:
    recovery_slice: S11
    evidence_kind: mood_compliance_v1
    evidence: private/evidence/S11/mood_compliance
  on_baseline_gate_failure_week_9:
    recovery_slice: S11
    evidence_kind: baseline_gate_v1
    evidence: private/evidence/S11/baseline_gate
"""


def make_fixture(root: Path) -> None:
    setup_report = setup_permissions(root)
    assert setup_report["status"] == "ok"
    for rel in readiness.REQUIRED_COMMITTED_INPUTS:
        if rel == readiness.S11_BRIEF:
            write(root / rel, VALID_BRIEF)
        elif rel == readiness.S11_AUTOPLAN:
            write(root / rel, VALID_AUTOPLAN)
        elif rel == "ops/autonomy/slices.json":
            write_json(root / rel, valid_slices())
        elif rel == "ops/autonomy/policy.yaml":
            write(root / rel, valid_policy())
        else:
            write(root / rel, f"fixture for {rel}\n")
    run_git(root, "init", "-q")
    run_git(root, "config", "user.email", "test@example.invalid")
    run_git(root, "config", "user.name", "Test")
    run_git(root, "add", ".")
    run_git(root, "commit", "-qm", "fixture")


def test_precompiler_contract_stops_on_unavailable_controls_without_payload_reads() -> None:
    with TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root)
        private_payload = root / "private/evidence/S11/activation/not-json.bin"
        private_payload.parent.mkdir(parents=True, mode=0o700)
        private_payload.write_bytes(b"\x00not-json-and-never-read\xff")
        private_payload.chmod(0o600)
        assert setup_permissions(root)["status"] == "ok"

        report = readiness.verify_s11_readiness(root)

    assert report["status"] == "error"
    assert any("trusted outer activation receipt validator is unavailable" in error for error in report["errors"])
    assert any("private is directly readable" in error for error in report["errors"])
    assert report["checks"]["private_payload_values_read"] is False
    assert report["checks"]["phone_evidence_required"] is False
    assert report["checks"]["paid_execution_performed"] is False
    assert report["checks"]["activation_acceptance_exact"] is True
    assert report["checks"]["trusted_outer_activation_receipt_validator"] == {
        "status": "unavailable",
        "reason": "not_implemented",
        "activation_receipt_values_read": False,
    }
    isolation = report["checks"]["sensitive_path_isolation"]
    assert isolation["file_contents_read"] is False
    assert isolation["directories_enumerated"] is False
    assert isolation["symlinks_followed"] is False


def test_readable_env_file_is_detected_from_metadata_without_opening_it() -> None:
    with TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root)
        env_path = root / ".env.local"
        env_path.write_bytes(b"\xff\x00not-utf8-and-must-never-be-opened")
        env_path.chmod(0o600)

        report = readiness.verify_s11_readiness(root)

    isolation = report["checks"]["sensitive_path_isolation"]
    assert isolation["paths"][".env.local"] == {
        "present": True,
        "symlink": False,
        "kind": "file",
        "directly_readable": True,
    }
    assert any(".env.local is directly readable" in error for error in report["errors"])
    assert report["checks"]["environment_values_read"] is False


def test_sensitive_roots_are_never_opened_or_enumerated(monkeypatch) -> None:
    with TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root)
        private_payload = root / "private/evidence/S11/activation/not-json.bin"
        private_payload.parent.mkdir(parents=True, mode=0o700)
        private_payload.write_bytes(b"\xff\x00must-not-be-read")
        private_payload.chmod(0o600)

        original_open = Path.open
        original_iterdir = Path.iterdir
        sensitive_roots = tuple(root / rel for rel in (".env", ".env.local", "data", "private", "models"))

        def is_sensitive(path: Path) -> bool:
            candidate = Path(path)
            return any(candidate == sensitive or sensitive in candidate.parents for sensitive in sensitive_roots)

        def guarded_open(path: Path, *args, **kwargs):
            if is_sensitive(path):
                raise AssertionError(f"sensitive path opened: {path}")
            return original_open(path, *args, **kwargs)

        def guarded_iterdir(path: Path):
            if is_sensitive(path):
                raise AssertionError(f"sensitive path enumerated: {path}")
            return original_iterdir(path)

        monkeypatch.setattr(Path, "open", guarded_open)
        monkeypatch.setattr(Path, "iterdir", guarded_iterdir)

        report = readiness.verify_s11_readiness(root)

    assert report["status"] == "error"
    assert report["checks"]["private_payload_values_read"] is False
    assert report["checks"]["permissions"]["file_contents_read"] is False
    assert report["checks"]["permissions"]["directories_enumerated"] is False


def test_sensitive_symlink_ancestor_stops_metadata_traversal(monkeypatch) -> None:
    with TemporaryDirectory() as temp, TemporaryDirectory() as outside_temp:
        root = Path(temp)
        outside = Path(outside_temp)
        make_fixture(root)
        (outside / "secrets").write_bytes(b"must-not-be-touched")
        (root / "data").rmdir()
        (root / "data").symlink_to(outside, target_is_directory=True)

        original_lstat = Path.lstat

        def guarded_lstat(path: Path, *args, **kwargs):
            if path == outside or outside in path.parents:
                raise AssertionError(f"followed sensitive symlink outside repository: {path}")
            return original_lstat(path, *args, **kwargs)

        monkeypatch.setattr(Path, "lstat", guarded_lstat)

        report = readiness.verify_s11_readiness(root)

    data_secrets = report["checks"]["sensitive_path_isolation"]["paths"]["data/secrets"]
    assert data_secrets["kind"] == "symlink_ancestor"
    assert data_secrets["stopped_at"] == "data"
    assert data_secrets["present"] == "unknown"
    assert report["checks"]["permissions"]["symlinks_followed"] is False


def test_uncommitted_primary_input_blocks_compiler() -> None:
    with TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root)
        write(root / readiness.S11_BRIEF, VALID_BRIEF + "\nuncommitted change\n")

        report = readiness.verify_s11_readiness(root)

    assert report["status"] == "error"
    assert any(readiness.S11_BRIEF in error and "worktree bytes differ" in error for error in report["errors"])


def test_invalid_autoplan_blocks_compiler() -> None:
    with TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root)
        write(root / readiness.S11_AUTOPLAN, "# S11\n")
        run_git(root, "add", readiness.S11_AUTOPLAN)
        run_git(root, "commit", "-qm", "invalid autoplan")

        report = readiness.verify_s11_readiness(root)

    assert report["status"] == "error"
    assert "S11 autoplan is missing Implementation Tasks section" in report["errors"]
    assert "S11 autoplan is missing compiler-parseable Files fields" in report["errors"]


def test_symlinked_contract_input_is_rejected_without_following_it() -> None:
    with TemporaryDirectory() as temp, TemporaryDirectory() as outside_temp:
        root = Path(temp)
        make_fixture(root)
        outside = Path(outside_temp) / "outside-brief.md"
        outside.write_text(VALID_BRIEF, encoding="utf-8")
        brief = root / readiness.S11_BRIEF
        brief.unlink()
        brief.symlink_to(outside)
        run_git(root, "add", readiness.S11_BRIEF)
        run_git(root, "commit", "-qm", "symlink brief")

        report = readiness.verify_s11_readiness(root)

    assert report["status"] == "error"
    assert any(
        readiness.S11_BRIEF in error and "missing, non-regular, or symlink input" in error
        for error in report["errors"]
    )
    assert report["checks"]["committed_inputs"][readiness.S11_BRIEF]["symlink"] is True


def test_missing_two_boundary_marker_blocks_compiler() -> None:
    with TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root)
        write(root / readiness.S11_BRIEF, "# S11 brief\n")
        write(root / readiness.S11_AUTOPLAN, VALID_AUTOPLAN.replace("runtime/evidence bridge", "runtime evidence"))
        run_git(root, "add", readiness.S11_BRIEF, readiness.S11_AUTOPLAN)
        run_git(root, "commit", "-qm", "remove bridge")

        report = readiness.verify_s11_readiness(root)

    assert report["status"] == "error"
    assert "S11 inputs are missing required two-boundary contract marker: runtime/evidence bridge" in report["errors"]


def test_activation_acceptance_must_match_exact_contract() -> None:
    with TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root)
        slices = valid_slices()
        slices[-1]["activation_acceptance"]["live_warehouse_access"] = "read_write"
        write_json(root / "ops/autonomy/slices.json", slices)
        run_git(root, "add", "ops/autonomy/slices.json")
        run_git(root, "commit", "-qm", "unsafe activation")

        report = readiness.verify_s11_readiness(root)

    assert report["status"] == "error"
    assert "S11 activation_acceptance must exactly match the canonical post-ship runtime contract" in report["errors"]


def test_ship_acceptance_cannot_read_runtime_or_private_evidence() -> None:
    with TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root)
        slices = valid_slices()
        slices[-1]["acceptance"].append("python scripts/verify_mood_logging_recovery.py --json")
        write_json(root / "ops/autonomy/slices.json", slices)
        run_git(root, "add", "ops/autonomy/slices.json")
        run_git(root, "commit", "-qm", "non-hermetic acceptance")

        report = readiness.verify_s11_readiness(root)

    assert report["status"] == "error"
    assert (
        "S11 ship acceptance is not hermetic; runtime/private activation belongs only in activation_acceptance"
        in report["errors"]
    )


def test_unsafe_runtime_modes_block_without_repairing_them() -> None:
    with TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root)
        (root / "data").chmod(0o755)

        report = readiness.verify_s11_readiness(root)

        assert report["status"] == "error"
        assert any("unsafe directory mode for data: 0o755" in error for error in report["errors"])
        assert (root / "data").stat().st_mode & 0o777 == 0o755


def test_json_main_emits_structured_report() -> None:
    with TemporaryDirectory() as temp:
        root = Path(temp)
        make_fixture(root)
        output = StringIO()
        with redirect_stdout(output):
            exit_code = readiness.main(["--root", str(root), "--json"])

    payload = json.loads(output.getvalue())
    assert exit_code == 1
    assert payload["status"] == "error"
    assert payload["checks"]["phase"] == "pre_compiler"

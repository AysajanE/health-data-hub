from __future__ import annotations

from datetime import UTC, date, datetime
import hashlib
import json
from pathlib import Path
import subprocess
import tempfile
from unittest.mock import patch
from uuid import uuid4

from scripts.verify_s04_readiness import verify_s04_readiness
from src.warehouse.features import SleepProviderPolicy, load_sleep_provider_policy
from src.warehouse.warehouse import (
    compute_daily_features,
    connect_duckdb,
    insert_mood_entry,
    insert_sleep_night,
    select_labeled_daily_features,
)


FALLBACK_POLICY = SleepProviderPolicy(
    active_sleep_source="oura",
    eight_sleep_state="fallback_active",
    eight_sleep_allowed_for_features=False,
)


def _sleep_payload(
    source: str,
    sleep_date: date,
    *,
    total_sleep_min: int,
    deep_min: int,
    hrv_avg_ms: float,
) -> dict:
    return {
        "source": source,
        "sleep_date": sleep_date,
        "bedtime_utc": datetime(2026, 5, sleep_date.day - 1, 22, 0, tzinfo=UTC),
        "waketime_utc": datetime(2026, 5, sleep_date.day, 6, 0, tzinfo=UTC),
        "total_sleep_min": total_sleep_min,
        "rem_min": 80,
        "deep_min": deep_min,
        "light_min": max(total_sleep_min - deep_min - 80, 0),
        "awake_min": 20,
        "hrv_avg_ms": hrv_avg_ms,
        "rhr_avg_bpm": 52,
        "body_temp_dev_c": 0.0,
        "sleep_score": 82,
        "ingested_at_utc": datetime(2026, 5, sleep_date.day, 7, 0, tzinfo=UTC),
    }


def _insert_mood(conn, mood_date: date, *, feeling: int) -> None:
    insert_mood_entry(
        conn,
        {
            "log_id": uuid4(),
            "logged_at_utc": datetime(2026, 5, mood_date.day, 22, 0, tzinfo=UTC),
            "mood_date": mood_date,
            "feeling": feeling,
            "energy": feeling,
            "notes": None,
            "context_chips": (),
            "source": "manual",
            "supersedes_log_id": None,
        },
    )


def _insert_prior_mood(conn, feature_date: date, feeling: int = 6) -> None:
    _insert_mood(conn, date(2026, 5, feature_date.day - 1), feeling=feeling)


def _diagnostics(conn, feature_date: date) -> tuple:
    return conn.execute(
        """
        SELECT oura_present, eight_present, total_sleep_delta_min, hrv_merge_method, stage_source, warning
        FROM sleep_merge_diagnostics
        WHERE sleep_date = ?
        """,
        [feature_date],
    ).fetchone()


def test_oura_only_fallback_ignores_8sleep_rows_for_features() -> None:
    feature_date = date(2026, 5, 23)
    conn = connect_duckdb(":memory:", apply_schema=True)
    try:
        _insert_prior_mood(conn, feature_date)
        insert_sleep_night(conn, _sleep_payload("oura", feature_date, total_sleep_min=430, deep_min=86, hrv_avg_ms=42.0))
        insert_sleep_night(
            conn,
            _sleep_payload("8sleep", feature_date, total_sleep_min=500, deep_min=130, hrv_avg_ms=64.0),
        )

        row = compute_daily_features(conn, feature_date, provider_policy=FALLBACK_POLICY)
        diagnostics = _diagnostics(conn, feature_date)
    finally:
        conn.close()

    assert row is not None
    assert row.total_sleep_min == 430
    assert row.hrv_avg_ms == 42.0
    assert row.deep_sleep_pct == 86 / 430
    assert row.sleep_source_count == 1
    assert row.sleep_merge_warning == "8sleep_fallback_ignored"
    assert diagnostics == (True, True, 70, "oura_primary", "oura", "8sleep_fallback_ignored")


def test_oura_and_8sleep_are_not_averaged_or_blended() -> None:
    feature_date = date(2026, 5, 24)
    conn = connect_duckdb(":memory:", apply_schema=True)
    try:
        _insert_prior_mood(conn, feature_date)
        insert_sleep_night(conn, _sleep_payload("oura", feature_date, total_sleep_min=400, deep_min=80, hrv_avg_ms=40.0))
        insert_sleep_night(
            conn,
            _sleep_payload("8sleep", feature_date, total_sleep_min=520, deep_min=160, hrv_avg_ms=90.0),
        )

        row = compute_daily_features(conn, feature_date, provider_policy=FALLBACK_POLICY)
        diagnostics = _diagnostics(conn, feature_date)
    finally:
        conn.close()

    assert row is not None
    assert row.total_sleep_min == 400
    assert row.total_sleep_min != 460
    assert row.hrv_avg_ms == 40.0
    assert row.hrv_avg_ms != 65.0
    assert row.deep_sleep_pct == 80 / 400
    assert diagnostics == (True, True, 120, "oura_primary", "oura", "8sleep_fallback_ignored")


def test_8sleep_only_rows_do_not_create_v1_sleep_features() -> None:
    feature_date = date(2026, 5, 25)
    conn = connect_duckdb(":memory:", apply_schema=True)
    try:
        _insert_prior_mood(conn, feature_date)
        insert_sleep_night(
            conn,
            _sleep_payload("8sleep", feature_date, total_sleep_min=510, deep_min=140, hrv_avg_ms=70.0),
        )

        row = compute_daily_features(conn, feature_date, provider_policy=FALLBACK_POLICY)
        diagnostics = _diagnostics(conn, feature_date)
    finally:
        conn.close()

    assert row is not None
    assert row.total_sleep_min is None
    assert row.hrv_avg_ms is None
    assert row.deep_sleep_pct is None
    assert row.prior_day_feeling == 6
    assert row.sleep_source_count is None
    assert row.sleep_merge_warning == "8sleep_fallback_ignored"
    assert diagnostics == (False, True, None, "missing", None, "8sleep_fallback_ignored")


def test_logged_day_without_sleep_rows_still_persists_daily_feature_row() -> None:
    feature_date = date(2026, 5, 26)
    conn = connect_duckdb(":memory:", apply_schema=True)
    try:
        _insert_prior_mood(conn, feature_date, feeling=6)
        _insert_mood(conn, feature_date, feeling=4)

        row = compute_daily_features(conn, feature_date, provider_policy=FALLBACK_POLICY)
    finally:
        conn.close()

    assert row is not None
    assert row.feature_date == feature_date
    assert row.total_sleep_min is None
    assert row.hrv_avg_ms is None
    assert row.deep_sleep_pct is None
    assert row.hrv_z is None
    assert row.hrv_z_method is None
    assert row.prior_day_feeling == 6
    assert row.prior_day_feeling_imputed is False


def test_select_labeled_daily_features_uses_same_day_mood_without_forward_fill() -> None:
    first_date = date(2026, 5, 27)
    second_date = date(2026, 5, 28)
    conn = connect_duckdb(":memory:", apply_schema=True)
    try:
        _insert_prior_mood(conn, first_date, feeling=6)
        _insert_mood(conn, first_date, feeling=4)
        insert_sleep_night(conn, _sleep_payload("oura", first_date, total_sleep_min=420, deep_min=84, hrv_avg_ms=44.0))
        compute_daily_features(conn, first_date, provider_policy=FALLBACK_POLICY)

        insert_sleep_night(conn, _sleep_payload("oura", second_date, total_sleep_min=415, deep_min=83, hrv_avg_ms=45.0))
        compute_daily_features(conn, second_date, provider_policy=FALLBACK_POLICY)

        rows = select_labeled_daily_features(conn, start_date=first_date, end_date=second_date)
    finally:
        conn.close()

    assert len(rows) == 1
    assert rows[0].feature_date == first_date
    assert rows[0].feeling == 4
    assert rows[0].prior_day_feeling == 6
    assert rows[0].total_sleep_min == 420


def _write_json(path: Path, payload: dict | list) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _write_public_evidence(root: Path, rel: str) -> Path:
    evidence = root / rel
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text("sanitized provider evidence\n", encoding="utf-8")
    return evidence


def _pyeight_decision(
    root: Path,
    *,
    status: str = "fallback_accepted",
    fallback_active: bool = True,
    evidence_rel: str = "docs/evidence/provider-decision.md",
    superseded_by: str | None = None,
) -> dict:
    evidence = _write_public_evidence(root, evidence_rel)
    payload = {
        "schema_version": "autokeel.provider_evidence_decision.v1",
        "created_at": "2026-05-31T00:00:00-04:00",
        "slice": "S03",
        "provider": "pyeight",
        "status": status,
        "evidence_status": "ok" if status == "ok" else "blocked_external",
        "evidence_path": evidence_rel,
        "fallback_active": fallback_active,
        "supersedes": [],
        "superseded_by": superseded_by,
        "sanitized": True,
        "raw_payload_tracked": False,
        "secret_values_tracked": False,
        "evidence_sha256": _sha256(evidence),
        "evidence_size_bytes": evidence.stat().st_size,
    }
    if status == "fallback_accepted":
        payload["action"] = "oura_only_v1"
    else:
        payload["decision"] = "include_8_sleep_under_tripwire"
    return payload


def _write_s04_readiness_fixture(root: Path) -> None:
    subprocess.run(["git", "init"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    subprocess.run(["git", "config", "user.email", "tests@example.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "Tests"], cwd=root, check=True)
    (root / ".gitignore").write_text("private/\ndata/\n.env\nops/autonomy/.autokeel.lock\nops/autonomy/*.tmp\n", encoding="utf-8")
    (root / "ops/autonomy/decisions").mkdir(parents=True)
    (root / "ops/autonomy/failure_ledger.jsonl").parent.mkdir(parents=True, exist_ok=True)
    (root / "ops/autonomy/failure_ledger.jsonl").write_text("", encoding="utf-8")

    _write_json(
        root / "ops/autonomy/slices.json",
        [
            {"id": "S01", "status": "complete", "required": True},
            {"id": "S02", "status": "complete", "required": True},
            {
                "id": "S03",
                "status": "complete",
                "required": True,
                "ship_branch": "ship/s03",
                "ship_commit": "",
                "review_artifacts": ["docs/reviews/s03-autonomous-ingestion-evidence-review.md"],
            },
            {"id": "S04", "status": "pending", "required": True},
        ],
    )
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-m", "seed"], cwd=root, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
    ship_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, text=True, stdout=subprocess.PIPE, check=True).stdout.strip()
    subprocess.run(["git", "branch", "ship/s03", ship_commit], cwd=root, check=True)

    slices = json.loads((root / "ops/autonomy/slices.json").read_text(encoding="utf-8"))
    slices[2]["ship_commit"] = ship_commit
    _write_json(root / "ops/autonomy/slices.json", slices)

    oura = root / "private/evidence/S03/oura_smoke/report.json"
    oura.parent.mkdir(parents=True, exist_ok=True)
    oura.write_text(json.dumps({"status": "ok"}), encoding="utf-8")
    _write_json(root / "ops/autonomy/decisions/S03-pyeight-fallback.json", _pyeight_decision(root))

    summary = root / "docs/evidence/ingestion/s03-ingestion-evidence.md"
    summary.parent.mkdir(parents=True, exist_ok=True)
    summary.write_text("direct_oura_api_v2_periodic_pull\noura_only_v1\n", encoding="utf-8")
    addendum = root / "docs/evidence/S03-8sleep-provider-status-addendum-20260531.md"
    addendum.write_text(
        "8 Sleep remains fallback-only\n"
        "Oura direct API v2 remains the first-class sleep provider\n"
        "8 Sleep values must not be averaged, blended, reconciled, used as fallback HRV\n",
        encoding="utf-8",
    )
    s04_rule = (
        "active S03 provider decision\n"
        "Oura-only v1\n"
        "must not require pyEight evidence\n"
        "8 Sleep must remain absent/fallback\n"
        "feature construction must ignore 8 Sleep rows\n"
        "used as fallback HRV\n"
    )
    brief = root / "docs/briefs/s04-feature-engineering.autonomous-brief.md"
    brief.parent.mkdir(parents=True, exist_ok=True)
    brief.write_text(s04_rule, encoding="utf-8")
    autoplan = root / "docs/gstack/s04-feature-engineering-autoplan.md"
    autoplan.parent.mkdir(parents=True, exist_ok=True)
    autoplan.write_text(s04_rule, encoding="utf-8")


def test_pyeight_evidence_is_not_required_by_s04_readiness() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _write_s04_readiness_fixture(root)

        report = verify_s04_readiness(root)

        assert report["status"] == "ok", report
        assert report["checks"]["pyeight_state"] == "fallback_active"
        assert not (root / "private/evidence/S03/pyeight_smoke").exists()


def test_conflicting_provider_decisions_fail_s04_readiness() -> None:
    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _write_s04_readiness_fixture(root)
        _write_json(
            root / "ops/autonomy/decisions/S03-pyeight-evidence.json",
            _pyeight_decision(root, status="ok", fallback_active=False, evidence_rel="docs/evidence/provider-evidence.md"),
        )

        report = verify_s04_readiness(root)

        assert report["status"] == "error"
        joined = "\n".join(report["errors"])
        assert "provider decision invalid" in joined or "exactly fallback_active" in joined


def test_compute_daily_features_loads_active_s03_provider_decision_from_policy_root() -> None:
    feature_date = date(2026, 5, 26)

    with tempfile.TemporaryDirectory() as temp:
        root = Path(temp)
        _write_s04_readiness_fixture(root)
        loaded_policy: SleepProviderPolicy | None = None

        def _load_policy_from_root(policy_root: Path) -> SleepProviderPolicy:
            nonlocal loaded_policy
            loaded_policy = load_sleep_provider_policy(policy_root)
            return loaded_policy

        conn = connect_duckdb(":memory:", apply_schema=True)
        try:
            _insert_prior_mood(conn, feature_date)
            insert_sleep_night(
                conn,
                _sleep_payload("oura", feature_date, total_sleep_min=410, deep_min=82, hrv_avg_ms=41.0),
            )
            insert_sleep_night(
                conn,
                _sleep_payload("8sleep", feature_date, total_sleep_min=490, deep_min=150, hrv_avg_ms=74.0),
            )

            with patch(
                "src.warehouse.warehouse.load_sleep_provider_policy",
                side_effect=_load_policy_from_root,
            ) as load_policy:
                row = compute_daily_features(conn, feature_date, policy_root=root)
        finally:
            conn.close()

    load_policy.assert_called_once_with(root)
    assert loaded_policy is not None
    assert loaded_policy.active_sleep_source == "oura"
    assert loaded_policy.eight_sleep_state == "fallback_active"
    assert loaded_policy.decision_paths == ("ops/autonomy/decisions/S03-pyeight-fallback.json",)
    assert row is not None
    assert row.total_sleep_min == 410
    assert row.deep_sleep_pct == 82 / 410
    assert row.hrv_avg_ms == 41.0
    assert row.sleep_merge_warning == "8sleep_fallback_ignored"

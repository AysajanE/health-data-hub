from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory

import duckdb

from scripts.verify_s05_provider_policy import (
    collect_model_training_input_violations,
    scan_model_training_code,
    training_input_policy_contract_report,
    verify_s05_provider_policy,
    warehouse_provider_violations,
)


REPO_ROOT = Path(__file__).resolve().parents[2]


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _decision_payload(*, status: str = "fallback_accepted", fallback_active: bool = True) -> dict[str, object]:
    payload: dict[str, object] = {
        "schema_version": "autokeel.provider_evidence_decision.v1",
        "created_at": "2026-06-11T00:00:00-04:00",
        "slice": "S03",
        "provider": "pyeight",
        "status": status,
        "fallback_active": fallback_active,
        "evidence_status": "blocked_external" if fallback_active else "ok",
        "evidence_path": "docs/evidence/provider-decision.md",
        "sanitized": True,
        "raw_payload_tracked": False,
        "secret_values_tracked": False,
    }
    if fallback_active:
        payload["action"] = "oura_only_v1"
    else:
        payload["decision"] = "include_8_sleep_under_tripwire"
    return payload


def _write_provider_policy_fixture(root: Path) -> None:
    _write_json(
        root / "ops/autonomy/slices.json",
        [
            {"id": "S03", "status": "complete"},
            {"id": "S04", "status": "complete"},
        ],
    )
    _write_json(root / "ops/autonomy/decisions/S03-pyeight-fallback.json", _decision_payload())


def test_model_training_excludes_8sleep_under_oura_only_v1() -> None:
    report = training_input_policy_contract_report()

    assert report["errors"] == []
    assert report["checks"]["allowed_fixture_violations"] == []
    assert report["checks"]["rejected_fixture_violations"] == [
        "training input row 0 must have exact persisted source='oura': '8sleep'",
        "training input row 1 must have sleep_source_count = 1: 2",
        "training input row 2 must have exact persisted hrv_merge_method='oura_primary': 'eight_fallback'",
        "training input row 3 must have exact persisted source='oura': ' OURA '",
        "training input row 4 must have exact persisted hrv_merge_method='oura_primary': 'OURA_PRIMARY'",
    ]
    assert scan_model_training_code(REPO_ROOT) == []


def test_collect_model_training_input_violations_rejects_fallback_breakers() -> None:
    violations = collect_model_training_input_violations(
        [
            {"feature_date": "2026-06-01", "source": "oura", "oura_present": True, "sleep_source_count": 1, "hrv_merge_method": "oura_primary"},
            {"feature_date": "2026-06-02", "source": "8sleep", "oura_present": True, "sleep_source_count": 1, "hrv_merge_method": "missing"},
            {"feature_date": "2026-06-03", "source": "oura", "oura_present": True, "sleep_source_count": 2, "hrv_merge_method": "oura_primary"},
            {"feature_date": "2026-06-04", "source": "oura", "oura_present": True, "sleep_source_count": 1, "hrv_merge_method": "eight_fallback"},
        ]
    )

    assert violations == [
        "training input row 1 must have exact persisted source='oura': '8sleep'",
        "training input row 1 must have exact persisted hrv_merge_method='oura_primary': 'missing'",
        "training input row 2 must have sleep_source_count = 1: 2",
        "training input row 3 must have exact persisted hrv_merge_method='oura_primary': 'eight_fallback'",
    ]


def test_collect_model_training_input_violations_rejects_aliases_and_bool_counts() -> None:
    violations = collect_model_training_input_violations(
        [
            {
                "feature_date": "2026-06-01",
                "source": "Oura",
                "oura_present": False,
                "sleep_source_count": True,
                "hrv_merge_method": "oura-primary",
            }
        ]
    )

    assert violations == [
        "training input row 0 must have exact persisted source='oura': 'Oura'",
        "training input row 0 must have persisted oura_present=true: False",
        "training input row 0 has non-integer sleep_source_count: True",
        "training input row 0 must have exact persisted hrv_merge_method='oura_primary': 'oura-primary'",
    ]


def test_collect_model_training_input_violations_fails_closed_on_missing_fields() -> None:
    violations = collect_model_training_input_violations(
        [
            {"feature_date": "2026-06-01", "oura_present": True, "sleep_source_count": 1, "hrv_merge_method": "oura_primary"},
            {"feature_date": "2026-06-02", "source": "oura", "oura_present": True, "hrv_merge_method": "oura_primary"},
            {"feature_date": "2026-06-03", "source": "oura", "oura_present": True, "sleep_source_count": 1},
            {"feature_date": "2026-06-04", "source": "oura", "sleep_source_count": 1, "hrv_merge_method": "oura_primary"},
        ]
    )

    assert violations == [
        "training input row 0 is missing required fields: source",
        "training input row 1 is missing required fields: sleep_source_count",
        "training input row 2 is missing required fields: hrv_merge_method",
        "training input row 3 is missing required fields: oura_present",
    ]


def test_verify_s05_provider_policy_fails_closed_without_active_fallback_decision() -> None:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_json(
            root / "ops/autonomy/slices.json",
            [
                {"id": "S03", "status": "complete"},
                {"id": "S04", "status": "complete"},
            ],
        )

        report = verify_s05_provider_policy(root)

    assert report["status"] == "error"
    assert "missing active S03 8 Sleep fallback decision" in report["errors"]


def test_warehouse_provider_violations_detects_sleep_source_and_hrv_policy_breaks() -> None:
    with TemporaryDirectory() as temp_dir:
        root = Path(temp_dir)
        _write_provider_policy_fixture(root)

        database = root / "data/warehouse.duckdb"
        database.parent.mkdir(parents=True, exist_ok=True)
        conn = duckdb.connect(str(database))
        try:
            conn.execute(
                """
                CREATE TABLE daily_features (
                    feature_date DATE,
                    total_sleep_min DOUBLE,
                    hrv_z DOUBLE,
                    deep_sleep_pct DOUBLE,
                    prior_day_feeling DOUBLE,
                    sleep_source_count INTEGER
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE sleep_merge_diagnostics (
                    sleep_date DATE,
                    hrv_merge_method VARCHAR,
                    stage_source VARCHAR,
                    oura_present BOOLEAN
                )
                """
            )
            conn.execute(
                "INSERT INTO daily_features VALUES ('2026-06-01', 420, 0.1, 0.2, 6, 2)"
            )
            conn.execute(
                "INSERT INTO sleep_merge_diagnostics VALUES ('2026-06-01', 'eight_fallback', 'oura', FALSE)"
            )
        finally:
            conn.close()

        violations, checks = warehouse_provider_violations(root)

    assert violations == [
        "daily_features contains rows with sleep_source_count != 1",
        "sleep_merge_diagnostics contains hrv_merge_method = eight_fallback",
        "model-ready daily_features rows lack exact persisted Oura diagnostic provenance",
    ]
    assert checks["daily_features_source_count_not_1"] == 1
    assert checks["diagnostics_eight_fallback_hrv"] == 1
    assert checks["model_ready_rows_without_exact_oura_provenance"] == 1

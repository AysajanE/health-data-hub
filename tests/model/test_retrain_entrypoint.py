from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import duckdb

from scripts.retrain_model import main, run_retrain


def _build_training_rows(
    count: int,
    *,
    residual_scale: float = 0.0,
) -> list[dict[str, float | str]]:
    rows: list[dict[str, float | str]] = []
    residual_pattern = (0.0, 0.05, -0.04, 0.03, -0.02)

    for offset in range(count):
        total_sleep_min = 390.0 + float((offset * 17) % 120)
        hrv_z = -1.5 + float((offset * 5) % 14) / 4.0
        deep_sleep_pct = 0.14 + float((offset * 7) % 10) / 100.0
        prior_day_feeling = 3.0 + float((offset * 3) % 7)
        residual = residual_pattern[offset % len(residual_pattern)] * residual_scale
        feeling = (
            1.25
            + (0.010 * total_sleep_min)
            + (0.90 * hrv_z)
            + (7.50 * deep_sleep_pct)
            + (0.55 * prior_day_feeling)
            + residual
        )
        rows.append(
            {
                "feature_date": (date(2026, 1, 1) + timedelta(days=offset)).isoformat(),
                "total_sleep_min": total_sleep_min,
                "hrv_z": hrv_z,
                "deep_sleep_pct": deep_sleep_pct,
                "prior_day_feeling": prior_day_feeling,
                "feeling": feeling,
                "hrv_avg_ms": 40.0 + float(offset),
                "feature_version": "v1.0",
            }
        )

    return rows


def _read_eval_records(path: Path) -> list[dict[str, object]]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def _ok_preflight(root: Path) -> dict[str, object]:
    return {"status": "ok", "errors": [], "warnings": [], "checks": {"root": str(root)}}


def _write_verified_feature_database(
    path: Path,
    rows: list[dict[str, float | str]],
    *,
    imputed_feature_dates: set[str] | None = None,
    multi_source_feature_dates: set[str] | None = None,
    eight_fallback_feature_dates: set[str] | None = None,
    non_oura_stage_feature_dates: set[str] | None = None,
    missing_diagnostic_feature_dates: set[str] | None = None,
) -> None:
    database = Path(path)
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
                hrv_avg_ms DOUBLE,
                feature_version VARCHAR,
                prior_day_feeling_imputed BOOLEAN,
                sleep_source_count INTEGER
            )
            """
        )
        conn.execute(
            "CREATE TABLE mood_current (mood_date DATE, log_id INTEGER)"
        )
        conn.execute(
            "CREATE TABLE mood_entries (log_id INTEGER, feeling DOUBLE)"
        )
        conn.execute(
            """
            CREATE TABLE sleep_merge_diagnostics (
                sleep_date DATE,
                hrv_merge_method VARCHAR,
                stage_source VARCHAR
            )
            """
        )

        imputed_feature_dates = imputed_feature_dates or set()
        multi_source_feature_dates = multi_source_feature_dates or set()
        eight_fallback_feature_dates = eight_fallback_feature_dates or set()
        non_oura_stage_feature_dates = non_oura_stage_feature_dates or set()
        missing_diagnostic_feature_dates = missing_diagnostic_feature_dates or set()
        for index, row in enumerate(rows, start=1):
            feature_date = str(row["feature_date"])
            conn.execute(
                """
                INSERT INTO daily_features VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    feature_date,
                    row["total_sleep_min"],
                    row["hrv_z"],
                    row["deep_sleep_pct"],
                    row["prior_day_feeling"],
                    row["hrv_avg_ms"],
                    row["feature_version"],
                    feature_date in imputed_feature_dates,
                    2 if feature_date in multi_source_feature_dates else 1,
                ],
            )
            conn.execute(
                "INSERT INTO mood_entries VALUES (?, ?)",
                [index, row["feeling"]],
            )
            conn.execute(
                "INSERT INTO mood_current VALUES (?, ?)",
                [feature_date, index],
            )
            if feature_date not in missing_diagnostic_feature_dates:
                conn.execute(
                    "INSERT INTO sleep_merge_diagnostics VALUES (?, ?, ?)",
                    [
                        feature_date,
                        (
                            "eight_fallback"
                            if feature_date in eight_fallback_feature_dates
                            else "oura_primary"
                        ),
                        (
                            "8sleep"
                            if feature_date in non_oura_stage_feature_dates
                            else "oura"
                        ),
                    ],
                )
    finally:
        conn.close()


def _assert_default_loader_skips_invalid_last_row(
    tmp_path: Path,
    **writer_kwargs: set[str],
) -> None:
    database_path = tmp_path / "data" / "warehouse.duckdb"
    eval_log_path = tmp_path / "models" / "eval.jsonl"
    rows = _build_training_rows(30)
    _write_verified_feature_database(database_path, rows, **writer_kwargs)

    report = run_retrain(
        root=tmp_path,
        database_path=database_path,
        eval_log_path=eval_log_path,
        model_dir=tmp_path / "models",
        persist_artifacts=False,
        run_date="2026-06-11",
        provider_preflight_runner=_ok_preflight,
    )

    records = _read_eval_records(eval_log_path)

    assert report["status"] == "skipped"
    assert report["record"] == records[0]
    assert records[0]["n_model"] == 29
    assert records[0]["trained_through_date"] == rows[-2]["feature_date"]


def test_retrain_entrypoint_runs_provider_preflight_before_loading_rows(
    tmp_path: Path,
) -> None:
    events: list[str] = []
    eval_log_path = tmp_path / "models" / "eval.jsonl"

    def provider_preflight(root: Path) -> dict[str, object]:
        events.append("preflight")
        return _ok_preflight(root)

    def feature_row_loader() -> list[dict[str, float | str]]:
        events.append("load")
        return _build_training_rows(29)

    exit_code = main(
        [
            "--root",
            str(tmp_path),
            "--eval-log-path",
            str(eval_log_path),
            "--model-dir",
            str(tmp_path / "models"),
            "--run-date",
            "2026-06-11",
            "--json",
        ],
        provider_preflight_runner=provider_preflight,
        feature_row_loader=feature_row_loader,
    )

    assert exit_code == 0
    assert events == ["preflight", "load"]
    assert _read_eval_records(eval_log_path)[0]["status"] == "skipped"


def test_retrain_entrypoint_skips_below_thirty_model_ready_days(tmp_path: Path) -> None:
    eval_log_path = tmp_path / "models" / "eval.jsonl"
    model_dir = tmp_path / "models"

    report = run_retrain(
        root=tmp_path,
        eval_log_path=eval_log_path,
        model_dir=model_dir,
        run_date="2026-06-11",
        provider_preflight_runner=_ok_preflight,
        feature_row_loader=lambda: _build_training_rows(29),
    )

    records = _read_eval_records(eval_log_path)

    assert report["status"] == "skipped"
    assert report["record"] == records[0]
    assert records[0]["n_model"] == 29
    assert records[0]["skip_reason"] == "below_minimum_training_rows"
    assert records[0]["baseline_gate_passed"] is False
    assert records[0]["feature_sign_stability"] == []
    assert records[0]["latest_contributions"] == []
    assert records[0]["ablation_prior_mood_only_rmse"] is None
    assert records[0]["ablation_sleep_features_only_rmse"] is None
    assert not any(model_dir.glob("*.pkl"))


def test_retrain_entrypoint_default_loader_excludes_imputed_prior_day_feelings(
    tmp_path: Path,
) -> None:
    rows = _build_training_rows(30)
    _assert_default_loader_skips_invalid_last_row(
        tmp_path,
        imputed_feature_dates={str(rows[-1]["feature_date"])},
    )


def test_retrain_entrypoint_default_loader_requires_diagnostics_rows(
    tmp_path: Path,
) -> None:
    rows = _build_training_rows(30)
    _assert_default_loader_skips_invalid_last_row(
        tmp_path,
        missing_diagnostic_feature_dates={str(rows[-1]["feature_date"])},
    )


def test_retrain_entrypoint_default_loader_excludes_multi_source_rows(
    tmp_path: Path,
) -> None:
    rows = _build_training_rows(30)
    _assert_default_loader_skips_invalid_last_row(
        tmp_path,
        multi_source_feature_dates={str(rows[-1]["feature_date"])},
    )


def test_retrain_entrypoint_default_loader_excludes_eight_fallback_rows(
    tmp_path: Path,
) -> None:
    rows = _build_training_rows(30)
    _assert_default_loader_skips_invalid_last_row(
        tmp_path,
        eight_fallback_feature_dates={str(rows[-1]["feature_date"])},
    )


def test_retrain_entrypoint_default_loader_excludes_non_oura_stage_rows(
    tmp_path: Path,
) -> None:
    rows = _build_training_rows(30)
    _assert_default_loader_skips_invalid_last_row(
        tmp_path,
        non_oura_stage_feature_dates={str(rows[-1]["feature_date"])},
    )


def test_retrain_entrypoint_trains_logs_gate_metrics_and_persists_artifacts(
    tmp_path: Path,
) -> None:
    eval_log_path = tmp_path / "models" / "eval.jsonl"
    model_dir = tmp_path / "models"

    report = run_retrain(
        root=tmp_path,
        eval_log_path=eval_log_path,
        model_dir=model_dir,
        run_date="2026-06-11",
        provider_preflight_runner=_ok_preflight,
        feature_row_loader=lambda: _build_training_rows(48, residual_scale=0.2),
    )

    records = _read_eval_records(eval_log_path)
    record = records[0]

    assert report["status"] == "trained"
    assert report["record"] == record
    assert record["n_model"] == 48
    assert record["n_eval_days"] == 14
    assert record["ridge_walk_forward_rmse"] is not None
    assert record["baseline_rolling_mean_rmse"] is not None
    assert record["baseline_prior_day_rmse"] is not None
    assert record["ablation_prior_mood_only_rmse"] is not None
    assert record["ablation_sleep_features_only_rmse"] is not None
    assert record["confidence_label"] in {"high", "medium", "low"}
    assert len(record["feature_sign_stability"]) == 4
    assert len(record["latest_contributions"]) == 4
    assert {item["feature_name"] for item in record["latest_contributions"]} == {
        "deep_sleep_pct",
        "hrv_z",
        "prior_day_feeling",
        "total_sleep_min",
    }
    assert Path(report["artifacts"]["model_path"]).exists()
    assert Path(report["artifacts"]["scaler_path"]).exists()

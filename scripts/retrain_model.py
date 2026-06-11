#!/usr/bin/env python3
"""Nightly S05 retrain entrypoint with eval log writing."""

from __future__ import annotations

import argparse
from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, date, datetime
import json
import pickle
from pathlib import Path
import subprocess
import sys
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.model.baseline_gate import BaselineGateResult, evaluate_baseline_gate
from src.model.eval_log import append_eval_record, confidence_label_for_interval
from src.model.ridge import (
    FeatureContribution,
    FeatureSignStability,
    MODEL_FEATURES,
    PredictionInterval,
    RidgePredictor,
)
from scripts.verify_s05_provider_policy import collect_model_training_input_violations


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DATABASE_PATH = REPO_ROOT / "data" / "warehouse.duckdb"
DEFAULT_MODEL_DIR = REPO_ROOT / "models"
DEFAULT_EVAL_LOG_PATH = DEFAULT_MODEL_DIR / "eval.jsonl"
DEFAULT_MODEL_VERSION = "ridge-v1.0"
DEFAULT_FEATURE_VERSION = "v1.0"
MIN_MODEL_ROWS_FOR_TRAIN = 30
REQUIRED_STAGE_SOURCE = "oura"

FeatureRowLoader = Callable[[], Sequence[Mapping[str, Any]]]
ProviderPreflightRunner = Callable[[Path], Mapping[str, Any]]


def run_provider_preflight(root: Path) -> dict[str, Any]:
    process = subprocess.run(
        [sys.executable, "scripts/verify_s05_provider_policy.py", "--json"],
        cwd=str(root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    stdout = process.stdout.strip()
    if not stdout:
        stderr = process.stderr.strip()
        raise RuntimeError(
            "provider preflight produced no JSON output"
            + (f": {stderr}" if stderr else "")
        )

    try:
        report = json.loads(stdout)
    except json.JSONDecodeError as error:
        raise RuntimeError("provider preflight returned invalid JSON") from error

    if not isinstance(report, dict):
        raise RuntimeError("provider preflight JSON payload must be an object")
    return report


def load_verified_feature_rows(database_path: Path) -> list[dict[str, Any]]:
    database = Path(database_path)
    if not database.exists():
        raise FileNotFoundError(
            f"S04 model-ready feature database not available: {database}"
        )

    import duckdb

    connection = duckdb.connect(str(database), read_only=True)
    try:
        rows = connection.execute(
            """
            SELECT
                CAST(f.feature_date AS VARCHAR) AS feature_date,
                CAST(f.total_sleep_min AS DOUBLE) AS total_sleep_min,
                f.hrv_z,
                f.deep_sleep_pct,
                CAST(f.prior_day_feeling AS DOUBLE) AS prior_day_feeling,
                CAST(e.feeling AS DOUBLE) AS feeling,
                f.hrv_avg_ms,
                f.feature_version,
                CAST(f.sleep_source_count AS BIGINT) AS sleep_source_count,
                d.hrv_merge_method,
                d.stage_source
            FROM daily_features f
            JOIN mood_current c ON c.mood_date = f.feature_date
            JOIN mood_entries e ON e.log_id = c.log_id
            JOIN sleep_merge_diagnostics d ON d.sleep_date = f.feature_date
            WHERE f.total_sleep_min IS NOT NULL
              AND f.hrv_z IS NOT NULL
              AND f.deep_sleep_pct IS NOT NULL
              AND f.prior_day_feeling IS NOT NULL
              AND COALESCE(f.prior_day_feeling_imputed, FALSE) = FALSE
              AND f.sleep_source_count = 1
              AND d.hrv_merge_method IS NOT NULL
              AND d.stage_source IS NOT NULL
              AND COALESCE(d.hrv_merge_method, 'oura_primary') != 'eight_fallback'
              AND COALESCE(LOWER(TRIM(d.stage_source)), '') = 'oura'
            ORDER BY f.feature_date
            """
        ).fetchall()
    except duckdb.Error as error:
        raise RuntimeError(
            f"failed to load verified S04 model-ready feature rows: {error}"
        ) from error
    finally:
        connection.close()

    loaded_rows: list[dict[str, Any]] = []
    provider_policy_rows: list[dict[str, Any]] = []
    for (
        feature_date,
        total_sleep_min,
        hrv_z,
        deep_sleep_pct,
        prior_day_feeling,
        feeling,
        hrv_avg_ms,
        feature_version,
        sleep_source_count,
        hrv_merge_method,
        stage_source,
    ) in rows:
        loaded_rows.append(
            {
                "feature_date": feature_date,
                "total_sleep_min": total_sleep_min,
                "hrv_z": hrv_z,
                "deep_sleep_pct": deep_sleep_pct,
                "prior_day_feeling": prior_day_feeling,
                "feeling": feeling,
                "hrv_avg_ms": hrv_avg_ms,
                "feature_version": feature_version,
            }
        )
        provider_policy_rows.append(
            {
                "feature_date": feature_date,
                "source": "oura",
                "sleep_source_count": sleep_source_count,
                "hrv_merge_method": hrv_merge_method,
                "stage_source": stage_source,
            }
        )

    violations = collect_model_training_input_violations(provider_policy_rows)
    violations.extend(_collect_stage_source_violations(provider_policy_rows))
    if violations:
        raise RuntimeError(
            "loaded S04 model-ready feature rows violate provider policy: "
            + "; ".join(violations)
        )

    return loaded_rows


def run_retrain(
    *,
    root: Path = REPO_ROOT,
    database_path: Path = DEFAULT_DATABASE_PATH,
    eval_log_path: Path = DEFAULT_EVAL_LOG_PATH,
    model_dir: Path = DEFAULT_MODEL_DIR,
    persist_artifacts: bool = True,
    run_date: str | date | None = None,
    provider_preflight_runner: ProviderPreflightRunner = run_provider_preflight,
    feature_row_loader: FeatureRowLoader | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    eval_log_path = Path(eval_log_path)
    model_dir = Path(model_dir)
    artifacts: dict[str, str] = {}

    try:
        preflight_report = dict(provider_preflight_runner(root))
        if preflight_report.get("status") != "ok":
            return {
                "status": "error",
                "errors": _preflight_errors(preflight_report),
                "record": None,
                "artifacts": artifacts,
                "eval_log_path": str(eval_log_path),
            }

        loader = feature_row_loader or (
            lambda: load_verified_feature_rows(Path(database_path))
        )
        normalized_rows, model_rows, targets = _normalize_training_rows(loader())
        record_date = _normalize_run_date(run_date).isoformat()
        feature_version = _resolve_feature_version(normalized_rows)
        trained_through_date = (
            normalized_rows[-1]["feature_date"] if normalized_rows else None
        )

        if len(model_rows) < MIN_MODEL_ROWS_FOR_TRAIN:
            record = append_eval_record(
                eval_log_path,
                _build_skipped_record(
                    record_date=record_date,
                    trained_through_date=trained_through_date,
                    n_model=len(model_rows),
                    feature_version=feature_version,
                    model_version=DEFAULT_MODEL_VERSION,
                ),
            )
            return {
                "status": "skipped",
                "errors": [],
                "record": record,
                "artifacts": artifacts,
                "eval_log_path": str(eval_log_path),
            }

        predictor = RidgePredictor().fit(model_rows, targets)
        gate_result = evaluate_baseline_gate(model_rows, targets)
        prior_only_gate = evaluate_baseline_gate(
            _apply_feature_ablation(model_rows, keep_features={"prior_day_feeling"}),
            targets,
        )
        sleep_only_gate = evaluate_baseline_gate(
            _apply_feature_ablation(
                model_rows,
                keep_features={"total_sleep_min", "hrv_z", "deep_sleep_pct"},
            ),
            targets,
        )

        latest_explanation = predictor.explain([model_rows[-1]])[0]
        latest_interval = predictor.predict_interval([model_rows[-1]])[0]
        sign_stability = predictor.feature_sign_stability()
        if persist_artifacts:
            artifacts = _persist_model_artifacts(
                model_dir=model_dir,
                trained_through_date=trained_through_date or record_date,
                predictor=predictor,
            )

        record = append_eval_record(
            eval_log_path,
            _build_trained_record(
                record_date=record_date,
                trained_through_date=trained_through_date,
                normalized_rows=normalized_rows,
                model_rows=model_rows,
                targets=targets,
                predictor=predictor,
                gate_result=gate_result,
                prior_only_gate=prior_only_gate,
                sleep_only_gate=sleep_only_gate,
                latest_explanation=latest_explanation,
                latest_interval=latest_interval,
                sign_stability=sign_stability,
                feature_version=feature_version,
                model_version=DEFAULT_MODEL_VERSION,
                artifacts=artifacts,
            ),
        )
        return {
            "status": "trained",
            "errors": [],
            "record": record,
            "artifacts": artifacts,
            "eval_log_path": str(eval_log_path),
        }
    except Exception as error:  # pragma: no cover - exercised by CLI callers
        return {
            "status": "error",
            "errors": [f"{type(error).__name__}: {error}"],
            "record": None,
            "artifacts": artifacts,
            "eval_log_path": str(eval_log_path),
        }


def main(
    argv: list[str] | None = None,
    *,
    provider_preflight_runner: ProviderPreflightRunner = run_provider_preflight,
    feature_row_loader: FeatureRowLoader | None = None,
) -> int:
    parser = argparse.ArgumentParser(
        description="Run the nightly S05 retrain entrypoint and append eval.jsonl."
    )
    parser.add_argument("--root", default=str(REPO_ROOT))
    parser.add_argument("--database", default=str(DEFAULT_DATABASE_PATH))
    parser.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR))
    parser.add_argument("--eval-log-path")
    parser.add_argument("--run-date")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--no-persist-artifacts", action="store_true")
    args = parser.parse_args(argv)

    root = Path(args.root).resolve()
    model_dir = Path(args.model_dir).resolve()
    eval_log_path = (
        Path(args.eval_log_path).resolve()
        if args.eval_log_path
        else model_dir / "eval.jsonl"
    )

    # Training data comes EXCLUSIVELY from the verified S04 model-ready loader;
    # no public CLI path may inject alternate feature rows. The
    # feature_row_loader parameter remains for in-process unit tests only.
    loader = feature_row_loader

    report = run_retrain(
        root=root,
        database_path=Path(args.database).resolve(),
        eval_log_path=eval_log_path,
        model_dir=model_dir,
        persist_artifacts=not args.no_persist_artifacts,
        run_date=args.run_date,
        provider_preflight_runner=provider_preflight_runner,
        feature_row_loader=loader,
    )

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report.get("errors", []):
            print(f"ERROR: {error}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] in {"skipped", "trained"} else 1


def _normalize_training_rows(
    raw_rows: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[float]]:
    normalized_rows: list[dict[str, Any]] = []
    for row in raw_rows:
        feature_date = _normalize_feature_date(_row_value(row, "feature_date"))
        normalized_row: dict[str, Any] = {
            "feature_date": feature_date,
            "feeling": _as_float(_row_value(row, "feeling"), field="feeling"),
        }
        for feature_name in MODEL_FEATURES:
            normalized_row[feature_name] = _as_float(
                _row_value(row, feature_name),
                field=feature_name,
            )
        hrv_avg_ms = _row_value(row, "hrv_avg_ms")
        if hrv_avg_ms is not None:
            normalized_row["hrv_avg_ms"] = float(hrv_avg_ms)
        feature_version = _row_value(row, "feature_version")
        if feature_version not in {None, ""}:
            normalized_row["feature_version"] = str(feature_version)
        normalized_rows.append(normalized_row)

    normalized_rows.sort(key=lambda item: item["feature_date"])
    model_rows = [
        {
            "feature_date": row["feature_date"],
            **{feature_name: row[feature_name] for feature_name in MODEL_FEATURES},
            **({"hrv_avg_ms": row["hrv_avg_ms"]} if "hrv_avg_ms" in row else {}),
        }
        for row in normalized_rows
    ]
    targets = [float(row["feeling"]) for row in normalized_rows]
    return normalized_rows, model_rows, targets


def _normalize_feature_date(value: Any) -> str:
    if isinstance(value, date):
        return value.isoformat()
    if value in {None, ""}:
        raise ValueError("training rows must include feature_date")
    return str(value)


def _collect_stage_source_violations(rows: Iterable[Mapping[str, Any]]) -> list[str]:
    violations: list[str] = []
    for index, row in enumerate(rows):
        stage_source = _normalize_provider_value(row.get("stage_source"))
        if stage_source != REQUIRED_STAGE_SOURCE:
            violations.append(
                "training input row "
                f"{index} uses stage_source={row.get('stage_source')!r} for deep_sleep_pct under Oura-only v1"
            )
    return violations


def _normalize_run_date(value: str | date | None) -> date:
    if value is None:
        return datetime.now(UTC).date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(value)


def _resolve_feature_version(rows: Sequence[Mapping[str, Any]]) -> str:
    versions = {
        str(row["feature_version"])
        for row in rows
        if row.get("feature_version") not in {None, ""}
    }
    if not versions:
        return DEFAULT_FEATURE_VERSION
    if len(versions) == 1:
        return next(iter(versions))
    return "mixed"


def _apply_feature_ablation(
    rows: Sequence[Mapping[str, Any]],
    *,
    keep_features: set[str],
) -> list[dict[str, Any]]:
    ablated_rows: list[dict[str, Any]] = []
    for row in rows:
        ablated_row = {"feature_date": str(row["feature_date"])}
        for feature_name in MODEL_FEATURES:
            ablated_row[feature_name] = float(row[feature_name]) if feature_name in keep_features else 0.0
        ablated_rows.append(ablated_row)
    return ablated_rows


def _build_skipped_record(
    *,
    record_date: str,
    trained_through_date: str | None,
    n_model: int,
    feature_version: str,
    model_version: str,
) -> dict[str, Any]:
    return {
        "date": record_date,
        "recorded_at_utc": datetime.now(UTC),
        "trained_through_date": trained_through_date,
        "status": "skipped",
        "skip_reason": "below_minimum_training_rows",
        "n_model": n_model,
        "n_eval_days": 0,
        "eval_window_days": 0,
        "baseline_gate_eligible": False,
        "baseline_gate_passed": False,
        "baseline_gate_reason": "below_minimum_training_rows",
        "ridge_walk_forward_rmse": None,
        "ridge_all_features_rmse": None,
        "baseline_rolling_mean_rmse": None,
        "baseline_prior_day_rmse": None,
        "best_baseline_rmse": None,
        "ridge_to_best_baseline_rmse_ratio": None,
        "ridge_better_day_count": 0,
        "better_day_threshold": 0,
        "ablation_prior_mood_only_rmse": None,
        "ablation_sleep_features_only_rmse": None,
        "ridge_prior_mood_only_rmse": None,
        "ridge_sleep_features_only_rmse": None,
        "sign_stable_features": [],
        "visible_contributor_features": [],
        "feature_sign_stability": [],
        "latest_contributions": [],
        "latest_feature_values": {},
        "latest_display_metadata": {},
        "latest_logged_feeling": None,
        "latest_prediction_interval": None,
        "confidence_label": None,
        "r2_in_sample_diagnostic_only": None,
        "model_version": model_version,
        "feature_version": feature_version,
        "persisted_model_path": None,
        "persisted_scaler_path": None,
    }


def _build_trained_record(
    *,
    record_date: str,
    trained_through_date: str | None,
    normalized_rows: Sequence[Mapping[str, Any]],
    model_rows: Sequence[Mapping[str, Any]],
    targets: Sequence[float],
    predictor: RidgePredictor,
    gate_result: BaselineGateResult,
    prior_only_gate: BaselineGateResult,
    sleep_only_gate: BaselineGateResult,
    latest_explanation: Any,
    latest_interval: PredictionInterval,
    sign_stability: Sequence[FeatureSignStability],
    feature_version: str,
    model_version: str,
    artifacts: Mapping[str, str],
) -> dict[str, Any]:
    sign_stability_payload = [
        _serialize_feature_sign_stability(item) for item in sign_stability
    ]
    contribution_payload = _serialize_contributions(
        latest_explanation.contributions
    )
    training_predictions = predictor.predict(model_rows)

    stable_features = [
        item["feature_name"]
        for item in sign_stability_payload
        if item["stability_label"] == "stable"
    ]
    visible_features = [
        item["feature_name"]
        for item in sign_stability_payload
        if item["stability_label"] != "suppressed"
    ]

    latest_row = normalized_rows[-1]
    return {
        "date": record_date,
        "recorded_at_utc": datetime.now(UTC),
        "trained_through_date": trained_through_date,
        "status": "trained",
        "skip_reason": None,
        "n_model": len(model_rows),
        "n_eval_days": gate_result.n_eval_days,
        "eval_window_days": gate_result.eval_window_days,
        "baseline_gate_eligible": gate_result.baseline_gate_eligible,
        "baseline_gate_passed": gate_result.baseline_gate_passed,
        "baseline_gate_reason": gate_result.gate_reason,
        "ridge_walk_forward_rmse": gate_result.ridge_walk_forward_rmse,
        "ridge_all_features_rmse": gate_result.ridge_walk_forward_rmse,
        "baseline_rolling_mean_rmse": gate_result.baseline_rolling_mean_rmse,
        "baseline_prior_day_rmse": gate_result.baseline_prior_day_rmse,
        "best_baseline_rmse": gate_result.best_baseline_rmse,
        "ridge_to_best_baseline_rmse_ratio": gate_result.ridge_to_best_baseline_rmse_ratio,
        "ridge_better_day_count": gate_result.ridge_better_day_count,
        "better_day_threshold": gate_result.better_day_threshold,
        "ablation_prior_mood_only_rmse": prior_only_gate.ridge_walk_forward_rmse,
        "ablation_sleep_features_only_rmse": sleep_only_gate.ridge_walk_forward_rmse,
        "ridge_prior_mood_only_rmse": prior_only_gate.ridge_walk_forward_rmse,
        "ridge_sleep_features_only_rmse": sleep_only_gate.ridge_walk_forward_rmse,
        "sign_stable_features": stable_features,
        "visible_contributor_features": visible_features,
        "feature_sign_stability": sign_stability_payload,
        "latest_contributions": contribution_payload,
        "latest_feature_values": {
            feature_name: float(latest_row[feature_name]) for feature_name in MODEL_FEATURES
        },
        "latest_display_metadata": _latest_display_metadata(latest_row),
        "latest_logged_feeling": float(latest_row["feeling"]),
        "latest_prediction_interval": {
            "prediction": latest_interval.prediction,
            "low": latest_interval.low,
            "high": latest_interval.high,
            "full_width": latest_interval.full_width,
        },
        "confidence_label": confidence_label_for_interval(latest_interval.full_width),
        "r2_in_sample_diagnostic_only": _r2_score(targets, training_predictions),
        "model_version": model_version,
        "feature_version": feature_version,
        "persisted_model_path": artifacts.get("model_path"),
        "persisted_scaler_path": artifacts.get("scaler_path"),
    }


def _latest_display_metadata(row: Mapping[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if "hrv_avg_ms" in row:
        metadata["hrv_avg_ms"] = float(row["hrv_avg_ms"])
    return metadata


def _serialize_feature_sign_stability(
    item: FeatureSignStability,
) -> dict[str, Any]:
    return {
        "feature_name": item.feature_name,
        "coefficient": item.coefficient,
        "sign_stability_pct": item.sign_stability_pct,
        "stability_label": item.stability_label,
    }


def _serialize_contributions(
    contributions: Sequence[FeatureContribution],
) -> list[dict[str, Any]]:
    total_abs_contribution = sum(abs(item.contribution) for item in contributions) or 1.0
    return [
        {
            "feature_name": item.feature_name,
            "feature_value": item.feature_value,
            "scaled_value": item.scaled_value,
            "coefficient": item.coefficient,
            "contribution": item.contribution,
            "pct_of_abs_contribution": abs(item.contribution) / total_abs_contribution,
            "sign_stability_pct": item.sign_stability_pct,
            "stability_label": item.stability_label,
        }
        for item in contributions
    ]


def _persist_model_artifacts(
    *,
    model_dir: Path,
    trained_through_date: str,
    predictor: RidgePredictor,
) -> dict[str, str]:
    model_dir.mkdir(parents=True, exist_ok=True)
    model_path = model_dir / f"ridge-{trained_through_date}.pkl"
    scaler_path = model_dir / f"scaler-{trained_through_date}.pkl"

    with model_path.open("wb") as handle:
        pickle.dump(
            {
                "model_version": DEFAULT_MODEL_VERSION,
                "model": predictor.model_,
                "feature_names": MODEL_FEATURES,
            },
            handle,
            protocol=pickle.HIGHEST_PROTOCOL,
        )
    with scaler_path.open("wb") as handle:
        pickle.dump(predictor.scaler_, handle, protocol=pickle.HIGHEST_PROTOCOL)

    return {
        "model_path": str(model_path),
        "scaler_path": str(scaler_path),
    }


def _r2_score(targets: Sequence[float], predictions: Sequence[float]) -> float:
    if not targets:
        return 0.0
    mean_target = sum(targets) / float(len(targets))
    residual_sum = sum((actual - predicted) ** 2 for actual, predicted in zip(targets, predictions))
    total_sum = sum((actual - mean_target) ** 2 for actual in targets)
    if total_sum == 0.0:
        return 1.0 if residual_sum == 0.0 else 0.0
    return 1.0 - (residual_sum / total_sum)


def _preflight_errors(report: Mapping[str, Any]) -> list[str]:
    errors = report.get("errors")
    if isinstance(errors, list) and errors:
        return [str(error) for error in errors]
    return ["provider preflight failed"]


def _row_value(row: Mapping[str, Any], field: str) -> Any:
    return row.get(field)


def _normalize_provider_value(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _as_float(value: Any, *, field: str) -> float:
    if value is None:
        raise ValueError(f"training rows must include {field}")
    try:
        return float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"training row field is not numeric: {field}") from error


if __name__ == "__main__":
    raise SystemExit(main())

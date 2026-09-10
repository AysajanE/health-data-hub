#!/usr/bin/env python3
"""Scheduled retrain wrapper that prints only an allowlisted aggregate summary.

``scripts/retrain_model.py --json`` prints the full evaluation record, which
includes the latest feature values and the latest logged rating. That is fine
for an interactive terminal but must never land in a launchd log. This wrapper
runs the same retrain, honors ``HEALTH_HUB_DATABASE_PATH`` from the environment
or ``.env.local``, secures the model directory afterwards, and prints a summary
that carries counts, gate outcomes, and error type names only.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any, Mapping

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.retrain_model import run_retrain
from scripts.setup_permissions import setup_permissions
from src.config.env_file import DEFAULT_ENV_FILE, resolve_env
from src.warehouse.warehouse import DEFAULT_DATABASE_PATH


REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MODEL_DIR = REPO_ROOT / "models"
SUMMARY_KEYS = (
    "trained_through_date",
    "n_model",
    "n_eval_days",
    "baseline_gate_eligible",
    "baseline_gate_passed",
    "baseline_gate_reason",
    "ridge_walk_forward_rmse",
    "best_baseline_rmse",
    "ridge_to_best_baseline_rmse_ratio",
    "ridge_better_day_count",
    "better_day_threshold",
    "sign_stable_features",
    "model_version",
    "feature_version",
)


def resolve_database_path(env_file: Path = DEFAULT_ENV_FILE) -> Path:
    settings = resolve_env(("HEALTH_HUB_DATABASE_PATH",), env_file=env_file)
    value = settings.get("HEALTH_HUB_DATABASE_PATH") or ""
    return Path(value).expanduser() if value else DEFAULT_DATABASE_PATH


def secure_model_dir(model_dir: Path) -> dict[str, Any]:
    """Make the selected model directory 0700 and its files 0600, refusing symlinks."""

    errors: list[str] = []
    try:
        absolute = model_dir.absolute()
        if any(candidate.is_symlink() for candidate in (absolute, *absolute.parents)):
            raise OSError("model directory or one of its ancestors is a symlink")
        model_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        model_dir.chmod(0o700)
        for path in model_dir.rglob("*"):
            if path.is_symlink():
                errors.append("symlink inside model directory")
                continue
            path.chmod(0o700 if path.is_dir() else 0o600)
    except OSError as error:
        errors.append(type(error).__name__)
    return {"status": "ok" if not errors else "error", "errors": errors}


def _error_type_names(errors: Any) -> list[str]:
    names: list[str] = []
    for item in errors if isinstance(errors, list) else []:
        text = str(item)
        names.append(text.split(":", 1)[0].strip() if ":" in text else "error")
    return names


def summarize(report: Mapping[str, Any]) -> dict[str, Any]:
    """Reduce a retrain report to fields that contain no health values."""

    record = report.get("record") if isinstance(report.get("record"), Mapping) else {}
    summary: dict[str, Any] = {
        "status": report.get("status"),
        "error_types": _error_type_names(report.get("errors")),
    }
    for key in SUMMARY_KEYS:
        if key in record:
            summary[key] = record[key]
    return summary


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the nightly retrain and print a redacted summary.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--model-dir", default=str(DEFAULT_MODEL_DIR))
    args = parser.parse_args(argv)

    database_path = resolve_database_path(Path(args.env_file).expanduser())
    model_dir = Path(args.model_dir).expanduser()

    # The S05 provider preflight audits only the default warehouse, so a
    # scheduled retrain against an overridden database would be unaudited.
    # Fail closed instead of training against a warehouse the gate never saw.
    if database_path.resolve() != DEFAULT_DATABASE_PATH.resolve():
        print(json.dumps({"status": "error", "error_types": ["UnsupportedDatabaseOverride"]}, sort_keys=True))
        return 1

    # Secure the output location before any artifact or eval record is written,
    # then again afterwards so newly created files end up owner-only too.
    before = secure_model_dir(model_dir)
    report = (
        run_retrain(
            root=REPO_ROOT,
            database_path=database_path,
            eval_log_path=model_dir / "eval.jsonl",
            model_dir=model_dir,
        )
        if before["status"] == "ok"
        else {"status": "error", "errors": ["OSError: model directory could not be secured"], "record": None}
    )
    after = secure_model_dir(model_dir)
    repo_permissions = setup_permissions(REPO_ROOT)
    permissions_ok = before["status"] == "ok" and after["status"] == "ok" and repo_permissions.get("status") == "ok"

    summary = summarize(report)
    summary["permissions"] = "ok" if permissions_ok else "error"
    print(json.dumps(summary, sort_keys=True))
    return 0 if summary["status"] in {"skipped", "trained"} and permissions_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())

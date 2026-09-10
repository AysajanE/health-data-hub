from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from io import StringIO
from stat import S_IMODE
from unittest.mock import patch

from scripts import nightly_retrain
from scripts.nightly_retrain import resolve_database_path, secure_model_dir, summarize


FULL_TRAINED_REPORT = {
    "status": "trained",
    "errors": [],
    "record": {
        "date": "2026-10-20",
        "trained_through_date": "2026-10-19",
        "status": "trained",
        "n_model": 40,
        "n_eval_days": 7,
        "baseline_gate_eligible": True,
        "baseline_gate_passed": False,
        "baseline_gate_reason": "failed_rmse_ratio",
        "ridge_walk_forward_rmse": 1.42,
        "best_baseline_rmse": 1.40,
        "ridge_to_best_baseline_rmse_ratio": 1.01,
        "ridge_better_day_count": 3,
        "better_day_threshold": 5,
        "sign_stable_features": ["total_sleep_min"],
        "latest_feature_values": {"total_sleep_min": 411.0, "hrv_z": -0.9},
        "latest_display_metadata": {"hrv_avg_ms": 38.0},
        "latest_logged_feeling": 4.0,
        "latest_contributions": [{"feature_name": "hrv_z", "feature_value": -0.9}],
        "latest_prediction_interval": {"low": 3.1, "high": 5.9},
        "model_version": "ridge-v1.0",
        "feature_version": "v1.0",
    },
    "artifacts": {"model_path": "/models/ridge-2026-10-19.pkl"},
}


class SummarizeTest(unittest.TestCase):
    def test_summary_keeps_gate_metrics_and_drops_every_health_value(self) -> None:
        summary = summarize(FULL_TRAINED_REPORT)
        serialized = json.dumps(summary)

        self.assertEqual(summary["status"], "trained")
        self.assertEqual(summary["n_model"], 40)
        self.assertEqual(summary["baseline_gate_reason"], "failed_rmse_ratio")
        self.assertEqual(summary["sign_stable_features"], ["total_sleep_min"])
        for forbidden in (
            "latest_feature_values",
            "latest_display_metadata",
            "latest_logged_feeling",
            "latest_contributions",
            "latest_prediction_interval",
            "411",
            "38.0",
            "4.0",
            "artifacts",
        ):
            self.assertNotIn(forbidden, serialized)

    def test_errors_are_reduced_to_type_names(self) -> None:
        summary = summarize(
            {
                "status": "error",
                "errors": ["FileNotFoundError: S04 model-ready feature database not available: /secret/path"],
                "record": None,
            }
        )
        self.assertEqual(summary, {"status": "error", "error_types": ["FileNotFoundError"]})

    def test_skipped_report_summary(self) -> None:
        summary = summarize(
            {
                "status": "skipped",
                "errors": [],
                "record": {"n_model": 3, "baseline_gate_reason": "below_minimum_training_rows"},
            }
        )
        self.assertEqual(summary["status"], "skipped")
        self.assertEqual(summary["n_model"], 3)


class SecureModelDirTest(unittest.TestCase):
    def test_directory_and_files_become_owner_only(self) -> None:
        with TemporaryDirectory() as tempdir:
            # resolve(): macOS temp dirs live under /var, itself a symlink.
            model_dir = Path(tempdir).resolve() / "models"
            model_dir.mkdir(mode=0o755)
            (model_dir / "eval.jsonl").write_text("{}\n", encoding="utf-8")
            (model_dir / "eval.jsonl").chmod(0o644)

            result = secure_model_dir(model_dir)

            self.assertEqual(result["status"], "ok")
            self.assertEqual(S_IMODE(model_dir.stat().st_mode), 0o700)
            self.assertEqual(S_IMODE((model_dir / "eval.jsonl").stat().st_mode), 0o600)

    def test_symlink_ancestor_is_refused_without_touching_the_target(self) -> None:
        with TemporaryDirectory() as tempdir:
            real_parent = Path(tempdir).resolve() / "real"
            real_parent.mkdir(mode=0o755)
            target = real_parent / "models"
            target.mkdir(mode=0o755)
            (target / "eval.jsonl").write_text("{}\n", encoding="utf-8")
            (target / "eval.jsonl").chmod(0o644)
            linked_parent = Path(tempdir).resolve() / "linked"
            linked_parent.symlink_to(real_parent, target_is_directory=True)

            result = secure_model_dir(linked_parent / "models")

            self.assertEqual(result["status"], "error")
            self.assertEqual(S_IMODE(target.stat().st_mode), 0o755)
            self.assertEqual(S_IMODE((target / "eval.jsonl").stat().st_mode), 0o644)


class DatabaseOverrideIsRejectedTest(unittest.TestCase):
    def test_scheduled_retrain_refuses_an_unaudited_database_override(self) -> None:
        with TemporaryDirectory() as tempdir:
            env_file = Path(tempdir) / ".env.local"
            env_file.write_text(f"HEALTH_HUB_DATABASE_PATH={tempdir}/other.duckdb\n", encoding="utf-8")
            with patch("sys.stdout", new_callable=StringIO) as stdout, patch.object(
                nightly_retrain, "run_retrain"
            ) as run_retrain:
                code = nightly_retrain.main(["--env-file", str(env_file), "--model-dir", f"{tempdir}/models"])

        self.assertEqual(code, 1)
        run_retrain.assert_not_called()
        self.assertEqual(
            json.loads(stdout.getvalue()),
            {"status": "error", "error_types": ["UnsupportedDatabaseOverride"]},
        )


class ResolveDatabasePathTest(unittest.TestCase):
    def test_env_file_override_and_default(self) -> None:
        with TemporaryDirectory() as tempdir:
            env_file = Path(tempdir) / ".env.local"
            env_file.write_text("HEALTH_HUB_DATABASE_PATH=/tmp/elsewhere/warehouse.duckdb\n", encoding="utf-8")
            self.assertEqual(resolve_database_path(env_file), Path("/tmp/elsewhere/warehouse.duckdb"))
            self.assertTrue(
                str(resolve_database_path(Path(tempdir) / "missing.env")).endswith("data/warehouse.duckdb")
            )


if __name__ == "__main__":
    unittest.main()

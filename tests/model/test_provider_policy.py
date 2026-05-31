from __future__ import annotations

from pathlib import Path

from scripts.verify_s05_provider_policy import scan_model_training_code


REPO_ROOT = Path(__file__).resolve().parents[2]


def test_model_training_excludes_8sleep_under_oura_only_v1() -> None:
    assert scan_model_training_code(REPO_ROOT) == []

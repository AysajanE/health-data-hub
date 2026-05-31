# S05 Model Lifecycle Autonomous Brief

Autonomy profile: guarded zero-supervision for S05 only.

Manual gates are forbidden. High-risk model work must use autonomous_gate_review artifacts, deterministic tests, and recorded command evidence instead of human signoff.

## Scope

S05 trains and evaluates the Health Data Hub v1 retrospective model after S04 feature engineering is complete. It must preserve the Oura-only v1 provider policy from S03/S04.

## Provider Policy Requirements

- S05 must run `python scripts/verify_s05_provider_policy.py --json` before model training.
- S03 and S04 must be complete before model training begins.
- The active sleep provider for v1 must be Oura.
- 8 Sleep / pyEight must remain `fallback_active`.
- No model feature row may have `sleep_source_count > 1`.
- Model training queries must not read `source = '8sleep'`.
- Model training must not use `hrv_merge_method = 'eight_fallback'`.
- Add or preserve `test_model_training_excludes_8sleep_under_oura_only_v1`.

## Required Model Invariants

- Model features are exactly `total_sleep_min`, `hrv_z`, `deep_sleep_pct`, and `prior_day_feeling`.
- `hrv_avg_ms` is display metadata only.
- No sleep forward-fill for training.
- Mood labels are never imputed.
- S05 must not weaken model gates to make the UI more interesting.

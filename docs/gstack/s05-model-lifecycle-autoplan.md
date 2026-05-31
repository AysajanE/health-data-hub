# S05 Model Lifecycle Autoplan

Slice ID: S05
Lane: swr_preferred
Risk: high

## Scope

Train and evaluate the v1 retrospective model from S04 feature rows. Preserve the active S03/S04 provider policy: Oura is active; 8 Sleep / pyEight is fallback-only and not a model feature source.

## Constraints

- Manual gates are forbidden; use autonomous_gate_review artifacts for high-risk model acceptance.
- S05 must run `python scripts/verify_s05_provider_policy.py --json` before model training.
- S03 and S04 must be complete.
- No model row may have `sleep_source_count > 1`.
- No training query may read `source = '8sleep'`.
- No training query may use `hrv_merge_method = 'eight_fallback'`.
- Model features are exactly `total_sleep_min`, `hrv_z`, `deep_sleep_pct`, and `prior_day_feeling`.

## Deliverables

- `scripts/verify_s05_provider_policy.py`
- `tests/model/test_provider_policy.py`
- `docs/reviews/s05-autonomous-model-gate-review.md`
- `docs/reviews/s05-autonomous-statistical-validity-review.md`

## Implementation Tasks

### Provider-policy preflight gate

- [ ] Enforce the Oura-only v1 provider policy before any model training path runs.
  Files: `scripts/verify_s05_provider_policy.py`; `tests/model/test_provider_policy.py`
  Verify: `python scripts/verify_s05_provider_policy.py --json`; `python -m pytest tests/model -q`

### Model training exclusion test

- [ ] Add `test_model_training_excludes_8sleep_under_oura_only_v1` so S05 fails if model training reads inactive 8 Sleep fallback rows.
  Files: `tests/model/test_provider_policy.py`
  Verify: `python -m pytest tests/model -q`

### Autonomous model reviews

- [ ] Produce autonomous_gate_review artifacts covering model gate behavior and statistical validity.
  Files: `docs/reviews/s05-autonomous-model-gate-review.md`; `docs/reviews/s05-autonomous-statistical-validity-review.md`
  Verify: `python scripts/check_autonomous_review_exists.py S05`

## Verification Expectations

- `python scripts/verify_s05_provider_policy.py --json` returns `status: ok`.
- `python -m pytest tests/model -q` passes.
- `python scripts/check_autonomous_review_exists.py S05` passes.
- `python scripts/check_no_tracked_data.py` passes.

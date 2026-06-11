# S05 Model Lifecycle Autoplan

Slice ID: S05
Lane: swr_preferred
Risk: high
Revision: 3 (2026-06-11: removed the .gitignore write root — the models/ exclusion already exists at HEAD and .gitignore is a validator-forbidden sensitive write root; rows assert the exclusion via verification commands only. Revision 2: 2026-06-10 scope reconciliation; see docs/evidence/s05-autoplan-scope-reconciliation-20260610.json)

## Scope

Train and evaluate the v1 retrospective model from S04 feature rows, and ship the
model lifecycle the design doc assigns to S05: ridge training, nightly retrain
entrypoint, eval.jsonl walk-forward logging, baseline gating, bootstrap sign
stability, and conservative prediction intervals. Preserve the active S03/S04
provider policy: Oura is active; 8 Sleep / pyEight is fallback-only and not a
model feature source.

Authority: design doc section "Model Lifecycle" in
docs/gstack/health-data-hub-office-hours.md, and the S02 autoplan boundary note
that defers "S05 model training, baseline gates, sign-stability bootstrap, and
SHAP" to this slice.

## Constraints

- Manual gates are forbidden; use autonomous_gate_review artifacts for high-risk model acceptance.
- S05 must run `python scripts/verify_s05_provider_policy.py --json` before model training.
- S03 and S04 must be complete.
- No model row may have `sleep_source_count > 1`.
- No training query may read `source = '8sleep'`.
- No training query may use `hrv_merge_method = 'eight_fallback'`.
- Model features are exactly `total_sleep_min`, `hrv_z`, `deep_sleep_pct`, and `prior_day_feeling`.
- Model artifacts and evaluation logs contain derived health data: `models/` must remain gitignored and never tracked. The `models/` exclusion ALREADY EXISTS in `.gitignore` at HEAD; no S05 row may edit `.gitignore` (it is a validator-forbidden sensitive write root) — rows assert the exclusion via verification commands only.
- The retrain entrypoint no-ops gracefully when N_model < 30 paired days: it appends a skipped record to `models/eval.jsonl` and exits 0; no model file is required to exist before that point.
- Baseline gate (both must hold): ridge walk-forward RMSE at most 0.95 times the better of the rolling-7-day-mean and prior-day baselines, and ridge beats the best baseline on at least 65 percent of eval days (ceil rule from the design doc).
- Eval window by N_model: below 37 no gate and no UI output; 37 to 43 uses the last 7 days; 44 and above uses the last 14 days; every fold trains on at least 30 prior model-ready days.
- Bootstrap sign stability: 200 resamples on feature_date with replacement; tiers at 90 percent (show), 80 to 89 percent (low-confidence label), below 80 percent (suppress).
- Prediction intervals: bootstrap residuals, 90 percent interval, full-width floor of 2.0 mood points until N >= 60.
- launchd plist installation is out of scope for S05 (S08 owns launchd); S05 ships the retrain entrypoint those plists will invoke.
- SHAP-style contributor surfacing in the UI is out of scope for S05 (S07 owns UI); S05 persists sign-stability tiers and contributions that S06/S07 consume.

## Allowed Write Roots

- `src/model/`
- `scripts/retrain_model.py`
- `scripts/verify_s05_provider_policy.py`
- `tests/model/`
- `docs/reviews/s05-autonomous-model-gate-review.md`
- `docs/reviews/s05-autonomous-statistical-validity-review.md`

## Out of Scope

- Counterfactual generation (S06).
- Read API and Streamlit UI surfaces (S07).
- launchd plists, backups, restore (S08).
- Any change to warehouse schema, ingestion, or provider policy enforcement beyond reading it.
- Any new model features beyond the four fixed v1 features.

## Deliverables

- `src/model/ridge.py` (RidgePredictor: StandardScaler + Ridge alpha=1.0, fit/predict, bootstrap sign stability, bootstrap prediction intervals)
- `src/model/eval_log.py` (eval.jsonl record writer matching the design schema, including ablation fields)
- `src/model/baseline_gate.py` (rolling-mean and prior-day baselines, walk-forward evaluation, gate decision)
- `scripts/retrain_model.py` (nightly retrain entrypoint: provider-policy preflight, N_model guard, train, evaluate, gate, persist model + eval record)
- `scripts/verify_s05_provider_policy.py`
- `tests/model/test_provider_policy.py`
- `tests/model/test_ridge.py`
- `tests/model/test_baseline_gate.py`
- `tests/model/test_retrain_entrypoint.py`
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

### RidgePredictor and statistical machinery

- [ ] Implement `src/model/ridge.py` with a RidgePredictor wrapping StandardScaler plus Ridge alpha=1.0 over exactly the four v1 features, bootstrap sign stability (200 resamples on feature_date, tiers 90/80), and bootstrap 90 percent prediction intervals with the 2.0-point full-width floor.
  Files: `src/model/ridge.py`; `tests/model/test_ridge.py`
  Verify: `python -m pytest tests/model/test_ridge.py -q`

### Baseline gate and walk-forward evaluation

- [ ] Implement `src/model/baseline_gate.py` with rolling-7-day-mean and prior-day baselines, the N_model-dependent eval window (none below 37; last 7 days at 37 to 43; last 14 days at 44 and above), and the two-condition gate (0.95 RMSE ratio and 65 percent better-day count).
  Files: `src/model/baseline_gate.py`; `tests/model/test_baseline_gate.py`
  Verify: `python -m pytest tests/model/test_baseline_gate.py -q`

### Nightly retrain entrypoint and eval logging

- [ ] Implement `scripts/retrain_model.py` and `src/model/eval_log.py`: run the provider-policy preflight, no-op gracefully below N_model 30 (append a skipped eval.jsonl record and exit 0), otherwise train, run walk-forward evaluation and the baseline gate, persist the model with its fitted scaler under `models/` (already gitignored at HEAD; assert via verification, never edit `.gitignore`), and append a schema-complete eval.jsonl record including ablation RMSEs and sign-stable feature tiers.
  Files: `scripts/retrain_model.py`; `src/model/eval_log.py`; `tests/model/test_retrain_entrypoint.py`
  Verify: `python -m pytest tests/model/test_retrain_entrypoint.py -q`; `python scripts/check_no_tracked_data.py`; `python -c "from pathlib import Path; lines={l.strip() for l in Path('.gitignore').read_text(encoding='utf-8').splitlines()}; assert 'models/' in lines, 'models/ ignore entry missing'"

### Autonomous model reviews

- [ ] Produce autonomous_gate_review artifacts covering model gate behavior and statistical validity.
  Files: `docs/reviews/s05-autonomous-model-gate-review.md`; `docs/reviews/s05-autonomous-statistical-validity-review.md`
  Verify: `python scripts/check_autonomous_review_exists.py S05`

## Verification Expectations

- `python scripts/verify_s05_provider_policy.py --json` returns `status: ok`.
- `python -m pytest tests/model -q` passes.
- `python scripts/check_autonomous_review_exists.py S05` passes.
- `python scripts/check_no_tracked_data.py` passes.
- `models/` and its contents are never tracked by git.

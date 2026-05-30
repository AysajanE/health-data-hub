# S04 Feature Engineering Autoplan

Slice ID: S04
Lane: compiler
Risk: medium

## Scope

Build the Health Data Hub v1 feature-engineering layer for the Sleep + Mood Retrospective Explainer. S04 consumes the completed S01 warehouse foundation, S02 mood endpoint/data contract, and the active S03 provider decision. It creates deterministic feature rows only; it does not train a model, build UI, generate counterfactuals, write provider-ingestion decisions, or add hosted infrastructure.

The active S03 provider decision is Oura-only v1. Feature engineering must read that decision, treat Oura as the first-class sleep source, and must not require pyEight evidence. 8 Sleep must remain absent/fallback unless a future explicit slice supersedes S03.

## Constraints

- Manual gates are forbidden; no `manual_gate` rows and no `keel-run mark-manual-gate`.
- Use narrow repo-relative write roots only: `src/warehouse/`, `tests/`, and `docs/evidence/` for sanitized command evidence.
- Do not write provider-evidence or ingestion-decision files except as read-only consult references.
- Do not commit raw health data, provider payloads, tokens, DuckDB files, snapshots, quarantine payloads, or secrets.
- Preserve the v1 target: same-day evening `feeling[D]`.
- Sleep features for `feeling[D]` come from sleep ending on morning `D`.
- `prior_day_feeling` is `feeling[D-1]`.
- Model features are exactly `total_sleep_min`, `hrv_z`, `deep_sleep_pct`, and `prior_day_feeling`.
- `hrv_avg_ms` is display metadata only.
- `hrv_z` must be prior-only and persisted.
- No sleep forward-fill for training.
- Mood labels are never imputed.
- 8 Sleep values must not be averaged, blended, or reconciled into v1 features.

## Deliverables

- `src/warehouse/features.py`
- `tests/test_features.py`
- `docs/evidence/s04-feature-engineering-command-evidence.json` if sanitized command evidence is recorded

## Implementation Tasks

### S04 readiness and provider-decision guard

- [ ] Add or update tests proving feature engineering reads the active S03 provider decision and treats Oura-only v1 as the first-class feature source. The tests must fail if pyEight evidence is required or 8 Sleep is treated as active without a future superseding decision.
  Files: `tests/test_features.py`
  Verify: `python scripts/verify_s04_readiness.py --json`; `python -m pytest tests/test_features.py -q`

### Daily feature construction

- [ ] Implement deterministic daily feature construction from warehouse mood and Oura sleep rows. For date `D`, use the sleep night ending on morning `D`, join the same-day mood label `feeling[D]`, and join `prior_day_feeling` from `D-1`. Do not forward-fill sleep or mood labels.
  Files: `src/warehouse/features.py`; `tests/test_features.py`
  Verify: `python -m pytest tests/test_features.py -q`

### Prior-only HRV z-score persistence

- [ ] Implement prior-only `hrv_z` calculation and persistence. Each day may use only earlier eligible days for its HRV baseline; the current day must not contribute to its own z-score. Keep `hrv_avg_ms` as display metadata only, not a model feature.
  Files: `src/warehouse/features.py`; `tests/test_features.py`
  Verify: `python -m pytest tests/test_features.py -q`

### Sleep merge diagnostics under Oura-only v1

- [ ] Implement sleep-source diagnostics that collapse to Oura-only identity under the active S03 fallback. Diagnostics may state that 8 Sleep is absent/fallback, but must not blend or average 8 Sleep values into v1 features.
  Files: `src/warehouse/features.py`; `tests/test_features.py`
  Verify: `python -m pytest tests/test_features.py -q`

### Hygiene and acceptance evidence

- [ ] Record sanitized S04 command evidence if useful for review, then run the S04 acceptance contract. No raw health data, provider payloads, private evidence contents, tokens, or DuckDB files may be committed.
  Files: `docs/evidence/s04-feature-engineering-command-evidence.json`; `tests/test_features.py`
  Verify: `python scripts/verify_s04_readiness.py --json`; `python -m pytest tests/test_features.py -q`; `python scripts/check_no_tracked_data.py`

## Verification Expectations

S04 is ready to launch only when:

- `python scripts/verify_s04_readiness.py --json` returns `status: ok`.
- The active S03 provider decision resolves Oura as active and pyEight as fallback or explicitly included.
- `python -m pytest tests/test_features.py -q` passes after implementation.
- `python scripts/check_no_tracked_data.py` passes.

Do not proceed automatically into S05 after S04 completion.

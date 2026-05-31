# S09 Testing and v1 Evaluation Autoplan

Slice ID: S09
Lane: compiler
Risk: medium

## Scope

Run final v1 verification across the local-first Health Data Hub build. Confirm provider-policy invariants before v1 completion.

## Constraints

- Manual gates are forbidden.
- Final verification must include `python scripts/verify_v1_provider_policy.py --json`.
- The provider-policy report must return `status: ok`.
- The report must show `active_sleep_provider: oura`.
- The report must show `eight_sleep_state: fallback_active`.
- The report must show `eight_sleep_used_in_model_features: false`.
- The report must show `eight_sleep_required_for_v1: false`.
- Do not mark v1 complete unless `python scripts/verify_v1.py --json` also passes.

## Deliverables

- `scripts/verify_v1_provider_policy.py`
- final test and evaluation evidence under `docs/reviews/`

## Implementation Tasks

### Final provider-policy invariant

- [ ] Add and run the final v1 provider-policy verifier.
  Files: `scripts/verify_v1_provider_policy.py`
  Verify: `python scripts/verify_v1_provider_policy.py --json`

### Final v1 evaluation

- [ ] Run full tests and final v1 verification without introducing tracked data or secrets.
  Files: `docs/reviews`
  Verify: `python -m pytest -q`; `python scripts/verify_v1.py --json`; `python scripts/check_no_tracked_data.py`

## Verification Expectations

- `python -m pytest -q` passes.
- `python scripts/verify_v1_provider_policy.py --json` returns `status: ok`.
- `python scripts/check_no_tracked_data.py` passes.
- `python scripts/verify_v1.py --json` passes before v1 completion.

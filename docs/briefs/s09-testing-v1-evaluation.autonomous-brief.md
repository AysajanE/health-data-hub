# S09 Testing and v1 Evaluation Autonomous Brief

Autonomy profile: guarded zero-supervision for S09 only.

Manual gates are forbidden. S09 performs final v1 verification and must not mark v1 complete unless all required invariants pass.

## Final Provider-Policy Invariant

S09 must run `python scripts/verify_v1_provider_policy.py --json` and require:

- `status: ok`
- `active_sleep_provider: oura`
- `eight_sleep_state: fallback_active`
- `eight_sleep_used_in_model_features: false`
- `eight_sleep_required_for_v1: false`

8 Sleep / pyEight remains fallback-only for v1 unless a future explicit provider-reopening slice supersedes S03.

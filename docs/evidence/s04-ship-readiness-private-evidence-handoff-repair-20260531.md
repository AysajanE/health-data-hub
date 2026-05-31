# S04 Ship Readiness Private Evidence Handoff Repair

Timestamp: 2026-05-31T17:41:42-04:00

## Failure

S04 PO run `RUN_20260531T195938Z_c4376275285148e89301757f7cfeb5e1` passed all five plan-orchestrator items, but AutoKeel rejected completion during detached ship-branch validation.

The failing command was `python scripts/verify_s04_readiness.py --json` inside the detached `ship/s04` checkout. The verifier failed because it delegated to `verify_s03_readiness.py`, which requires private Oura smoke evidence under `private/evidence/S03/oura_smoke`. That private evidence is intentionally untracked and absent from detached ship worktrees.

## Root Cause

S04 readiness was using the S03 launch readiness gate as a shipped-slice handoff gate. S03 launch readiness must require real private provider evidence, but S04 does not need private provider payloads after S03 is complete. S04 needs the tracked S03 handoff contract:

- S01, S02, and S03 are complete.
- S03 ship branch and commit resolve.
- S03 provider decisions validate.
- The tracked S03 ingestion evidence summary records Oura as active.
- The tracked S03 8 Sleep addendum keeps 8 Sleep fallback-only for v1.
- The active pyEight decision is exactly `fallback_active`.

## Repair

`scripts/verify_s04_readiness.py` now accepts a completed-S03 tracked handoff only when the sole S03 readiness failure is the expected missing-private-Oura-evidence preflight. All other S03 readiness errors still block S04.

This preserves the safety invariant that S03 itself still requires real private Oura evidence before launch or completion. It only prevents S04 detached ship validation from depending on intentionally untracked private evidence.

## Validation

Main checkout:

- `python -m pytest tests/autonomy/test_verify_scripts.py -q` passed: `28 passed`.
- `python -m pytest tests/test_features.py -q` passed: `5 passed`.

Retargeted S04 run-branch descendant `6d2eb8eaa1e02229cb917c0485840c4ab9602fca`:

- `python scripts/verify_s04_readiness.py --json` passed with `s03_completed_tracked_handoff: true`.
- `python scripts/verify_slice.py S04 --json` passed.
- `python -m pytest tests/autonomy/test_verify_scripts.py tests/test_features.py -q` passed: `39 passed`.

## Run Retarget

The S04 run branch was moved from `ae2eaf21138649184d24481eeb52844aa4127349` to descendant commit `6d2eb8eaa1e02229cb917c0485840c4ab9602fca`.

The merge-base is the old run-branch head, terminal item counts remained `ST130_PASSED: 5`, and no plan-orchestrator item checkpoint was skipped.

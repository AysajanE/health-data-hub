# S02 Invalid Compiler-Lane Launch Stopped

Timestamp: 2026-05-26T13:48:17-04:00

## Issue

S02 is a high-risk `swr_preferred` slice, but the launch path accepted the lane decision `compile_with_keel_compile` and generated `docs/playbooks/s02-mood-api.playbook.md` through the deterministic compiler path. That did not honor the intended SWR planning lane for generating `markdown_playbook_v1`.

The affected run was:

- `RUN_20260526T173005Z_e4b9f767c7024cc9b3741d04055ec544`

The run reached PO item `01` audit work before it was stopped.

## Stop Evidence

Operator actions:

- Sent `C-c` to tmux session `autokeel-s02`.
- Terminated the remaining S02 PO process group with `TERM`.
- Verified no `autokeel-s02` tmux session remained.
- Verified no lingering S02 AutoKeel, PO, Claude, or Codex child process remained.

S02 was not relaunched after this stop.

## Root Cause

The lane policy treated `swr_preferred` as `compile_with_decision`, and the S02 lane decision artifact allowed a high-risk `swr_preferred` slice to downgrade to `compile_with_keel_compile`. AutoKeel then followed the compiler path because the policy and validator allowed it.

The issue was not the PO runner or the generated playbook content. The fundamental issue was that high-risk SWR-preferred lane enforcement permitted a compiler fallback before SWR materialization.

## Fix

- `ops/autonomy/policy.yaml` now maps `swr_preferred` to `use_swr`.
- `scripts/lane_decision_policy.py` now rejects high-risk `swr_preferred` decisions unless `decision` is exactly `use_swr`.
- The S02 lane decision artifact now records `decision: use_swr`.
- AutoKeel now routes SWR-required slices through `keel-swr run` and refuses to accept an existing playbook unless a matching `docs/evidence/<slice>-swr-playbook-evidence.json` file proves it came from `keel-swr`.
- AutoKeel now refuses direct PO start for SWR-required slices unless the canonical playbook has matching SWR evidence.
- AutoKeel now records final SWR evidence only after the materialized playbook passes autonomous playbook validation.
- S02 readiness now rejects non-null `active_run` state and rejects a canonical S02 playbook that lacks matching SWR evidence.
- The stale compiler-generated S02 playbook artifacts were moved out of `docs/playbooks/` to `ops/autonomy/failures/archived_playbooks/S02-20260526T133005-0400-invalid-compiler-lane/`.
- Regression tests now cover high-risk compiler downgrade rejection, exact lane-review artifact matching, SWR dry-run routing, stale playbook archiving, SWR evidence hash mismatch regeneration, direct PO-start blocking, readiness active-run rejection, readiness stale-playbook rejection, policy pinning, and validation-before-evidence behavior.

## Current Posture

S02 remains `replan_required`. The next S02 launch must materialize the playbook through the SWR path and must not start PO from the archived compiler-generated playbook.

# S05 SWR Review Repair Root-Cause Closure Evidence

Date: 2026-06-01

## Failure

The open S05 `audit_failure` was caused by SWR supervisor review output that did
not satisfy the fail-closed review-bundle contract. The failed run still had a
completed Stage 1 raw response, but AutoKeel's prior behavior quarantined the
whole run and made it invisible to later resume selection.

## Root Cause

The root cause was not missing SWR resume support. The lower-level SWR runner can
resume a run with `--run-dir` and can rerun a specific stage with `--stage`.
AutoKeel was missing a repair state for review-history failures. Invalid review
history went directly to whole-run quarantine, and the next launch created a new
run from Stage 1.

There was also a manifest-model ambiguity: `review_bundle_path` can represent a
stage's own approved bundle or the prior-stage bundle consumed by a downstream
stage. AutoKeel now validates only bundles whose run/stage identity matches the
stage under review, avoiding false current-stage taint from consumed handoff
bundles.

## Repair Implemented

Commit `71de9b9261451cc8e0dcacefc6aeb78550a1eec4` implements the repair path:

- Adds `swr_review_repair` planning and execution.
- Reuses completed, hash-stable raw stage outputs by rerunning only the SWR
  supervisor review lane.
- Falls back to same-run single-stage rerun with `keel-swr run --run-dir ...
  --stage ...` only when the raw output is not safely reviewable.
- Recovers already-quarantined repairable runs from the stored `swr_run_manifest`
  before any fresh full SWR launch is considered.
- Clears the repair plan once the repaired run is active again, so later ticks
  monitor/resume the run instead of repeating the repair command.
- Adds policy authorization and invariant checks for malformed repair plans.

## Verification

The fix was verified with:

```bash
python -m pytest tests/autonomy/test_autokeel_v1_feedback.py -q
python -m pytest tests/autonomy/test_s05_autonomous_launch.py -q
python -m pytest tests/autonomy -q
python -m pytest -q
git diff --check
python scripts/check_no_tracked_data.py --json
```

Observed results:

- `tests/autonomy/test_autokeel_v1_feedback.py`: 52 passed
- `tests/autonomy/test_s05_autonomous_launch.py`: 7 passed
- `tests/autonomy`: 196 passed
- Full test suite: 238 passed
- `git diff --check`: passed
- `check_no_tracked_data`: status ok

## Relaunch Posture

After this closure, S05 should be relaunched only as a bounded guarded tick. The
expected first behavior is planning or executing `swr_review_repair` against the
stored S05 SWR manifest, not creating a fresh Stage 1 run.

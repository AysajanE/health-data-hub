# S05 Stale-Bundle Review-Cycle Reuse Repair

Date: 2026-06-11
Slice: S05
Closes: the 2026-06-11T07:53+ `audit_failure` row (operator blocked during the
regenerated stage-3 review at cycle `execution_row_draft_stage_review`).

## What happened

The regenerated stage-3 artifact (autoplan rev 3, corrective findings
resolved — verified: no `.gitignore` row anywhere in the draft) entered a
fresh review, but the lane reused the DEFAULT review-cycle directory, which
still contained the previous cycle's sidecars and its review bundle — a
bundle hashed against the replaced (pre-regeneration) artifact. The operator
correctly refused: "The preexisting Stage 3 review bundle cannot be advanced
because it is bound to stale response and artifact hashes rather than the
current completed Stage 3 output."

This is the stale malformed reviewer-sidecar contamination class on the
DEFAULT lane: the repair lane gained pristine attempt-unique cycle dirs on
2026-06-10, but non-repair reviews still reused their directory
unconditionally. The operator's blocked verdict targets the stale review
STATE, not the regenerated artifact's content; the auto-minted
rerun_single_stage plan (which would wastefully regenerate a good artifact)
is replaced by a rerun_review_lane plan in a rolled, pristine cycle.

## Repair

`run_swr_review_lane` now rolls the default cycle id (`_c2`, `_c3`, ...) to a
pristine directory whenever the default dir already holds a prior cycle's
sidecars, mirroring the repair-lane freshness rule. Suite green (231).

## Budget classification

Control-plane: stale malformed review-state contamination (invalid
review-history family), not a semantic rejection of the artifact.

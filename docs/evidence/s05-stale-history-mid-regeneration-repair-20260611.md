# S05 Invalid Review-History Check Mid-Regeneration

Date: 2026-06-11
Slice: S05
Closes: the 2026-06-11T06:57:03-04:00 `audit_failure` row.

## What happened

While stage 3 was actively regenerating (launched 06:49 from the
cross-stage repair plan), the 06:57 resume tick's internal review-history
check (`swr_review_history_findings`) validated a superseded review bundle
from the stage's PRIOR review cycle against the in-flight stage and failed
the run closed ("execution_row_draft: review bundle failed
decision-record/hash validation"). This was an invalid review-history
control-plane defect, not a reviewer verdict: a regeneration replaces the
artifacts the old bundle hashed, so prior-cycle bundles are stale by design,
and the script verifier (`verify_swr_review_history.py`) already skips both
in-flight stages and consumed handoff bundles.

The auto-minted 06:57 repair plan (`rerun_single_stage` for
`execution_row_draft`) was redundant — that exact regeneration was already
running — and is cleared with this closure.

## Repair

`swr_review_history_findings` now skips stages whose status is
prepared/queued/submitted/in_progress without `review_approved`, and skips
consumed handoff bundles recorded on a stage entry (bundle stage_id
mismatch), mirroring the script verifier's semantics. Regression covered by
the autonomy suite (231 passing); verified live: the same manifest now yields
zero findings mid-regeneration.

## Budget classification

Control-plane: invalid review-history guard firing on superseded artifacts
(the existing "invalid review-history" budget marker class).

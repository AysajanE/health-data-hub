# S05 Stage-1 Semantic Rejection and Stage-Rerun Escalation

Date: 2026-06-10
Slice: S05
Closes: the 2026-06-10T13:58:00-04:00 `audit_failure` row (operator review
failed closed during review cycle
`source_authority_map_stage_review_repair_20260610t1344410400`).

## What happened

The 2026-06-10 bounded review-lane repair tick executed cleanly end-to-end as
designed — session init, classify (`completed_complete_artifact`), operator
invocation — and produced, for the first time in S05's history, a genuine
semantic reviewer verdict instead of a transport artifact:

- status: `succeeded` (the June-1 canonicalization fix works in live use)
- approval_decision: `do_not_approve`
- validation_errors: none
- blocking issue: "The Stage 1 authority map contradicts the embedded current
  S05 autoplan revision 2 by reducing S05 deliverables to provider-policy
  verifier/test/review artifacts and treating model implementation as
  deferrable."

Sanitized sidecar:
`.local/autokeel/swr/review_lane/S05-run_20260601_133046_ae09e1ea-source_authority_map-source_authority_map_stage_review_repair_20260610t1344410400/operator/`.

## Why this is correct behavior, not a defect

The Stage-1 artifact was generated on 2026-06-01 against autoplan revision 1,
which omitted the design's model-lifecycle deliverables. The 2026-06-10 scope
reconciliation (docs/evidence/s05-autoplan-scope-reconciliation-20260610.json)
corrected the autoplan to revision 2 and explicitly anticipated this outcome:
"If any reviewer in the rerun review lane objects to the Stage-1 artifact
against the revised autoplan, that is a legitimate semantic finding and must
flow through the normal fail-closed path." The operator did exactly that,
grounded in the embedded_sources now included in the review job.

## Resolution

A semantic NO on a hash-stable artifact cannot be cured by re-reviewing
identical content. Per the convergence rule added to
`plan_swr_review_repair` (semantic_rejection=True), the repair escalates to
`rerun_single_stage` for `source_authority_map`: the stage is regenerated from
its current primary job inputs (design doc, autoplan revision 2, brief — read
from disk at run time per the manifest's operator_overrides), downstream
stages remain reset, and the regenerated artifact goes through the full
fail-closed review lane.

## Budget classification

This row is a genuine semantic review-lane outcome and correctly consumes the
SWR review-lane budget (now 2 of 3). It is evidence the review lane works,
not evidence of supervisor unreliability.

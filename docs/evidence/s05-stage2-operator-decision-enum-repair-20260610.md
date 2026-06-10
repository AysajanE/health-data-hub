# S05 Stage-2 Operator Decision Enum Transport Repair

Date: 2026-06-10
Slice: S05
Closes: the 2026-06-10T18:02:08-04:00 `swr_review_transport_failure` row.

## What happened

The stage-2 (`repo_grounding`) review-lane rerun under the repaired acceptance
step reached the operator, whose raw stdout was again a FULL APPROVAL
(`status: succeeded`, `approval_decision: approve`) — but transport failed on
two dialect defects in the same payload:

1. Per-recommendation `operator_decision` values were spelled as verb forms
   (`"accept"`, `"reject"`) instead of the schema's past-participle enum
   (`accepted`/`rejected`/...).
2. The payload self-described `review_kind: "operator_acceptance"` for what
   the supervisor invoked as a `stage_output` review, which would have
   imported the wrong validation rules.

AutoKeel's typed transport path correctly recorded
`swr_review_transport_failure` (control-plane scope) and planned a same-run
`rerun_review_lane` repair — no semantic rejection occurred.

## Repairs (keel kernel commit 7fe3572, pushed)

- `_canonical_operator_decision`: verb-form aliases normalize to the
  past-participle enum; unknown spellings still fail schema (fail closed).
- The invoked `review_kind` is now authoritative over agent self-description,
  exactly as `actor_role` already was.
- The operator prompt pins the exact `operator_decision` enum.
- The verbatim failing operator stdout is the cycle-7 regression fixture;
  replayed, it now canonicalizes to a valid succeeded/approve decision.

## Budget classification

Typed control-plane transport row, governed by the fresh stability
checkpoint. The planned rerun_review_lane repair for the hash-stable stage-2
artifact remains the next bounded action.

# S05 Stage-3 Semantic Rejection and Corrective Regeneration

Date: 2026-06-11
Slice: S05
Closes: the 2026-06-11T06:05:25-04:00 `audit_failure` row.

## What happened

The stage-3 (`execution_row_draft`) review-lane rerun completed a full
semantic cycle: operator passed, Claude reviewer APPROVED, and the Codex
reviewer REJECTED with five substantive blocking issues (`status: succeeded`,
`approval_decision: do_not_approve`) — all variants of one real contract
violation: the drafted execution rows list same-row deliverables in
`repo_surfaces`, which the playbook contract defines as inputs that must
exist before the row runs. This is a legitimate semantic finding, not
transport; the fail-closed lane worked as designed.

## Resolution

Per the semantic-rejection convergence rule, the repair escalated to
`rerun_single_stage` for `execution_row_draft` (regenerate from current
inputs; re-reviewing identical content cannot converge). New hardening landed
with this closure: AutoKeel now collects the rejecting reviewers' blocking
issues from the failed cycle's sidecars into a bounded corrective-findings
document inside the run directory and injects it into the stage rerun via
`--reference-context`, so the regeneration sees exactly why its predecessor
was rejected (verified against the real cycle sidecars: all five Codex
findings extracted).

## Budget classification

Genuine semantic review-lane outcome: consumes the SWR review-lane budget
(now 3 of 3 — at cap). Any further semantic rejection in this slice will
require a scoped budget release; that is the fail-closed contract working.

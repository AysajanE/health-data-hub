# S05 Stage-2 Acceptance Crash: Transport Root Cause and Repair

Date: 2026-06-10
Slice: S05
Closes: the 2026-06-10T15:25:44-04:00 `compile_failure` row.

## What happened on the 15:14 resume tick

Stage 2 (`repo_grounding`) generated successfully and entered its review lane.
The lane progressed further than any prior S05 cycle: operator passed, BOTH
independent reviewers returned genuine semantic verdicts (`status: succeeded`,
`approval: approve_with_conditions`, zero blocking issues each), and
consolidation passed (`approve_with_conditions`,
`proceed_to_operator_acceptance`). The supervisor `accept` step then CRASHED
with `AgentOutputError: Review decision cannot approve when missing_artifacts
is non-empty.`

Root cause chain:

1. The read-only reviewers reported five required run artifacts
   (response.final.json, stage_checkpoint.json, run_manifest.json,
   request_payload.json, input_manifest.json) as missing because their
   sandbox could not read gitignored `.local/` paths. All five existed on
   disk (verified by size and content during diagnosis).
2. Consolidation carried the `missing_artifacts` claims forward, and the
   acceptance builder computed `approve` (no blocking issues) while keeping
   the non-empty `missing_artifacts` — an unwritable combination its own
   validator correctly refused, crashing the command with exit 1.
3. AutoKeel's outer handler misread the crash (exit 1) as a generic
   `compile_failure` and flipped S05 to `replan_required`.

## Repairs (all landed before this closure)

- Keel kernel commit 6878b7e: acceptance now verifies each reviewer
  missing-artifact claim against the filesystem — verified-present claims
  clear with a note in the acceptance summary; truly-missing required
  artifacts become blocking issues with a schema-valid `do_not_approve`
  record. Regression-tested for both directions.
- AutoKeel: all five synchronous supervisor commands (classify,
  invoke-operator, invoke-reviewers, consolidate, accept) now route nonzero
  exits through the typed `swr_review_transport_failure` path (control-plane
  scope, planned same-run repair) instead of returning raw results that the
  outer handler misclassifies as generic compile failures.

## Why this row closes as a false generic

No reviewer rejected anything; every semantic verdict in the cycle was an
approval. The crash was kernel transport, now fixed and typed. The stage-2
artifacts remain hash-stable and the active SWR run resumes its review lane
under the repaired acceptance step.

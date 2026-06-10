# S05 June-1 Cycle-5 Review Failure: Transport Root Causes and Closure

Date: 2026-06-10
Slice: S05
Closes: the restored 2026-06-01T18:17:43-04:00 `audit_failure` ledger row
(suppressed by the unrecorded 18:25-18:28 intervention; restored 2026-06-10 via
`scripts/record_intervention.py`).

## What actually happened in cycle 5 (18:01-18:17)

The recorded reason repeated the 8-check "SWR independent review failed
closed" text, but the archived raw sidecars (preserved in
`.local/cleanup_archives/20260601T182533-0400-pre-s05-reset-cleanup/autokeel-swr-runs-and-review-lane.tgz`,
restored to `.local/autokeel/swr/review_lane/...repair_20260601t1757240400/`)
show the cycle died on review TRANSPORT failures, not reviewer rejections:

1. The Codex reviewer wrapper recorded `read_only_violation` because the
   read-only workspace snapshot diff flagged seven gitignored `.DS_Store`
   files — a false positive; the reviewer's only substantive note was that the
   review bundle did not embed the source text of the cited primary/contract
   files.
2. The Claude reviewer was killed by an external SIGTERM (exit 143) with empty
   stdout and was recorded as a generic agent failure.
3. The attempt also reused review cycle id
   `source_authority_map_stage_review_repair_20260601t1757240400`, whose
   directory already held the failed sidecars of the prior attempt.

The stage-1 artifact itself was hash-stable and had been substantively
approved by both reviewers in earlier cycles (raw stdout `approve`/`approved`).

## Root-cause fixes (all landed before this closure)

- Transport-vs-verdict separation: decision sidecars with transport statuses
  (`malformed_output`, `timeout`, `read_only_violation`, `missing_cli`,
  `interrupted`) now record `swr_review_transport_failure` with explicit
  control-plane budget scope (`ops/autonomy/autokeel.py`, commit c6429cb).
- Stale-sidecar replay: marker-matching review dirs containing prior sidecars
  are refused, and repair attempts mint attempt-unique cycle ids (same commit).
- Read-only snapshot false positive (.DS_Store/gitignored churn) and
  missing_cli/interrupted classification: fixed in the keel
  staged-workflow-runner kernel (nested repo).
- The source-text objection: stage review jobs now embed the source text plus
  sha256 of the primary/contract authority files (`embedded_sources`).
- SIGTERM attribution: the kill source was never identified from surviving
  logs; reviewer interruption is now a typed transport class (`interrupted`)
  so any recurrence is precisely classified instead of read as a rejection.

## Budget classification

This row is a review transport / control-plane failure ("transport failure"
marker, reviewer transport defects). It must consume the control-plane repair
budget, not the SWR review-lane budget; the semantic review lane never
produced a verdict in this cycle.

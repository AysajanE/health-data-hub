# S05 SWR reviewer-decision schema adapter repair

- Date: 2026-06-01
- Slice: S05
- Failure class: audit_failure
- Target run: run_20260601_133046_ae09e1ea
- Target review stage: source_authority_map
- Failed review cycle: source_authority_map_stage_review_repair_20260601t1645480400

## Root Cause

The guarded S05 relaunch correctly stayed on the same-run SWR review-lane
repair, but failed closed during independent reviewer validation. The operator
provisional decision validated and approved the Stage 1 source-authority-map
artifact. The independent reviewer sidecars then failed the strict review
decision schema because their semantically approving outputs used alternate
reviewer field shapes:

- `status=completed` instead of `status=succeeded`
- `approval_decision=approved` instead of `approval_decision=approve`
- `reviewed_artifacts` as strings or objects with an extra `status` field
- non-blocking improvement aliases such as `id`, `title`, `detail`, and
  `affected_artifact`
- recommendation severity `info`
- evidence entries using `evidence_summary`

The fail-closed stop was correct; no review bundle was created and the SWR
manifest remained at `source_authority_map`.

## Repair

The Keel supervisor adapter now canonicalizes independent reviewer decision
variants before strict schema validation, matching the existing operator
canonicalization safety model:

- status and approval aliases are mapped to schema enums
- severity aliases such as `info` are mapped to allowed severities
- reviewed artifact strings and status-bearing artifact objects are normalized
  to artifact refs
- missing artifacts, blocking issues, non-blocking improvements,
  recommendations, and evidence entries are normalized to their schema shapes
- true `blocked` or `do_not_approve` decisions are not upgraded to approval

## Verification

Commands run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest automation/tests/test_responses_runner_v2_supervisor.py -q
```

Result:

- Keel supervisor contract suite: 26 passed

The adapter was also checked directly against the exact failed S05 Codex and
Claude reviewer stdout files from the guarded relaunch. Both now normalize and
validate as `status=succeeded`, `approval_decision=approve`.

## Relaunch Safety

This evidence closes only the reviewer schema-boundary failure. It does not
authorize a human gate, fresh SWR launch, product-scope bypass, or PO launch.
The next permitted action remains the existing same-run planned
`rerun_review_lane` repair for `source_authority_map`, subject to S05 readiness
and AutoKeel invariants.

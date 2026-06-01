# S05 SWR review-decision schema adapter repair

- Date: 2026-06-01
- Slice: S05
- Failure class: audit_failure
- Target run: run_20260601_133046_ae09e1ea
- Target review stage: source_authority_map

## Root Cause

The post-release S05 bounded tick failed before review-bundle creation because
the operator produced parseable JSON with valid review intent, but the Keel
supervisor adapter only filled metadata and then submitted the raw agent shape
directly to the strict `review_decision.schema.json` validator.

The strict validator was correct to fail closed. The missing control-plane
piece was a deterministic schema-boundary canonicalizer for common agent output
variants:

- extra top-level fields such as `slice`, `job_id`, and `handoff_path`
- recommendation `id` instead of `recommendation_id`
- `supporting_evidence` strings instead of structured evidence objects
- singular `affected_artifact` instead of `affected_artifacts`
- `changes_applied[].change` instead of `changes_applied[].summary`
- string `validation_evidence` entries
- unsupported-claim `rejected_reason` / `evidence` instead of `reason` / `source`
- verbose continuation text instead of a schema-enum `next_action`

## Repair

Keel supervisor agent normalization now canonicalizes these shape variants before
schema validation while preserving the safety semantics:

- it strips unsupported top-level fields
- it normalizes ids to schema-safe lowercase identifiers
- it converts evidence strings to structured evidence objects
- it converts change summaries and validation evidence to the required shape
- it maps verbose next-action approval text to the appropriate schema enum
- it never upgrades `blocked` or `do_not_approve` decisions to approval
- it still fails closed if required evidence, artifacts, or decision fields are
  absent after canonicalization

AutoKeel failure-budget scoping was also tightened so malformed/schema/history
contamination in SWR review-decision records is charged to the AutoKeel
control-plane repair budget, while valid semantic reviewer rejections continue
to charge the SWR review-lane budget.

## Verification

Commands run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest automation/tests/test_responses_runner_v2_supervisor.py -q
python -m pytest tests/autonomy/test_s05_swr_review_lane_budget_release.py tests/autonomy/test_autokeel.py::AutoKeelTests::test_malformed_swr_review_decisions_use_control_plane_budget_scope -q
python -m pytest tests/autonomy/test_s05_swr_review_lane_budget_release.py -q
```

Results:

- Keel supervisor contract suite: 25 passed
- AutoKeel targeted budget/release tests: 4 passed
- S05 SWR review-lane budget release tests: 3 passed

## Relaunch Safety

This evidence closes only the schema-boundary `audit_failure`. It does not
authorize a human gate, a fresh SWR run, a product-scope bypass, or PO launch.
The next permitted action remains the existing same-run planned
`rerun_review_lane` repair for `source_authority_map`, subject to the S05
relaunch readiness verifier and AutoKeel invariants.

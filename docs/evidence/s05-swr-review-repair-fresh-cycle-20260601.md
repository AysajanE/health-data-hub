# S05 SWR Review Repair Fresh-Cycle Evidence

Date: 2026-06-01

## Failure

After the readiness and budget guard fixes, the bounded S05 tick reached the
planned same-run SWR review repair. AutoKeel did not launch a fresh SWR run. It
authorized `swr_review_repair`, reset only `source_authority_map` in
`run_20260601_133046_ae09e1ea`, and invoked the supervisor review lane.

The review then failed closed with:

```text
SWR operator review failed closed: operator provisional review status must be
succeeded; operator provisional review approval_decision must be one of
['approve', 'approve_with_conditions']; operator provisional review contains
blocking_issues
```

The operator sidecar showed that it blocked because the review directory still
contained stale malformed independent reviewer sidecars from the earlier failed
review cycle.

## Root Cause

`rerun_review_lane` used the same deterministic review-cycle id and review-lane
directory as the failed review attempt. That preserved provenance, but it also
let repair execution re-read tainted reviewer sidecars from the previous failed
cycle. The repair was therefore not a fresh review of the same stage artifact;
it was contaminated by stale review artifacts.

## Fix

`ops/autonomy/autokeel.py` now gives SWR review repairs a fresh repair-specific
review cycle and review-lane output directory:

- Fresh cycle id: `<stage>_stage_review_repair_<repair-created-at>`.
- Fresh output directory includes that cycle id.
- Review-history repair does not reuse existing review bundles.
- Normal non-repair review-lane behavior is unchanged.

This keeps the same SWR run and same stage artifact, while preventing stale
review sidecars or old review bundles from being treated as current repair
evidence.

## Verification

Commands run:

```bash
python -m pytest tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_swr_review_repair_reruns_review_lane_without_fresh_full_workflow -q
python -m pytest tests/autonomy -q
```

Results:

- Targeted stale-review regression: 1 passed.
- Full autonomy suite: 200 passed.

## Safety Assessment

This does not rerun the full SWR workflow. The repair still targets the existing
`run_manifest.json`, does not use `--run-name`, and continues the same run only
with a validated review bundle. The change only makes the review repair cycle
fresh so previous malformed reviewer sidecars cannot be reused.

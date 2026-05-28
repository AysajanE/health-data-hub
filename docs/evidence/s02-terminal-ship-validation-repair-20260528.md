# S02 Terminal Ship Validation Repair

Date: 2026-05-28

## Issue

AutoKeel observed PO terminal state `passed` for `RUN_20260528T012206Z_d1a034d3e30d4b26a26273e07597d115`, but then recorded `review_artifact_invalid`.

This was a wrapper defect, not an S02 implementation defect.

## Root Cause

AutoKeel terminal handling made two incorrect assumptions after PO passed:

1. `ship_slice` always used `orchestrator/run/<run_id>`.
   The repaired S02 run had been retargeted by PO to `run_state.json` field `run_branch_name = orchestrator/run-refresh/RUN_20260528T012206Z_d1a034d3e30d4b26a26273e07597d115/1`.
   The legacy branch still pointed at item 06, so `ship/s02` was created from a stale checkpoint.

2. Review and slice acceptance checks ran in the operator checkout.
   Terminal validation must run against the shipped branch content, because S02 implementation and repaired review artifacts live on the PO run branch, not on the AutoKeel operator branch.

The false failure therefore came from validating stale/operator-checkout files after a successful repaired PO run.

## Fix

AutoKeel now:

- resolves the PO branch from `run_state.json` `run_branch_name`, falling back to `orchestrator/run/<run_id>` only when no refreshed branch is recorded;
- updates `ship/<slice>` with `git branch -f` instead of switching the operator checkout with `git checkout -B`;
- validates review artifacts and `scripts.verify_slice` inside a detached temporary worktree for the shipped branch;
- records the shipped branch commit from `ship/<slice>^{commit}`;
- recovers an already-passed run recorded on the slice without launching a new PO run, after open failures for that run are closed with local evidence.

## Verification

Commands run:

```bash
python -m pytest tests/autonomy/test_autokeel.py::AutoKeelTests::test_ship_slice_uses_run_state_branch_without_switching_checkout \
  tests/autonomy/test_autokeel.py::AutoKeelTests::test_passed_po_validates_shipped_branch_not_operator_checkout \
  tests/autonomy/test_autokeel.py::AutoKeelTests::test_ship_slice_rejects_dirty_product_changes_before_checkout \
  tests/autonomy/test_autokeel.py::AutoKeelTests::test_escalated_po_keeps_active_run_for_supervised_resume -q

python -m pytest tests/autonomy -q
```

Results:

- targeted regression tests: `4 passed`
- full autonomy suite: `126 passed`

## Relaunch Rule

Do not relaunch SWR or replay PO items 01-07 for this issue. Close the false `review_artifact_invalid` with this evidence, then let AutoKeel recover the existing passed PO run and complete terminal validation from the refreshed shipped branch.

# S05 Closed Repair Budget Meta-Row Repair Evidence

Date: 2026-06-01

## Failure

After the S05 readiness-skip fix was committed and pushed, a bounded S05
AutoKeel tick stopped before SWR execution with:

```text
closed repair budget exceeded for S05: 6 > 5
```

No new SWR run was launched and no SWR stage was rerun. The failure occurred in
AutoKeel's pre-SWR failure-budget guard.

## Root Cause

`failure_budget_exceeded` ledger rows are meta-guardrail stops. They are not a
repair of the S05 playbook, model slice, or SWR run. AutoKeel was counting
evidence-closed `failure_budget_exceeded` rows as closed repairs for the same
slice. That creates a recursive budget deadlock: closing a false budget stop
adds one more closed repair and can trigger another budget stop before the
actual bounded SWR repair executes.

## Fix

`ops/autonomy/autokeel.py` now separates ordinary evidence-closed repair rows
from evidence-closed budget meta rows:

- Open `failure_budget_exceeded` rows still block the run.
- Closed rows without retained local closure evidence still count against the
  unresolved failure budget.
- Evidence-closed `failure_budget_exceeded` rows no longer consume
  `max_closed_repairs_total_per_slice` or the same-root closed repair budget.

This preserves the safety invariant that unresolved or unevidenced failures
block progress while preventing the budget guard from recursively blocking its
own correction.

## Verification

Commands run:

```bash
python -m pytest tests/autonomy/test_autokeel.py::AutoKeelTests::test_closed_failure_budget_rows_do_not_consume_closed_repair_budget tests/autonomy/test_autokeel.py::AutoKeelTests::test_repeated_closed_same_root_cause_exceeds_repair_budget tests/autonomy/test_autokeel.py::AutoKeelTests::test_generic_root_cause_ids_are_grouped_by_description_for_closed_repairs -q
python -m pytest tests/autonomy -q
```

Results:

- Targeted budget regression tests: 3 passed.
- Full autonomy suite: 200 passed.

## Safety Assessment

This does not increase the allowed number of real S05 closed repairs. The total
closed repair budget still applies to ordinary evidence-closed S05 failures, and
the same-root budget still blocks repeated repairs of the same underlying issue.
The change only prevents closed budget guardrail rows from recursively consuming
the repair budget.

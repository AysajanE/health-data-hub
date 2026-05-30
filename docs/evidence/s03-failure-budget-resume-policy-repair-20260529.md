# S03 Failure Budget Resume Policy Repair

Date: 2026-05-29

## Context

AutoKeel correctly stopped S03 after the third `audit_failure` row exceeded
`max_same_failure_class_per_slice`. The stop prevented a repeated escalation
from being converted into another generic retry.

The underlying item 06 root cause was then repaired and the active PO run was
retargeted to a descendant branch containing the verifier fix. The latest
`audit_failure` row was closed with local retarget evidence:

- `docs/evidence/S03-run-retarget-20260529T2037-item06-readiness.json`

However, the failure budget guard still counted all historical S03 failures,
including rows already closed with local closure evidence. That made S03
permanently unable to resume even after root-cause repair.

## Repair

AutoKeel now treats a failure row as resolved for retry-budget purposes only
when all of the following are true:

- `open` is false.
- `closure_evidence` is present.
- `closure_note` is present.
- The closure evidence path resolves under the repository root.
- The closure evidence file still exists locally.

Open failures and closed failures without retained local evidence still consume
the failure budget.

AutoKeel also restores an escalated PO run from `slice.run_id` only when:

- There is no current `active_run`.
- The run has no open failure rows.
- The run has an evidence-closed `audit_failure`.
- The current PO digest still reports `terminal_state=escalated`.

The restored run is resumed through the normal supervised PO resume path, which
keeps the bounded repaired-escalation resume policy intact.

## Verification

Targeted regression tests:

```bash
python -m pytest \
  tests/autonomy/test_autokeel.py::AutoKeelTests::test_failure_budget_counts_only_unresolved_failures \
  tests/autonomy/test_autokeel.py::AutoKeelTests::test_failure_budget_blocks_closed_failures_without_local_evidence \
  tests/autonomy/test_autokeel.py::AutoKeelTests::test_repaired_escalated_run_can_be_restored_from_slice_run_id \
  tests/autonomy/test_autokeel.py::AutoKeelTests::test_closed_escalated_audit_failure_allows_one_repaired_resume \
  -q
```

Result: `4 passed`.

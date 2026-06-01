# S05 Typed Budget Same-Root Scope Repair Evidence

## Summary

The bounded S05 repair tick stopped before the planned same-run SWR review repair because the legacy same-root closed-repair budget still applied to typed `swr_review_lane` rows.

The ledger contained exactly three evidence-closed SWR review-lane repairs with the same historical generic root key:

```text
audit_failure:swr supervisor review did not satisfy the fail-closed review-bundle contract.
```

The v7 typed budget policy sets:

```text
max_closed_swr_review_lane_repairs_per_slice: 3
```

The correct typed-scope behavior is therefore to allow three closed SWR review-lane repairs and stop only when the typed SWR review-lane count is greater than three. The repair narrows the legacy same-root budget check to `product_or_playbook` rows so it no longer overrides the new typed SWR review-lane budget.

## Verification

```bash
python -m pytest tests/autonomy/test_s05_swr_control_plane_regression.py -q
```

Result: exit code 0, `3 passed`.

```bash
python -m pytest tests/autonomy/test_autokeel.py::AutoKeelTests::test_closed_failure_budget_rows_do_not_consume_closed_repair_budget tests/autonomy/test_autokeel.py::AutoKeelTests::test_generic_root_cause_ids_are_grouped_by_description_for_closed_repairs -q
```

Result: exit code 0, `2 passed`.

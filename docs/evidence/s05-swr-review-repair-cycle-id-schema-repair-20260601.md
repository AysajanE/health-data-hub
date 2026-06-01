# S05 SWR Review Repair Cycle ID Schema Repair Evidence

## Summary

The bounded S05 same-run review repair reached the intended `rerun_review_lane` path, but the operator review failed closed before reviewer bundle creation because the generated review cycle id contained an uppercase `T`.

The supervisor schema requires review-decision identifiers to match:

```text
^[a-z0-9][a-z0-9._-]{0,127}$
```

The failing cycle id was:

```text
source_authority_map_stage_review_repair_20260601T1150560400
```

The repair normalizes SWR repair cycle timestamps to lowercase schema-safe identifiers before invoking the review lane.

## Verification

```bash
python -m pytest tests/autonomy/test_s05_swr_control_plane_regression.py -q
```

Result: exit code 0, `4 passed`.

```bash
python -m pytest tests/autonomy/test_autokeel.py::AutoKeelTests::test_closed_failure_budget_rows_do_not_consume_closed_repair_budget tests/autonomy/test_autokeel.py::AutoKeelTests::test_generic_root_cause_ids_are_grouped_by_description_for_closed_repairs -q
```

Result: exit code 0, `2 passed`.

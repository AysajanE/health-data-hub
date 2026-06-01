# S05 Failure-Budget Root-Key Repair Evidence

Date: 2026-06-01

## Failure

The guarded S05 relaunch stopped before SWR repair execution with:

```text
root-cause repair budget exceeded for S05: S05-AUDIT-FAILURE=3
```

## Root Cause

The failure-budget checker was grouping closed repairs by `root_cause_id`.
Historical AutoKeel records generate `root_cause_id` from only the slice and
failure class, such as `S05-AUDIT-FAILURE`. That value is a class placeholder,
not a precise diagnosis.

For S05, this over-grouped unrelated audit failures:

- a readiness gate false-positive before launch
- a prior SWR review-bundle/history failure
- the later SWR independent-review failure being repaired now

The result was a false same-root budget block before the new same-run SWR repair
path could run.

## Repair Implemented

AutoKeel now uses a narrower budget key for closed repairs when the stored
`root_cause_id` is the generic generated placeholder. Generic placeholders are
grouped by failure description, while explicit diagnostic root-cause IDs still
group strictly and still block repeated same-root repairs.

## Verification

The targeted regression check passed:

```bash
python -m pytest \
  tests/autonomy/test_autokeel.py::AutoKeelTests::test_generic_root_cause_ids_are_grouped_by_description_for_closed_repairs \
  tests/autonomy/test_autokeel.py::AutoKeelTests::test_repeated_closed_same_root_cause_exceeds_repair_budget \
  -q
```

Observed result:

```text
2 passed
```

This preserves the safety invariant: repeated explicitly diagnosed root causes
still block, but unrelated repairs are not merged just because they share a
generic failure class.

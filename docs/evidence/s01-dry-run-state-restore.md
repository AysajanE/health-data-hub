# S01 Dry-Run State Restore

Status: ok

## Issue

The controlled launch requires this sequence:

```text
python -m ops.autonomy.autokeel --once --dry-run --slice S01
python -m ops.autonomy.autokeel --once --slice S01
```

After the S01 playbook existed, dry-run validation succeeded but then continued
into the PO start path. Because command execution is mocked in dry-run, there was
no real run id, and AutoKeel returned `po_run_id_missing`.

The dry-run also appended tracked state/log rows, which dirtied the checkout
immediately before the real PO start. That conflicts with plan-orchestrator's
clean tracked checkout guard.

## Root Cause

Dry-run was modeled as "commands do not execute" but state writes still executed.
That was sufficient before a playbook existed, but not once dry-run reached the
validated-playbook stage.

## Fix

- Dry-run now snapshots tracked AutoKeel state files before `run_once()` and
  restores them afterward.
- Dry-run now stops after playbook validation and records the skipped PO start
  only in the temporary dry-run state that is restored before exit.
- The real run can therefore start from the same clean tracked checkout that
  existed before the dry-run command.

## Verification

- `tests/autonomy/test_autokeel.py` covers dry-run state restoration and PO-start
  skipping when a playbook already exists.

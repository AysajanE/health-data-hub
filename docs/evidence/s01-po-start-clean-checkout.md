# S01 PO Start Failure: Dirty Tracked Checkout

Status: ok

## Failure

After S01 playbook validation passed, AutoKeel invoked:

```text
keel-run supervise run --playbook docs/playbooks/s01-warehouse.playbook.md --next
```

The plan-orchestrator supervisor created a run directory but the kernel exited
before creating `run_state.json`.

The kernel stderr was:

```text
ERROR: Tracked main checkout must be clean before starting the orchestrator.
M .gitignore
```

## Root Cause

Plan-orchestrator intentionally refuses to start from a dirty tracked main
checkout. The AutoKeel wrapper fixes, policy updates, tests, and event/failure
ledger updates were still uncommitted, so the PO kernel stopped before run-state
creation.

AutoKeel also had a state-handling bug: it extracted `run_state.json` from the
error path as if it were a run id and persisted that bogus value as
`active_run`.

## Fix

- AutoKeel now only persists `active_run` after a successful PO start.
- Run-id extraction now accepts real `RUN_...` ids and ignores bare
  `run_state.json` path fragments.
- The bogus `active_run` state from the failed launch was cleared.
- The tracked AutoKeel launch-hardening changes must be committed before the
  next PO attempt so the orchestrator's clean-checkout guard remains intact.

## Verification

- `tests/autonomy/test_autokeel.py` checks that failed PO starts do not persist
  active runs.
- `tests/autonomy/test_autokeel.py` checks that run-id extraction does not
  return `run_state.json`.

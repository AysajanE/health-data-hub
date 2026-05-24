# S01 Pre-PO AutoKeel State Checkpoint Fix

## Symptom

After AutoKeel switched PO execution to the product-root runner, PO resolved
`health-data-hub` correctly. The next real S01 run still failed before
`run_state.json` was created:

```text
ERROR: Tracked main checkout must be clean before starting the orchestrator.
M ops/autonomy/autonomy_state.json
 M ops/autonomy/events.jsonl
```

## Root Cause

AutoKeel records a heartbeat, `verify_v1`, tripwire, and playbook-validation
events before starting PO. Those files are tracked in this repository, so the
wrapper itself dirtied the checkout immediately before handing control to PO.
The plan-orchestrator clean-check was correct to stop.

This is distinct from the earlier product-root bug. PO was now checking the
right repository; AutoKeel had to preserve its own audit trail without
violating the PO clean-check contract.

## Fix

AutoKeel now performs a narrow pre-PO checkpoint immediately before starting or
resuming PO. It commits only approved AutoKeel/generated planning paths:

- `ops/autonomy/`
- `docs/briefs/`
- `docs/evidence/`
- `docs/gstack/`
- `docs/playbooks/`

If any product code or unexpected path is dirty, AutoKeel refuses to start PO
and reports those paths instead of hiding or staging them. This preserves the
audit trail and gives PO a clean product checkout.

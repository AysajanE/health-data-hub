# S01 Compile Failure: Plan-Orchestrator Root

Status: ok

## Failure

The controlled S01 run reached Keel's PO contract verification stage and failed
before writing the playbook:

```text
error: PO contract verification failed.
  reason: plan-orchestrator runner not found: /Users/aeziz-local/keel/plan-orchestrator/automation/run_plan_orchestrator.py
```

## Root Cause

AutoKeel passed `--plan-orchestrator-root` using the legacy default
`/Users/aeziz-local/keel/plan-orchestrator`. The installed Keel wrapper resolves
the canonical PO checkout as `/Users/aeziz-local/keel/tools/plan-orchestrator`
and exports that path as `KEEL_PO_ROOT` before executing `keel-run`.

The compiler failure was therefore caused by AutoKeel drifting from the Keel
wrapper's tool layout, not by a missing PO checkout.

## Fix

AutoKeel now resolves the PO root in this order:

1. `KEEL_PO_ROOT` from the environment.
2. `plan_orchestrator_root` from `ops/autonomy/policy.yaml`.
3. `<keel_root>/tools/plan-orchestrator`.

The policy also records the canonical local path explicitly for operator
visibility:

```yaml
plan_orchestrator_root: /Users/aeziz-local/keel/tools/plan-orchestrator
```

The existing `keel-run` wrapper was left unchanged and remains the execution
kernel for PO runs.

## Verification

- `/Users/aeziz-local/keel/tools/plan-orchestrator/automation/run_plan_orchestrator.py` exists locally.
- `tests/autonomy/test_autokeel.py` includes a regression check that the
  AutoKeel default PO root matches Keel's `tools/plan-orchestrator` layout.

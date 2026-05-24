# S01 PO Runner Product Root Fix

## Symptom

After the launch-hardening commit, the product repo was clean and the controlled
S01 real run reached PO start. The supervisor still exited before creating
`run_state.json`:

```text
ERROR: [Errno 2] No such file or directory: '/Users/aeziz-local/keel/tools/plan-orchestrator/.local/automation/plan_orchestrator/runs/RUN_20260524T183420Z_d480bd1918ea4e6c9c22431a162bc22a/run_state.json'
```

The kernel stderr for that run showed:

```text
ERROR: Tracked main checkout must be clean before starting the orchestrator.
M .gitignore
```

## Root Cause

The dirty `.gitignore` was not in `health-data-hub`. It was in the installed
plan-orchestrator checkout at `/Users/aeziz-local/keel/tools/plan-orchestrator`.

The deeper issue was launch-root drift. AutoKeel invoked `keel-run`, and the
installed `keel-run` wrapper changes directory into the plan-orchestrator tool
checkout before executing the runner. The plan-orchestrator then uses its
resolved repo root for clean-checks, run branches, run state, and worktrees.
So PO was checking and preparing the Keel tool checkout instead of the Health
Data Hub product checkout.

## Fix

AutoKeel now creates a local ignored `automation/` shim in the product repo:

- `automation/run_plan_orchestrator.py`
- `automation/plan_orchestrator`

Both point at the installed Keel plan-orchestrator runtime. AutoKeel launches
PO through this product-local runner path from the product repo cwd. That
preserves the Keel plan-orchestrator as the execution kernel while making the
plan-orchestrator resolve `health-data-hub` as the repo under execution.

`scripts/keel_status_digest.py` now prefers the same product-local runner for
status inspection, falling back to `keel-run` only if no local runner exists.

No files in `/Users/aeziz-local/keel/tools/plan-orchestrator` were reverted,
committed, or modified to work around the failure.

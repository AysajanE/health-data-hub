# S01 Compile Failure Closure Evidence

Date: 2026-05-24

## Issue

The controlled S01-only run generated `docs/gstack/s01-warehouse-autoplan.md`, then failed during `keel-compile` before PO execution.

Compiler stderr:

```text
error: --design must be promoted under one of ('docs/gstack/',); got aeziz-local-AysajanE-health-data-hub-design-20260515-114138.md
```

## Root Cause

`ops/autonomy/policy.yaml` configured the compiler design input as `docs/gstack/health-data-hub-office-hours.md`, but that promoted design artifact did not exist. AutoKeel's fallback logic selected the tracked root-level office-hours design file instead. Keel correctly rejected that path because compiler inputs must be promoted under `docs/gstack/`.

## Fix

The tracked office-hours design was promoted to:

```text
docs/gstack/health-data-hub-office-hours.md
```

This preserves the existing policy path and satisfies Keel's promoted-input contract without loosening compiler validation or bypassing AutoKeel.

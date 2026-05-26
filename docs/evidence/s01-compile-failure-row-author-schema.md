# S01 Compile Failure Closure Evidence: Row Author Schema

Date: 2026-05-24

## Issue

After the S01 autoplan became compiler-parseable, Keel reached the row-author stage but failed before emitting a playbook.

Compiler stderr showed that the model-backed row author returned `po_candidate_rows_v1` JSON with string values where arrays were required, including `rows.0.deliverable` and `rows.0.notes`.

## Root Cause

The controlled launch depended on a general `claude -p` subprocess to emit strict schema-perfect row JSON. Keel correctly rejected the near-valid output. Because this happened after previous compile failures, AutoKeel also moved S01 to `blocked` after the configured retry cap.

## Fix

The compiler remains the execution-plan generator and validator, but the row-author command is now a deterministic local adapter:

```text
python scripts/autokeel_row_author.py
```

The adapter reads Keel's validated `row_author_context_v1` prompt input and emits strict `po_candidate_rows_v1` JSON. AutoKeel also records an explicit `--allow-warnings` reason for autonomous manual-gate substitution, because the profile forbids manual gates and relies on deterministic verification plus autonomous review artifacts instead.

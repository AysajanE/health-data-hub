# S01 Compile Failure Closure Evidence: Autoplan Structure

Date: 2026-05-24

## Issue

After the design document was promoted under `docs/gstack/`, the real S01-only run reached Keel's compiler IR stage and failed before PO execution.

Compiler stderr:

```text
error: Step 2 cannot author executable rows because the IR contains no implementation_tasks.
```

## Root Cause

The generated `docs/gstack/s01-warehouse-autoplan.md` contained detailed build-step tables, but it did not use the compiler-parseable `/autoplan` shape. Keel's parser extracts implementation work from a `## Implementation Tasks` section with concrete `Files:` and `Verify:` fields, or from an equivalent markdown task table. The generated artifact also included assistant wrapper text, which AutoKeel's previous validation did not reject.

## Fix

AutoKeel autoplan validation now rejects assistant wrapper/refusal text and requires:

- `## Implementation Tasks`
- compiler-parseable `Files:` fields
- compiler-parseable `Verify:` fields

The S01 autoplan was replaced with a concise compiler-parseable artifact using that structure. This fixes the input contract while preserving Keel's fail-closed behavior.

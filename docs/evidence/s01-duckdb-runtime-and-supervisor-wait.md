# S01 DuckDB Runtime And Supervisor Wait Closure

Date: 2026-05-24

## Issue

The controlled S01 run reached PO item 01 and produced the schema checkpoint, but audit correctly blocked the row because verification never executed `src/db/schema.sql` in DuckDB. The fix worker could not close the finding because the runner Python environment had no `duckdb` module or CLI.

The same run then parked at `ST120_BLOCKED_EXTERNAL`, but AutoKeel continued waiting on `supervise run` because no bounded supervisor wait was configured.

## Root Cause

- S01 depends on a real DuckDB runtime, but the repository did not declare `duckdb` and preflight did not check for it.
- The S01 autoplan did not explicitly require a test that executes the schema against in-memory DuckDB.
- AutoKeel called `supervise run` without `--max-wait-seconds`, so parked waiting states did not return promptly to the wrapper.

## Fix

- Declared `duckdb` in top-level `requirements.txt`.
- Added a preflight check for the required Python `duckdb` module.
- Updated the S01 autoplan so row 01 requires an in-memory DuckDB schema execution test.
- Added `loop.po_supervisor_wait_seconds` and pass `--max-wait-seconds` to supervised PO run/resume calls.
- Updated close-failure handling so a blocked slice with all failures closed is requeued to `replan_required` with retry count reset.
- Updated dry-run playbook handling so `replan_required` does not archive the tracked playbook during a dry-run.

## Evidence

- PO run: `RUN_20260524T190200Z_3560c8330dd14e82b157720c3be82a2e`
- PO terminal state: `ST120_BLOCKED_EXTERNAL`
- Blocking audit finding: item 01 verification was text-only and did not execute the DuckDB schema.
- Fix report: `ModuleNotFoundError: No module named 'duckdb'`

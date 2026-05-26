# S01 SQL Ignore and PO Timeout Fix

## Symptom

PO started successfully and item 01 reached execution, verification, and audit.
The first attempt escalated even though the execution report said
`src/db/schema.sql` was created and tests passed. Codex audit found that the
candidate patch only contained `tests/warehouse/test_schema.py`; the primary
schema deliverable was missing from the checkpoint patch.

The same supervised S01 run then exceeded AutoKeel's 600 second command timeout
while PO was still legitimately auditing/remediating item 01. The timeout left
the PO resume process running outside AutoKeel's parent process.

## Root Cause

The schema file was ignored by the user's global gitignore:

```text
/Users/aeziz-local/.gitignore_global:26:*.sql src/db/schema.sql
```

Codex created `src/db/schema.sql`, but PO's checkpoint commit did not include
the ignored SQL file. The audit escalation was therefore correct: the
checkpoint did not prove the schema deliverable.

Separately, AutoKeel used the generic 600 second command timeout for supervised
PO execution. A real PO item can exceed that while running execution,
verification, and two audits.

## Fix

The repository `.gitignore` now explicitly unignores `src/db/*.sql`, so the
S01 schema is treated as source and can be checkpointed by PO.

AutoKeel now passes a dedicated PO timeout (`po_timeout_seconds`, default
7200 seconds) for supervised PO run/resume commands. The command runner also
starts subprocesses in their own process group and terminates that group on
timeout, so timed-out PO commands cannot leave orphaned Codex/Claude children.

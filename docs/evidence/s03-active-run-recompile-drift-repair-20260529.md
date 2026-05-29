# S03 Active Run Recompile Drift Repair

Date: 2026-05-29

## Incident

During the controlled S03 AutoKeel run, active PO run
`RUN_20260529T212731Z_4400a6b66bc7499f8bb577260bc05864` had item 01 in
`passed` state and items 02-06 not started. The next bounded AutoKeel tick
recompiled `docs/playbooks/s03-ingestion-provider.playbook.md` before resuming
that active run.

The recompile changed the canonical playbook hash from the active run snapshot:

- active run snapshot: `0ae846fc898cd819f925305db7a9cff30b0eb1b427120def7052a4c682f7c5e9`
- recompiled canonical file: `c2cf66c4a0ebc9e68072a29fc98f103f2d80f7f572b62ec147cd390ca5f61d32`

AutoKeel then correctly rejected the active run as a superseded snapshot and
recorded a `state_divergence` failure, but the root cause was AutoKeel's own
operation ordering. The operator stopped duplicate run
`RUN_20260529T215731Z_723c5b5ba5614337aaa6aff2ae1c7340` while item 01 was still
in `ST30_EXECUTING`; it had no passed item checkpoint.

## Root Cause

`AutoKeel._run_once_impl()` selected S03, then ran lane/evidence/compiler
preparation before calling `start_or_resume_po()`. The active-run resume and
snapshot-mismatch guard lived inside `start_or_resume_po()`, so the canonical
playbook could be rewritten before AutoKeel checked and resumed the already
active PO run.

This ordering created a false divergence: the active run was not stale or unsafe;
the canonical playbook had been mutated before resume.

## Repair

AutoKeel now checks for an active run for the selected slice immediately after
slice brief, failure-budget, and terminal-recovery checks. If an active run
exists, AutoKeel calls `start_or_resume_po()` directly and handles PO status
without running lane, evidence, compiler, or validation steps first.

The S03 canonical playbook bundle was restored to the original active run
snapshot hash so `RUN_20260529T212731Z_4400a6b66bc7499f8bb577260bc05864` can
resume from item 02 without snapshot drift.

## Verification

Regression coverage:

- `tests/autonomy/test_autokeel.py::AutoKeelTests::test_run_once_resumes_active_run_before_recompile`

Expected command set:

```bash
python -m pytest tests/autonomy/test_autokeel.py -q
python scripts/verify_failure_ledger.py --json
python scripts/verify_autokeel_invariants.py --json
python scripts/check_no_tracked_data.py --json
```

Safety invariant:

An active PO run is bound to the playbook snapshot captured at launch. AutoKeel
must resume or terminal-handle that run before any operation that can rewrite the
canonical playbook for the same slice.

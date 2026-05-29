# S03 Item 02 Readiness Sequencing Repair

Date: 2026-05-29

## Incident

S03 run `RUN_20260529T212731Z_4400a6b66bc7499f8bb577260bc05864`
escalated on item 02. The item implementation itself added the intended Oura
blocked-external behavior and regression coverage, but the frozen item 02
verification command was `python scripts/verify_s03_readiness.py --json`.

That command is a cumulative S03 readiness gate. It also requires explicit
pyEight provider state, which is owned by item 03 and is not available inside PO
worktrees through `private/evidence/`.

## Root Cause

The readiness verifier only accepted live private pyEight evidence or a fallback
decision under `ops/autonomy/decisions/`. The controlled S03 operator had real
positive pyEight evidence in `private/evidence/S03/pyeight_smoke/`, but that
private evidence is intentionally gitignored and is not copied into isolated PO
worktrees.

As a result, the PO worktree could not prove pyEight readiness even though the
operator had collected real local evidence before S03 launch.

## Repair

The readiness verifier now accepts a sanitized tracked pyEight evidence decision
with:

- provider `pyeight`
- status `ok`
- evidence_status `ok`
- fallback_active `false`
- a relative path to the ignored private evidence file

The tracked decision file is
`ops/autonomy/decisions/S03-pyeight-evidence-20260529T175729-0400.json`.
It contains no secret values, no raw provider payload, and no health data.

This keeps the safety invariant intact:

- private provider evidence remains untracked;
- the PO worktree receives a durable, reviewable proof that the operator
  collected positive pyEight evidence;
- fallback is not falsely asserted when positive pyEight evidence exists.

## Verification

Commands:

```bash
python -m pytest tests/autonomy/test_verify_scripts.py::VerifyScriptsTests::test_verify_s03_readiness_accepts_sanitized_pyeight_evidence_decision -q
python -m pytest tests/autonomy/test_verify_scripts.py -q
python scripts/verify_s03_readiness.py --json
python scripts/check_no_tracked_data.py --json
```

Expected result: all commands pass from the operator checkout. A repaired
descendant of the active PO run branch must carry the same verifier and decision
so item 02 can rerun without private evidence in the worktree.

# S03 Item 06 Readiness Verifier Repair

Date: 2026-05-29

## Root Cause

S03 item 06 escalated because `scripts/verify_s03_readiness.py` treated a present `OURA_ACCESS_TOKEN` as sufficient to pass the Oura readiness branch when no Oura evidence report was visible in the PO item worktree.

That was unsafe. A token only proves that a future collection may be possible. The S03 readiness contract requires either real Oura evidence or an explicit open `blocked_external_missing_evidence` ledger row.

## Repair

- Tightened `scripts/verify_s03_readiness.py` so Oura readiness passes only when the latest Oura smoke report has `status: ok`, or when an open S03 `blocked_external_missing_evidence` failure row exists.
- Added `oura_evidence_status` and `oura_blocked_external_open` to the verifier checks so future audits can see which branch satisfied the gate.
- Allowed the verifier, when run from a Plan Orchestrator item worktree under `.local/automation/plan_orchestrator/worktrees/`, to read the primary repo's gitignored `private/evidence/S03/...` files without copying private evidence into the PO worktree.
- Tightened pyEight evidence status acceptance to `ok` or `fallback_accepted`; other provider states require a tracked decision artifact.
- Added regression coverage for token-only false positives, open blocked-external substitution, and PO-worktree private-evidence resolution.

## Verification

- `python -m py_compile scripts/verify_s03_readiness.py` -> ok
- `python -m pytest tests/autonomy/test_verify_scripts.py -q` -> 23 passed
- `python scripts/verify_s03_readiness.py --json` -> status ok, with `oura_evidence_status: ok`
- `python scripts/verify_s03_readiness.py --root .local/automation/plan_orchestrator/worktrees/RUN_20260529T212731Z_4400a6b66bc7499f8bb577260bc05864/item-06-attempt-1 --json` -> status ok, with the primary private Oura evidence path resolved
- `python scripts/check_no_tracked_data.py --json` -> status ok
- `python -m pytest tests/autonomy -q` -> 164 passed

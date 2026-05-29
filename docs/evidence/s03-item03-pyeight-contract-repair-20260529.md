# S03 Item 03 pyEight Contract Repair Evidence

Date: 2026-05-29

## Root Cause

S03 item 03 escalated because the generated playbook contract treated `ops/autonomy/decisions/S03-pyeight-fallback-<timestamp>.json` as an unconditional deliverable. That contradicted the source autoplan: the fallback decision is written only when the week-2 8 Sleep tripwire fires.

The item implementation also made fallback available only through an extra `--accept-fallback` flag, while the required command is `python scripts/evidence/pyeight_smoke.py --json`.

## Repair

- Repaired the active item-03 branch so the default `pyeight_smoke.py --json` path writes a sanitized `fallback_accepted` decision only when 8 Sleep is unavailable, missing credentials, or returns a non-ok provider state.
- Kept the stable provider path unchanged: when pyEight returns `status: ok`, no fallback decision is fabricated.
- Repaired the canonical S03 autoplan/playbook wording and the active run playbook snapshot so the fallback decision file is conditional, while `ops/autonomy/decisions/` remains the durable provider-decision surface.

## Verification

Active item-03 worktree:

- `python -m pytest scripts/evidence/test_pyeight_smoke.py -q` -> 4 passed
- `python -m py_compile scripts/evidence/pyeight_smoke.py` -> ok
- `python scripts/evidence/pyeight_smoke.py --json` with local environment loaded -> status ok
- `git diff --check` -> ok

Canonical playbook:

- `python scripts/validate_playbook_autonomous.py docs/playbooks/s03-ingestion-provider.playbook.md --json` -> ok
- `PLAN_ORCHESTRATOR_CLEAN_ENV_CONFIRMED=1 python automation/run_plan_orchestrator.py list-items --playbook docs/playbooks/s03-ingestion-provider.playbook.md --format json` -> item 03 required artifacts are `scripts/evidence/pyeight_smoke.py` and `ops/autonomy/decisions/`

No fallback decision was fabricated during the stable `status: ok` verification path, and no token values, raw provider payloads, or private evidence contents were copied into tracked files.

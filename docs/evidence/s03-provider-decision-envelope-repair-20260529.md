# S03 Provider Decision Envelope Repair Evidence

Date: 2026-05-29

## Scope

Strict AutoKeel doctor rejected the tracked positive pyEight decision because it lacked the generic decision-artifact envelope field `created_at`.

## Repair

- Added `created_at` to `ops/autonomy/decisions/S03-pyeight-evidence-20260529T175729-0400.json`.
- Updated S03 readiness test fixtures so provider/fallback decision examples are schema-valid.
- Applied the same repair to the active S03 PO run branch before resuming item 03.

## Verification

Main checkout:

- `python -m pytest tests/autonomy/test_verify_scripts.py -q` -> 20 passed
- `python scripts/verify_autonomy_preflight.py --json` -> ok before commit, with only dirty-worktree warning
- `python scripts/check_no_tracked_data.py --json` -> ok
- `git diff --check` -> ok

Active S03 run worktree:

- `python -m pytest tests/autonomy/test_verify_scripts.py -q` -> 20 passed
- `python scripts/verify_autonomy_preflight.py --json` -> ok before commit, with only dirty-worktree warning
- `git diff --check` -> ok

No raw provider data, token values, or private evidence contents were copied into tracked files.

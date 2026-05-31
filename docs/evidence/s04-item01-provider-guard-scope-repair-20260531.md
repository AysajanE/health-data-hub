# S04 item 01 provider guard scope repair

Date: 2026-05-31

Failure being closed: S04 `audit_failure` from `RUN_20260530T191329Z_40064d103fdf412db1150e69ef247613`, item 01.

Root cause:
- S04 item 01 was scoped as tests-only.
- The new 8 Sleep stability review showed that tests exposed a production feature-construction defect.
- `src/warehouse/warehouse.py` averaged, blended, and used 8 Sleep as fallback for v1 sleep features.
- That production surface was outside the old item 01 write roots, so PO correctly escalated instead of writing outside scope.

Repair:
- Added `docs/evidence/S03-8sleep-provider-status-addendum-20260531.md`.
- Patched the S04 brief and autoplan with the exact fallback-only v1 rule.
- Patched `scripts/verify_s04_readiness.py` so S04 requires `pyeight_state == "fallback_active"`.
- Added `src/warehouse/features.py` with `SleepProviderPolicy`, `load_sleep_provider_policy()`, and `eligible_sleep_rows_for_v1()`.
- Patched `src/warehouse/warehouse.py` so 8 Sleep rows are ignored for v1 model features and never averaged, blended, counted, or used as fallback HRV or stages.
- Added S04 regression tests proving Oura-only fallback behavior, no blending, 8 Sleep-only feature exclusion, no pyEight evidence requirement, and conflicting provider decision failure.
- Recompiled `docs/playbooks/s04-feature-engineering.playbook.md` from the patched S04 autoplan.

Recompiled item 01 scope:
- Deliverables: `src/warehouse/features.py`, `src/warehouse/warehouse.py`, `tests/test_features.py`.
- Allowed write roots: `src/warehouse`, `tests/test_features.py`.

Verification run:
- `python -m pytest tests/test_features.py -q`: pass, 5 passed.
- `python -m pytest tests/warehouse/test_schema.py -q`: pass, 9 passed.
- `python -m pytest tests/autonomy/test_verify_scripts.py -q -k s04`: pass, 1 passed and 26 deselected.
- `python -m pytest tests/model -q`: pass, 1 passed.
- `python scripts/validate_provider_decisions.py S03 --json`: pass.
- `python scripts/verify_v1_provider_policy.py --json`: pass.
- `python scripts/check_no_tracked_data.py --json`: pass.
- `python scripts/verify_s04_readiness.py --json`: policy checks pass and the only remaining blocker is the open S04 `audit_failure` being closed by this evidence.
- `/Users/aeziz-local/keel/bin/keel-compile compile ...`: pass; PO contract verification pass.
- `python /Users/aeziz-local/keel/tools/plan-orchestrator/automation/run_plan_orchestrator.py list-items --playbook docs/playbooks/s04-feature-engineering.playbook.md --format json`: item 01 shows the repaired deliverables and allowed write roots above.

Relaunch posture:
- The prior S04 PO run should not be resumed because its item 01 contract was tests-only.
- S04 is prepared for a fresh guarded zero-supervision relaunch from the recompiled canonical playbook after the open audit failure is closed.

# S05 SWR Review Repair Readiness Skip Evidence

Date: 2026-06-01

## Failure

The bounded S05 relaunch reached a stored `swr_review_repair` plan, but the next
AutoKeel tick ran the S05 pre-launch readiness verifier before `ensure_playbook`
could execute the same-run SWR review repair.

That readiness verifier is correct for fresh S05 launch, but not for a planned
same-run repair. The repair state is intentionally `blocked_compile_inputs`
because the original SWR run manifest is quarantined pending bounded review-lane
repair. Requiring fresh-launch readiness in that state caused a false
`audit_failure` and changed S05 to `blocked`, leaving a valid repair plan in an
invalid status.

## Root Cause

AutoKeel skipped pre-launch readiness for active SWR manifests, but did not apply
the same ordering invariant to planned `swr_review_repair` entries. This let a
fresh-launch gate run ahead of the repair executor.

`scripts/close_failure.py` also requeued all closed blocked slices to
`replan_required`, which is correct for ordinary failures but wrong for a stored
SWR review repair plan. Closing this false readiness failure would have pushed
the slice away from the only valid repair status.

## Fix

- `ops/autonomy/autokeel.py` now skips pre-launch slice readiness when the slice
  has a planned `swr_review_repair`, logs
  `slice_readiness_skipped_swr_review_repair`, and lets `ensure_playbook`
  execute the bounded same-run repair.
- `scripts/close_failure.py` now preserves `blocked_compile_inputs` when closing
  a blocked slice that still has `swr_review_repair`, instead of converting the
  slice to `replan_required`.
- Test fixtures now remove live SWR repair fields when copying runtime state so
  unrelated autonomy tests remain independent of the current S05 run state.

## Verification

Commands run:

```bash
python -m pytest tests/autonomy/test_s05_autonomous_launch.py::S05AutonomousLaunchTests::test_planned_s05_swr_review_repair_skips_prelaunch_readiness_gate tests/autonomy/test_autokeel_ops_tools.py::AutoKeelOpsToolTests::test_close_failure_preserves_swr_review_repair_state -q
python -m pytest tests/autonomy -q
```

Results:

- Targeted regression tests: 2 passed.
- Full autonomy suite: 199 passed.

## Safety Assessment

This does not weaken S05 readiness for a fresh launch. The skip applies only when
a concrete `swr_review_repair` plan already exists on the slice. That plan still
must satisfy the SWR review-history authorization policy, point to an existing
run manifest, and execute through the same-run repair path before AutoKeel can
continue.

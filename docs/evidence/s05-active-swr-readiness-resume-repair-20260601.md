# S05 Active SWR Readiness Resume Repair

Timestamp: 2026-06-01T07:39:43-04:00

## Incident

S05 launched in the required `swr_preferred` lane and recorded an active SWR
run:

- SWR run: `run_20260601_113412_81f8b7d6`
- Response: `resp_07ab40b5b7fce60a006a1d6e38f0fc819cb72ccbb3b2c6e92f`
- Manifest: `.local/autokeel/swr/runs/2026-06-01_113412_autokeel-s05-20260601t073412-0400_gstack_design_to_po_playbook/run_manifest.json`
- Stage: `source_authority_map`
- State: `in_progress`

The first guarded resume tick incorrectly ran the S05 pre-launch readiness
gate while `active_swr_run` was already set. That gate intentionally requires
`active_swr_run` to be null before launch, so AutoKeel recorded a false
`audit_failure` and blocked the slice instead of resuming the existing SWR run.

## Root Cause

`AutoKeel.run_once()` invoked `run_slice_readiness()` before checking for an
active SWR run. Slice readiness is a pre-launch gate. Once a slice has an
active SWR lease for the same slice, subsequent ticks must resume or inspect
that SWR run and must not reapply launch-only readiness predicates.

## Repair

`ops/autonomy/autokeel.py` now checks for an active SWR manifest for the same
slice before running slice readiness:

- If no active SWR manifest exists, readiness remains fail-closed.
- If an active SWR manifest exists, AutoKeel logs
  `slice_readiness_skipped_active_swr` and proceeds to the lane/playbook path
  so `ensure_playbook()` can resume or inspect the existing SWR run.

This preserves the safety invariant that S05 cannot launch without readiness,
while preventing a running SWR workflow from being blocked by a launch-only
gate during guarded resume.

## Verification

Added regression coverage in `tests/autonomy/test_s05_autonomous_launch.py`:

- `test_active_s05_swr_run_skips_prelaunch_readiness_gate`

The test creates an active S05 SWR run fixture, configures the S05 readiness
gate to fail if called, and verifies that `run_once(requested_slice="S05")`
does not call readiness and does call `ensure_playbook()`.

Command:

```bash
python -m pytest tests/autonomy/test_s05_autonomous_launch.py -q
```

Result:

```text
4 passed
```

## Closure Basis

The recorded `S05-audit_failure-20260601T073943-0400-2bbab79d.md` failure was
caused by the AutoKeel wrapper applying a pre-launch readiness check to an
already-launched SWR run. The failure did not indicate an invalid S05 brief,
provider policy failure, missing credential, PO launch, or SWR lane bypass.

The existing SWR run must be resumed through AutoKeel; no fresh SWR launch and
no PO start should occur until the SWR playbook is materialized and validated.

# AutoKeel

AutoKeel is the autonomous supervisor for building Health Data Hub through the
Keel toolchain. It does not replace Keel or plan-orchestrator. It manages slice
state, compiles missing playbooks when compile inputs exist, rejects fake human
gates, records evidence/failures, and only marks a slice complete after
`scripts/verify_slice.py` passes.

## Common Commands

```bash
python -m ops.autonomy.autokeel --doctor
python -m ops.autonomy.autokeel --doctor --strict
python -m ops.autonomy.autokeel --readiness S02
python -m ops.autonomy.autokeel --once --dry-run
python -m ops.autonomy.autokeel --next-slice
python -m ops.autonomy.autokeel --status --failures
python -m ops.autonomy.autokeel --replay-events
python -m ops.autonomy.autokeel --unblock-evidence S03 private/evidence/S03/request
python -m ops.autonomy.autokeel --close-failure S01 manual_gate_leak --closure-evidence docs/reviews/example.md --closure-note "Reviewed replacement autonomous gate evidence."
```

Missing autoplans are generated through the configured `autoplan.command`.
When a slice enters `replan_required`, AutoKeel archives the existing playbook
before recompiling so the same stale artifact is not reused.

High-risk `swr_preferred` slices require a schema-valid `lane_decision`
artifact whose decision is `use_swr`. Missing decisions are recorded as
`lane_decision_missing`; malformed, failing, or compiler-downgrade decisions
are recorded as `lane_decision_invalid`. AutoKeel must route these slices to
`keel-swr` and must not fall back to `keel-compile` unless the slice lane is
changed by policy. `--readiness S02` runs the pre-launch readiness gate for
S02, including lane-decision validation, review artifact validation, and
tracked-data safety checks. It is not a slice completion gate.

## S02 SWR Pre-Launch Runbook

Before any S02 PO execution, run:

```bash
python -m ops.autonomy.autokeel --readiness S02
python -m ops.autonomy.autokeel --once --dry-run --slice S02
```

The dry-run event log must include `swr_playbook_generation_planned`. It must
not include `playbook_compile_passed` or a `keel-compile compile` command for
S02. A real S02 iteration may start PO only after the SWR-generated playbook
has matching SWR evidence and passes autonomous playbook validation.

For PO execution, AutoKeel creates a local ignored `automation/` shim that
points at the installed Keel plan-orchestrator runtime. This lets the
plan-orchestrator resolve this product checkout as the repo under execution,
while still using the Keel runtime as the execution kernel.

## Safety Rules

- AutoKeel must never call `mark-manual-gate`.
- Human approvals are not simulated; autonomous gate substitution requires
  deterministic verification plus review artifacts.
- External evidence must be real local files under allowed evidence roots.
- Raw health data, secrets, tokens, DuckDB files, and quarantine payloads must
  not be tracked by git or written to general logs.
- PO `passed` is not enough for slice completion. The ship branch and slice
  acceptance verification must pass first.
- Heartbeats are written only to ignored runtime JSON under
  `ops/autonomy/heartbeats/`; they do not mutate tracked autonomy state or
  append heartbeat-only events.

# AutoKeel

AutoKeel is the autonomous supervisor for building Health Data Hub through the
Keel toolchain. It does not replace Keel or plan-orchestrator. It manages slice
state, compiles missing playbooks when compile inputs exist, rejects fake human
gates, records evidence/failures, and only marks a slice complete after
`scripts/verify_slice.py` passes.

## Common Commands

```bash
python -m ops.autonomy.autokeel --doctor
python -m ops.autonomy.autokeel --once --dry-run
python -m ops.autonomy.autokeel --status --failures
python -m ops.autonomy.autokeel --replay-events
python -m ops.autonomy.autokeel --close-failure S01 manual_gate_leak --closure-evidence docs/reviews/example.md --closure-note "Reviewed replacement autonomous gate evidence."
```

## Safety Rules

- AutoKeel must never call `mark-manual-gate`.
- Human approvals are not simulated; autonomous gate substitution requires
  deterministic verification plus review artifacts.
- External evidence must be real local files under allowed evidence roots.
- Raw health data, secrets, tokens, DuckDB files, and quarantine payloads must
  not be tracked by git or written to general logs.
- PO `passed` is not enough for slice completion. The ship branch and slice
  acceptance verification must pass first.

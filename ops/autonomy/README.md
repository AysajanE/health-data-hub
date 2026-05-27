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
When AutoKeel materializes the SWR task pack, it appends this repo's
autonomous validation overlay to the task-pack contract and Stage 3 through
Stage 5 prompts so `required_verification_commands` and
`autonomous_gate_review` are generated before the playbook reaches
`scripts/validate_playbook_autonomous.py`.

`keel-swr` uses background Responses API work and the first stage can remain
queued or in progress for minutes to hours. A short local wait timeout with a
remote `last_status=in_progress` is not a compile failure. AutoKeel records the
run in `autonomy_state.json` as `active_swr_run`, marks S02
`waiting_for_playbook`, and refuses to launch another SWR run while the active
manifest remains non-terminal. The S02 readiness gate also scans local SWR run
manifests so it blocks duplicate starts even before state adoption. Do not poll
the live response at minute cadence; resume or inspect it only on the configured
operator-approved low-cadence interval. The current S02 SWR monitor interval is
300 seconds.

When AutoKeel later observes a non-terminal SWR stage at `waiting_for_review`,
it must use the SWR supervisor lane before continuing the run: classify the
stage, invoke the operator/reviewer/consolidation/acceptance cycle, create the
approved review bundle, and continue the same SWR run with `--review-bundle`.
AutoKeel must not launch the next SWR stage from an unreviewed stage output.
If an approved review bundle already exists for the same run, stage, response
artifacts, and hashes, AutoKeel reuses that bundle on retry instead of running a
duplicate supervisor review cycle.

If the terminal SWR playbook materializes but fails
`scripts/validate_playbook_autonomous.py`, AutoKeel must not convert that into
`replan_required` or start a fresh five-stage SWR workflow. It archives the
rejected playbook, records a `swr_validation_repair` plan on the slice, and
blocks with `blocked_compile_inputs`. The repair plan preserves the source
`run_manifest`, `run_dir`, validator errors, and the smallest rerunnable stage.
For Stage 4 contract drift, AutoKeel resets only Stage 4 and downstream Stage 5
before any authorized repair. For Stage 5-only drift, it resets only Stage 5.
A future repair command must use `keel-swr run --run-dir ... --stage ...` plus
the required approved review bundle; it must not use `--run-name` or
`--output-root`.

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

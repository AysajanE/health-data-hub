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
python -m ops.autonomy.autokeel --doctor --strict-swr S05
python -m ops.autonomy.autokeel --readiness S02
python -m ops.autonomy.autokeel --readiness S03
python -m ops.autonomy.autokeel --readiness S04
python scripts/verify_failure_ledger.py --json
python scripts/verify_autokeel_invariants.py --json
python scripts/verify_ship_invariants.py S02 --json
python scripts/verify_run_retarget_evidence.py docs/evidence/<slice>-run-retarget-<timestamp>.json --json
python scripts/validate_provider_decisions.py S03 --json
python scripts/validate_swr_review_bundle.py .local/autokeel/swr/review_lane/<bundle>.json --json
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

SWR-required slices also require matching `keel-swr` evidence immediately
before PO start and immediately before terminal shipping. The invariant is
rechecked inside `start_or_resume_po()` and `ship_slice()` so a forced slice,
terminal recovery, or manual state mutation cannot silently downgrade to a
compiler playbook.

Before a real SWR launch, AutoKeel checks the configured `swr.required_env`
without logging secret values. For OpenAI-backed SWR this means
`OPENAI_API_KEY` must be present in the AutoKeel process environment. A missing
key records sanitized `blocked_external` evidence and does not fall back to the
compiler route. Use `python -m ops.autonomy.autokeel --doctor --strict-swr S05`
to test that prerequisite before selecting a future SWR slice.

`keel-swr` uses background Responses API work and the first stage can remain
queued or in progress for minutes to hours. A short local wait timeout with a
remote `last_status=in_progress` is not a compile failure. AutoKeel records the
run in `autonomy_state.json` as `active_swr_run`, marks S02
`waiting_for_playbook`, writes a per-slice lease under
`.local/autokeel/swr/leases/<slice>.json`, and refuses to launch another SWR
run while the active lease or manifest remains non-terminal. The readiness gate
also scans local SWR run manifests so it blocks duplicate starts even before
state adoption. Do not poll
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
A future repair command must satisfy
`ops/autonomy/authorization_policy.yaml`, use `keel-swr run --run-dir ...
--stage ...` plus the required approved review bundle, and must not use
`--run-name` or `--output-root`.

Before PO start, AutoKeel runs both repository validation and the real
plan-orchestrator parser:

```bash
python scripts/validate_playbook_autonomous.py docs/playbooks/<slice>.playbook.md --risk <risk> --json
python automation/run_plan_orchestrator.py list-items --playbook docs/playbooks/<slice>.playbook.md --format json
python automation/run_plan_orchestrator.py doctor --playbook docs/playbooks/<slice>.playbook.md --format json
```

Any failure blocks PO before execution.

## S03 Controlled Launch Posture

S03 is controlled-autonomous only. Do not run a full S03-S09 zero-human loop
yet. Before launching S03, run:

```bash
python -m ops.autonomy.autokeel --doctor --strict
python scripts/verify_autonomy_preflight.py --json
python scripts/verify_failure_ledger.py --json
python scripts/verify_autokeel_invariants.py --json
python scripts/verify_s03_readiness.py --json
python -m ops.autonomy.autokeel --once --dry-run --slice S03
```

S03 provider evidence is collected by explicit evidence collectors, not hidden
verification calls. Load local credentials only into the shell process that
runs the collector:

```bash
chmod 600 .env.local
set -a; source .env.local; set +a
python scripts/evidence/oura_smoke.py --json
python scripts/evidence/pyeight_smoke.py --json
python scripts/verify_s03_readiness.py --json
```

For positive 8 Sleep/pyEight evidence, `.env.local` must contain
`PYEIGHT_EMAIL`, `PYEIGHT_PASSWORD`, `PYEIGHT_TIMEZONE`,
`PYEIGHT_CLIENT_ID`, and `PYEIGHT_CLIENT_SECRET`. `PYEIGHT_TIMEZONE` must be
an explicit IANA timezone such as `America/Toronto`; do not use `local`.
The PyPI `pyEight==0.3.2` package still calls the retired 8 Sleep `/login`
flow, so the collector uses the current token + bearer + trends flow directly
and stores any short-lived credential cache only under ignored `data/secrets/`
with mode `0600`. The evidence file under
`private/evidence/S03/pyeight_smoke/` contains only sanitized aggregate
booleans, counts, and a coarse freshness bucket; it must not persist raw
provider payloads, credentials, account email, password, full user IDs, full
device IDs, exact sleep dates, or exact sleep metrics. If 8 Sleep cannot
authenticate or return recent sleep intervals reliably, record that evidence
and use the documented Oura-only v1 fallback instead of weakening the provider
gate.

Only if those pass should a real bounded S03 tick run:

```bash
python -m ops.autonomy.autokeel --once --slice S03
```

S03 posthoc acceptance is the cumulative readiness contract, not a fresh live
provider collection. The S03 slice acceptance must run
`python scripts/verify_s03_readiness.py --json`,
`python scripts/validate_provider_decisions.py S03 --json`, autonomous review
validation, and tracked-data hygiene. The final closure bundle is
`docs/evidence/S03-final-closure-bundle-20260530.json`. It records only command
exit codes and redacted command surfaces; it does not commit private evidence
contents.

Provider decisions under `ops/autonomy/decisions/` must validate against
`autokeel.provider_evidence_decision.v1`. Private evidence references require
tracked hashes, sizes, and file modes so the decision proves the ignored local
evidence artifact without committing raw provider data. pyEight fallback may
not coexist with an active positive include decision unless the fallback
explicitly supersedes the include decision and the include records
`superseded_by`.

Run-branch retargeting is a high-risk recovery operation. Every
`docs/evidence/<slice>-run-retarget-*.json` file must pass
`scripts/verify_run_retarget_evidence.py`, including descendant ancestry,
unchanged terminal counts, `skipped_item_count: 0`, explicit `repaired_files`,
and local closure evidence. AutoKeel validates retarget evidence before ship
and enforces per-slice retarget and root-cause repair budgets.

## S04 Guarded Zero-Supervision Runbook

S04 may run as a single guarded zero-supervision slice only after S03 closure
and S04 readiness pass. It must not continue automatically into S05.

Before launching S04:

```bash
python scripts/verify_s03_readiness.py --json
python scripts/validate_provider_decisions.py S03 --json
python scripts/verify_s04_readiness.py --json
python -m ops.autonomy.autokeel --once --dry-run --slice S04
```

The S04 brief must consume the active S03 provider decision. S04 treats
Oura-only v1 as the first-class sleep source, must not require pyEight
evidence, and must keep 8 Sleep absent/fallback unless a future explicit slice
supersedes S03. Feature engineering may not write provider-evidence or
ingestion-decision files except as read-only consult references.

If a S04 run retarget, provider-decision conflict, high/critical open failure,
or readiness failure appears, stop the zero-supervision launch and return to
controlled-autonomous diagnosis.

For PO execution, AutoKeel creates a local ignored `automation/` shim that
points at the installed Keel plan-orchestrator runtime. This lets the
plan-orchestrator resolve this product checkout as the repo under execution,
while still using the Keel runtime as the execution kernel.

AutoKeel passes `--max-auto-resume-attempts 0` to new supervised PO runs and
normal resumes. Deterministic escalations must park for root-cause diagnosis
rather than silently consuming repeated PO attempts. After the matching
`audit_failure` has been closed with local root-cause evidence, AutoKeel permits
one bounded supervised resume of the escalated item. If that repaired attempt
parks again, the next root cause must be diagnosed before another resume is
allowed.

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

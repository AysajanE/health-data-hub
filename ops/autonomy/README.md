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
python -m ops.autonomy.autokeel --readiness S05
python -m ops.autonomy.autokeel --readiness S06
python -m ops.autonomy.autokeel --readiness S11
python -m ops.autonomy.autokeel --readiness S12
python scripts/verify_failure_ledger.py --json
python scripts/verify_autokeel_invariants.py --json
python scripts/verify_event_log.py --json
python scripts/verify_ship_invariants.py S02 --json
python scripts/verify_slice_integration.py S02 --json
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

When more than one open row has the same slice and failure class, closure is
fail-closed. Pass `--failure-id` for one exact row or `--root-cause-id` for one
unambiguous root cause; one evidence artifact must never close an unrelated
tripwire or repair failure.

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

Completed-slice state is also not enough to prove that a ship result reached
the continuation branch. `scripts/verify_slice_integration.py` classifies a
recorded ship as ancestral, exact-surface-equivalent, patch-equivalent, or
explicitly reconciled. Reconciliation receipts are read only from committed
Git objects, must enumerate every ship-changed path, and must bind the exact
current review and command-evidence blobs. A dirty working tree can prepare a
receipt, but it cannot satisfy this landed-state gate.

Post-completion reviews do not rewrite historical ship metadata. A new review
must validate in the current tree and have a separately recorded
`<slice>-post-completion-review-integration` intervention that names the
unchanged historical run, ship branch, ship commit, exact new review list, and
command evidence. The event binds the ratification artifact's own SHA-256, and
the artifact binds the exact canonical slice entry, reconciliation receipt,
review, and command-evidence SHA-256 values, so later edits cannot silently
broaden an older intervention. Unrelated future slice-status changes do not
invalidate historical review ratification. Ship-time reviews still require
their original detached ship-checkout validation event.

Frozen integration reviews prove what was reviewed at their recorded commit;
they are historical evidence, not a source of current product policy. Current
behavior comes from the live slice row, brief, autoplan, and executable
readiness checks. The August 17, 2026 owner scope decision recorded in
`docs/evidence/s12-personal-tool-scope-decision-20260817t064725-0400.json`
supersedes the former outside-stop statements in the frozen S03 and S05
integration reviews. Those files remain byte-identical only so the historical
receipts stay truthful; their old policy prose cannot block S12 or downstream
work.

The event log retains historical rows exactly. Known legacy duplicate ids are
accepted only when `scripts/verify_event_log.py` matches every raw row to the
hash-bound reconciliation receipt; new duplicate or non-monotonic ids fail.

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
duplicate supervisor review cycle. Bundle approval is valid only when the
operator provisional review, both independent reviewer decisions, consolidation,
and operator acceptance are all schema-valid, `succeeded`, non-blocking records.
Malformed agent output, validation errors, `blocking_issues`, or a
`do_not_approve` / `blocked` decision must stop the SWR lane before bundle
creation or reuse.

If AutoKeel discovers malformed prior review history after a stage response is
already complete, it must not quarantine the whole SWR run and relaunch from
Stage 1. It records a `swr_review_repair` plan, preserves the existing
`run_manifest` and `run_dir`, and repairs the smallest affected boundary. When
the target stage response artifacts are completed and hash-checked, AutoKeel
reruns only the supervisor review lane and resets downstream stages that
consumed the tainted handoff. If the raw response is not safely reviewable, it
reruns only that stage in the same SWR run with `keel-swr run --run-dir ...
--stage ...`. If an older AutoKeel version already quarantined a repairable
review failure, the next tick must first materialize this `swr_review_repair`
from the stored `swr_run_manifest`; it must not start a fresh Stage 1 run.
Full-run quarantine is reserved for irreparable state.

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

As of the 2026-05-31 S03 provider-status addendum, 8 Sleep / pyEight remains
fallback-only for v1. S04-S09 must not require pyEight evidence, and feature
construction must ignore 8 Sleep rows for v1 model features even if those rows
exist in the warehouse.

For any future positive 8 Sleep/pyEight evidence collection, `.env.local` must contain
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

S08 backup, restore, and logging work must treat `PYEIGHT_EMAIL`,
`PYEIGHT_PASSWORD`, `PYEIGHT_CLIENT_ID`, `PYEIGHT_CLIENT_SECRET`,
`EIGHT_SLEEP_TOKEN`, and `EIGHT_SLEEP_PASSWORD` as sensitive names if present.
Their absence must not fail v1.

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

## Tripwire Recovery and S06 Launch

Behavioral tripwires use strict typed JSON evidence, not Markdown review
markers or arbitrary file existence. Mood transport requires exactly seven
aggregate boolean opportunities. Mood compliance remains `not_due` until the
real logger activation date plus 28 days and then requires at least 23 unique,
non-backfill logged days in the 28 completed local days. A missing, ineligible,
or failed baseline gate can only enforce `collecting_state_no_override`; it is
never promoted to a model pass, and AutoKeel cannot manufacture the evidence.

When the two mood tripwires and baseline-display tripwire require the configured
S11 recovery, AutoKeel may route only S11 while they are fired. This is not a
general bypass: all non-automatic fired tripwires must name the same required,
incomplete recovery slice. S11 must build the real mobile form, verify a
non-synthetic LAN persistence flow, and independently prove the collecting-state
runtime guard before fallback evidence can be accepted. Until then, leave S11
blocked on real local evidence and do not launch S06.

Tripwire decisions are evidence-state-specific, not permanent waivers. If the
evidence changes—or mature compliance later fails after S11 is already
complete—AutoKeel records a new uniquely addressable GLOBAL hard-stop failure
and requires an explicit replan. It must not reuse the old recovery decision or
silently route a completed slice.

Before any usage-billed S06 SWR generation, run:

```bash
python -m ops.autonomy.autokeel --readiness S06
python scripts/verify_s06_readiness.py --json
```

The S06 zero-spend gate requires the full dependency closure complete and
durably integrated, all consumed product and control surfaces tracked at
`HEAD`, every primary design/autoplan/brief input byte-identical to `HEAD`, a
matching state digest that includes the active-run state, resolved tripwires, passing global
invariants, no open high/critical S06 or GLOBAL failure, and no active PO/SWR
run. Its lane decision must be freshly materialized after the final recovery
commit and must bind both that `HEAD` and the exact committed input-tree blobs.
The sanctioned lane-decision writer records its event and refreshes the state
digest; an older pre-recovery decision is intentionally invalid.

Every compiler launch also fails closed unless its design, autoplan, and
approved brief are tracked and byte-identical to `HEAD`; the same seal remains
mandatory for the billed S06 SWR launch. The compiler seal applies to all
compiler slices, including S11, rather than only to S06. An invalid
autoplan discovered by `--once --dry-run` is reported as a planned corrective
regeneration without moving the source into `archived_autoplans`, creating a
failure artifact, or leaving a heartbeat or generated input behind.

AutoKeel children receive a deny-by-default environment containing only a
small process/runtime allowlist plus explicit per-command values. Repo-local
`.env.local`/`.env` files are never read during AutoKeel construction and are
never injected into `os.environ`. Status, readiness, doctor, intervention, and
digest operations do not open them. Only the final authorized billed SWR route
may lazily read and receive the exact provider variables named by
`swr.required_env`; diagnostic SWR preflight uses process environment only.
Compiler, PO, review, readiness, S11, and activation subprocesses do not inherit
provider or health secrets.

S05/S06 readiness receives only
`AUTOKEEL_READINESS_OPENAI_API_KEY_PRESENT=1|0`, computed from whether the
parent process explicitly exported a nonblank `OPENAI_API_KEY`. The provider
value itself remains stripped by the child environment and repo env files are
not opened. The marker is presence-only launch metadata, not a credential.

Environment sanitization does not constrain same-user file reads. The current
S11 compiler, PO, reviewer, and generated acceptance routes have no enforceable
OS file-read sandbox, while repo-local `.env.local`, `data/`, `private/`, or
model artifacts may exist. AutoKeel therefore stops S11 with
`blocked_compile_inputs` and `control_error` exit 69 before readiness and
before autoplan/compiler spend, PO start/resume/recovery, review generation,
generated acceptance, ship, or activation. The stop is metadata-only: it may
report which sensitive roots exist but never opens or enumerates them. S11 must
not be retried until the complete generated-tool route has enforceable
deny-by-default file-read isolation and the activation receipt control below is
available.

S11 adds a distinct post-ship `activation_acceptance` phase after review and
hermetic `verify_slice` acceptance. Generated activation code is currently
disabled and fails closed as `control_error`: the trusted outer-process
validator for a regular, non-symlink, `0600`, fresh aggregate evidence file is
not yet implemented, so a generated verifier cannot self-attest completion
with a dummy hash. The future receipt contract requires an exact aggregate
`evidence_path`, independently recomputed SHA-256, and offset-aware
`observed_at`, bound to the slice, run, detached ship commit, verifier hash,
and no extra outside-approval field.

The macOS sandbox profile is a disabled, uncertified draft, not an enabled
execution path. It allows exact executable paths only; file-content reads only
from the detached ship worktree, narrowly selected interpreter/runtime roots,
one exact aggregate evidence file, and exact read-only warehouse files; it has
no network or file-write allowance. Broad `/Library`, `/opt/homebrew`,
`/private/etc`, `/dev`, arbitrary home files, repo-local env files, credential
storage, and unrelated private evidence are not allowed. AutoKeel does not
execute generated activation code until this profile has a positive
generated-verifier runtime probe and a trusted outer validator independently verifies a regular,
non-symlink, `0600`, fresh aggregate receipt and recomputes its SHA-256. Only
after both controls exist may a schema-valid
`blocked_external` result be classified as missing external evidence;
malformed/configuration/execution/sandbox failures remain `control_error` and
generic `{"status":"ok"}` output cannot complete a slice. Activation control
errors remain `blocked_compile_inputs`; they do not make S11 actionable for
another paid compile.

S12 uses the same two-boundary lifecycle for production Oura sync. Its offline
readiness check validates committed technical inputs, dependency order, and
the Oura-only contract without reading credentials, private evidence, or the
network. Deterministic ship acceptance remains hermetic. A later read-only
`activation_acceptance` verifies real aggregate sync evidence against the
canonical runtime root. Missing evidence blocks completion without committing
provider data or fabricating a successful sync.
Every boundary uses the same strict report validator: exact top-level fields,
typed required safety checks and blocker counts, and raw status/exit binding.
An incomplete `{status}` object or a `blocked_external` report with any raw
exit other than 2 is a control error, never external evidence.

Ship creation seals the exact commit returned when `ship/<slice>` is created.
Detached review, acceptance, and activation validation use that immutable
commit rather than the mutable branch name. Completion re-resolves the branch,
then immediately performs an atomic compare-and-swap `update-ref` using the
validated commit as both expected and new value before recording completion.
Movement detected at either check is rejected without overwriting the ref.
The durable completion record remains bound to the immutable commit; because a
mutable ref may still move after the compare-and-swap, later landed-state
verification must continue to validate the recorded commit rather than trust
the branch name alone.

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

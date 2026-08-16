# S11 Mood Logging Recovery Autoplan

Slice ID: S11
Lane: compiler
Risk: high
Revision: 1 (2026-08-16 tripwire deadlock recovery)

Deliverables and verification are frozen below. Manual gates are forbidden;
every high-risk decision must be represented by an `autonomous_gate_review`
artifact plus deterministic verification, never human approval.

## Purpose

Resolve the circular pre-S06 stop created by the expired mood and baseline
tripwires. The
design precommits a tiny Streamlit-on-home-Wi-Fi form when Shortcut transport
fails. S11 owns that fallback, its secure write path, a minimal shared
collecting-state guard, and typed behavioral evidence. AutoKeel may route S11
while those three tripwires are fired; no other slice may bypass them.

Authority: `docs/gstack/health-data-hub-office-hours.md` mood-transport and
mood-compliance tripwires; the S01 warehouse contract; the S02 mood validation,
date, and correction contracts; `scripts/tripwire_evidence.py`.

## Frozen Behavior

### Two-boundary runtime/evidence bridge

S11 completion has two ordered boundaries. First, hermetic ship acceptance
validates the ship code, deterministic tests, reviews, and tracked-data safety
inside the detached ship worktree. It must not access `private/` or the live
warehouse. Only after that passes may AutoKeel run the separately configured
`activation_acceptance` command:

`python scripts/verify_mood_logging_recovery.py --runtime-root-env HEALTH_HUB_RUNTIME_ROOT --json`

`HEALTH_HUB_RUNTIME_ROOT` identifies the canonical runtime root, and the
activation verifier may inspect aggregate/private runtime evidence and the
live warehouse read-only with `HEALTH_HUB_RUNTIME_READ_ONLY=1`. The second
boundary must not write health data or fabricate the mobile interaction. A
missing, malformed, nonzero, or non-`ok` activation result means S11 must not
complete; it becomes `blocked_external` while preserving the verified ship
code rather than entering a compiler replan.

### Form and persistence

- Implement `app/mood_form.py` as a compact, touch-friendly form for feeling
  1-10 with the existing anchors. Energy, notes, and context are optional and
  may be omitted to preserve one-tap sustainability.
- Bind only to an explicitly configured `LAN_BIND_IP`. Require a non-empty
  `MOOD_FORM_TOKEN` from the environment and constant-time session validation.
  Never place secrets in query strings, files, logs, rendered errors, command
  evidence, or evidence reports.
- Resolve `mood_date` through the existing `resolve_mood_date` contract using
  the timezone-aware submission timestamp, `HOME_TIMEZONE`, and the canonical
  04:00 local cutoff. Persist through the canonical warehouse model and
  correction flow with the existing source `manual`; do not add a fourth
  warehouse source or duplicate SQL in the UI. The private activation proof,
  not the health row, carries the aggregate `streamlit_mobile_form` marker.
- Do not call the FastAPI endpoint from the form. The activated tripwire
  fallback writes locally and defers the Shortcut/FastAPI runtime to v1.1, as
  the authoritative design specifies.
- Add a reusable Python `fcntl.flock()` write guard for
  `data/.healthhub.lock`. The form and existing FastAPI persistence path must
  use it. Hold the lock through transaction commit and DuckDB `CHECKPOINT`.
  Timeout or lock failure is a visible error and never a second unguarded write.
- Secure creation is part of this recovery because it precedes the first real
  mood write: private directories use `0700`, and warehouse/model/evidence
  files use `0600`. Extend `scripts/setup_permissions.py` to cover `models/` and
  make creation paths safe even when setup ran before the directories existed.

### Baseline collecting-state evidence

- Add one shared model-display guard that the S11 Streamlit surface uses and
  S07 must reuse. Unless a freshly evaluated runtime baseline gate is `passed`,
  the only permitted model status is the exact text `collecting model-ready
  days`; contributor, interval, estimate, and counterfactual output must be
  absent. No stored gate boolean or AutoKeel assertion may authorize output.
- `scripts/evidence/baseline_gate_report.py` may set
  `collecting_state_enforced=True` only after an independent UI/runtime
  verifier exercises the non-passing baseline states and proves the exact
  collecting state with all model-output fields suppressed. It then calls
  `build_baseline_gate_report`; it never invents a baseline pass.
- Store typed reports only under `private/evidence/S11/baseline_gate/`. AutoKeel
  must not create this evidence. A missing or failed verifier remains
  `fallback_required` and S11 remains incomplete.

### Activation and transport evidence

- `scripts/evidence/mood_transport_report.py` queries only the minimum metadata
  needed to classify the last seven completed local-date opportunities. It
  emits seven booleans through `build_mood_transport_report`; it does not emit
  dates, ratings, energy, notes, tokens, log ids, response bodies, or raw rows.
- For the historical Shortcut path, a completed date counts successful only
  when a canonical row attributable to `ios_shortcut` arrived by 23:59:59 local.
  Absence is a failed post. Backfill and corrections from other sources do not
  retroactively turn a transport failure into a Shortcut success.
- `fallback_verified=True` is permitted only after a real user-selected form
  submission is observed through the mobile LAN surface and verified in both
  `mood_entries` and `mood_current`. The private activation proof records only
  booleans, timestamps/date boundaries, the aggregate logger marker
  `streamlit_mobile_form`, and artifact hashes; it contains no submitted values
  or stable health identifiers. The persisted row retains canonical source
  `manual`.
- Keep the private artifacts type-separated: activation proof lives under
  `private/evidence/S11/activation/`, typed transport reports under
  `private/evidence/S11/mood_transport/`, and typed compliance reports under
  `private/evidence/S11/mood_compliance/`. A non-typed activation record must
  never share a newest-report-wins directory with either typed evaluator.
- Synthetic POSTs or synthetic warehouse entries are forbidden. If real phone
  interaction is unavailable, the collector returns `blocked_external` and S11
  remains incomplete.

### Compliance evidence

- The real activation record is the sole compliance clock. Compute
  `due_date = activation_date + 28 days` and generate a fresh report for the
  evaluator's current local date.
- `scripts/evidence/mood_compliance_report.py` projects only `mood_date` and
  `source`, then calls `build_mood_compliance_report`. Count unique current
  entries only; exclude `source=backfill`, ignore duplicate corrections, and
  exclude the incomplete current-day opportunity.
- Before the due date the only valid result is `not_due`. At maturity, 23 or
  more eligible days is `ok`; fewer is `triggered` with
  `stop_modeling_fix_logging`. A `not_due` report is scheduling evidence, not a
  compliance pass.

## Allowed Write Roots

- `app/mood_form.py`
- `src/warehouse/locking.py`
- `src/model/display_gate.py`
- `src/warehouse/models.py`
- `src/warehouse/warehouse.py`
- `src/api/dependencies.py`
- `scripts/run_mood_form.py`
- `scripts/setup_permissions.py`
- `scripts/evidence/mood_transport_report.py`
- `scripts/evidence/mood_compliance_report.py`
- `scripts/evidence/baseline_gate_report.py`
- `scripts/verify_mood_logging_recovery.py`
- `tests/ui/`
- `tests/warehouse/`
- `tests/autonomy/`
- `tests/test_api_security.py`
- `tests/test_mood_date.py`
- `tests/test_mood_correction.py`
- `tests/test_mood_locking.py`
- `docs/reviews/s11-autonomous-security-privacy-review.md`
- `docs/reviews/s11-autonomous-evidence-integrity-review.md`
- `docs/evidence/s11-mood-logging-recovery-command-evidence.json`

## Implementation Tasks

1. Add the private-permission and shared-lock primitives, wire the existing API
   persister through them, and test timeout, exception release, checkpoint, and
   first-creation modes. Add a contention test that exercises the existing
   FastAPI persistence path while the shared warehouse lock is held; the API
   must fail visibly without an unlocked or duplicate write.
   Files: `src/warehouse/locking.py`, `src/warehouse/models.py`,
   `src/warehouse/warehouse.py`, `src/api/dependencies.py`,
   `scripts/setup_permissions.py`, `tests/warehouse/`,
   `tests/test_api_security.py`, `tests/test_mood_locking.py`
   Verify: `python -m pytest tests/warehouse tests/test_api_security.py tests/test_mood_locking.py -q`
2. Build and test the authenticated mobile form using the canonical mood date,
   validation, correction, and persistence interfaces.
   Files: `app/mood_form.py`, `scripts/run_mood_form.py`, `tests/ui/`,
   `tests/test_mood_date.py`, `tests/test_mood_correction.py`
   Verify: `python -m pytest tests/ui tests/test_mood_date.py tests/test_mood_correction.py -q`
3. Build the two aggregate-only evidence producers and their strict tests,
   including seven-opportunity thresholds, activation timing, 23/28 boundary,
   backfill exclusion, duplicate handling, current-day exclusion, and forbidden
   field scans.
   Files: `scripts/evidence/mood_transport_report.py`,
   `scripts/evidence/mood_compliance_report.py`,
   `tests/autonomy/test_tripwire_typed_evidence.py`
   Verify: `python -m pytest tests/autonomy/test_tripwire_typed_evidence.py -q`
4. Build the shared collecting-state guard and independent UI/runtime verifier,
   including missing, ineligible, and failed baseline cases plus suppression of
   every model-output field. Produce typed baseline fallback evidence only from
   that passing verifier.
   Files: `src/model/display_gate.py`,
   `scripts/evidence/baseline_gate_report.py`, `tests/ui/`,
   `tests/autonomy/`
   Verify: `python -m pytest tests/ui tests/autonomy/test_tripwire_typed_evidence.py -q`
5. Implement `scripts/verify_mood_logging_recovery.py --json`. It passes only
   with a schema-valid private activation proof, exact form persistence, a typed
   transport result of `ok` or `fallback_accepted`, and a current typed
   compliance result of `not_due` or `ok`, plus typed baseline evidence of
   `ok` or independently verified `fallback_accepted`.
   Files: `scripts/verify_mood_logging_recovery.py`, `tests/autonomy/`
   Verify: `python scripts/verify_mood_logging_recovery.py --json`
6. Produce independent autonomous security/privacy and evidence-integrity
   reviews with real command evidence. They must distinguish deterministic
   fixture coverage from the real mobile activation proof.
   Files: `docs/reviews/s11-autonomous-security-privacy-review.md`,
   `docs/reviews/s11-autonomous-evidence-integrity-review.md`,
   `docs/evidence/s11-mood-logging-recovery-command-evidence.json`
   Verify: `python scripts/check_autonomous_review_exists.py S11`

## Acceptance

- `python -m pytest tests/ui tests/warehouse tests/autonomy/test_tripwire_typed_evidence.py tests/test_api_security.py tests/test_mood_date.py tests/test_mood_correction.py tests/test_mood_locking.py -q`
- `python scripts/check_autonomous_review_exists.py S11`
- `python scripts/check_no_tracked_data.py`

After hermetic ship acceptance passes, run the distinct runtime activation
acceptance from the detached ship worktree against the canonical runtime root:

- `python scripts/verify_mood_logging_recovery.py --runtime-root-env HEALTH_HUB_RUNTIME_ROOT --json`

## Stop Conditions

- Any missing real mobile activation/persistence proof: `blocked_external`.
- Any evidence containing a mood value, note, token, identifier, or raw row:
  fail with `secret_leak_risk` or `unsafe_write_root` as appropriate.
- Any attempt to mark compliance passed before maturity, accept a Markdown
  proxy, fabricate collecting-state enforcement, weaken the 23/28 threshold,
  or run S06 while S11 is incomplete: stop.

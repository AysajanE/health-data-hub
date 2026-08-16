# S12 Oura Production Sync Autonomous Brief

Autonomy profile: guarded zero-supervision for S12 only.

S12 is the fail-closed production-ingestion bridge between the completed S03
provider choice and downstream Health Data Hub model use. It does not reopen
the provider decision: Oura remains the only active v1 sleep source and 8 Sleep
remains fallback-only. Manual gates are forbidden; high-risk acceptance uses
an `autonomous_gate_review`, deterministic verification, and sanitized
evidence instead of human signoff.

## External Authority Gate

Under the official Oura API/MCP Agreement effective 2026-06-08, Section
4(a)(iii) has a prior-written-consent exception for its restricted-use rule.
That exception does not override Section 4(d), which independently says the
Oura API shall not be used to develop, train, evaluate, or input data into an
AI Model, or Section 6(g), which states an absolute User Data training
prohibition. The Agreement also constrains storage and retention. Ordinary
prior written consent under Section 4(a)(iii) is insufficient for S12.

Health Data Hub correlates Oura sleep data with mood, trains and evaluates
Ridge models, produces retrospective explanations, keeps local backups, and
retains bounded raw audit material. An OAuth grant, functioning token, or
generic provider permission therefore cannot establish authority.

Before any provider call, OAuth flow, token exchange, production write, or
compiler execution for S12, `python scripts/verify_s12_readiness.py --json`
must return `status: ok`. The following two local assertions are necessary
metadata checks but cannot establish issuer authority:

- a sanitized, git-tracked decision at
  `docs/evidence/s12-oura-provider-authority-decision.json` conforming to
  `ops/autonomy/schemas/provider_authority_decision.schema.json`; and
- a non-empty, regular, non-symlink private source document under
  `private/evidence/S12/authority/`, mode `0600`, whose path, size, and SHA-256
  match the tracked decision.

A self-authored decision plus arbitrary opaque bytes, including a matching
self-authored SHA-256 value, must remain `blocked_external`. The current
verifier has no issuer-authenticated validation mechanism and therefore cannot
return `ok`; a separately reviewed control-plane amendment must implement
validation for the actual authority artifact before S12 may proceed. The
future accepted decision must establish exactly one of these routes:

- a separate written agreement with Oura that explicitly authorizes and
  supersedes both the Section 4(d) AI Model prohibition and the Section 6(g)
  User Data training prohibition for the complete S12 scope; or
- a qualified legal confirmation of a non-API acquisition route that is
  outside the Oura API/MCP Agreement restrictions and does not use the Oura
  API.

Either route must explicitly cover provider data acquisition, local storage,
finite retention, mood correlation, Ridge training, Ridge evaluation,
retrospective explanations, local backups, and raw audit retention. It must
state finite limits for provider payloads, audit material, derived data, and
backups, plus purge-on-revocation behavior. Generic or ordinary prior written
consent, user acknowledgement, an account or membership, OAuth authorization,
an access or refresh token, and an old smoke test are insufficient
individually or together.

The readiness verifier may inspect the private source only as filesystem
metadata: existence, regular-file and non-symlink status, mode, and byte size.
It must not read, hash, parse, quote, log, copy, or otherwise expose its
contents. A local digest is not issuer authentication.
Missing or invalid authority is `blocked_external`, never success and never a
simulated human approval.

## Immutable Sealed Inputs and Lifecycle

`scripts/verify_s12_readiness.py`,
`ops/autonomy/schemas/provider_authority_decision.schema.json`, and
`tests/autonomy/test_verify_s12_readiness.py` are immutable sealed inputs, not
PO write scope. They must be tracked at and byte-identical to HEAD. The exact
readiness command must run in both `pre_po_commands` and `pre_ship_commands`;
generated code cannot rewrite or approve its own authority verifier.

## Conditional Production-Sync Contract

The following behavior is authorized for implementation only after the
external authority gate passes:

- For the separate-Oura-agreement API route only, implement Oura API v2 OAuth
  with private credential storage, refresh-token rotation, atomic replacement,
  expiry handling, revocation handling, and a reauthorization stop. Tokens
  must never enter git, logs, command evidence, URLs, exception text, or
  aggregate attestations. A legally confirmed non-API route must not call the
  Oura API or perform its OAuth flow.
- Fetch only the minimum authorized sleep fields and window. Rate limits,
  authentication failures, revoked authority, schema drift, and incomplete
  pagination must fail closed without partial-success attestation.
- Derive `sleep_date` only from `waketime_utc` converted through
  `HOME_TIMEZONE` using `zoneinfo`. DST transitions, UTC offset changes, and
  cross-midnight sleeps must be tested; provider query dates are not the
  warehouse date authority.
- Hold the shared `data/.healthhub.lock` with `fcntl.flock()` for every live
  warehouse write. The transaction, idempotent upsert, chronological feature
  recomputation, commit, and DuckDB `CHECKPOINT` must complete while the lock
  is held. Timeout or failure must never fall back to an unlocked write.
- Recompute from the earliest changed `sleep_date` forward in chronological
  order so prior-only `hrv_z`, prior-day mood alignment, and same-day evening
  target semantics remain correct. No sleep forward-fill and no mood
  imputation are permitted.
- Create private directories with mode `0700` and provider payload, token,
  warehouse, audit, evidence, and backup files with mode `0600`. Enforce the
  authorized retention limits and purge on expiry, revocation, or withdrawal
  of authority.
- Emit only privacy-minimized aggregate sync attestation: status, bounded
  request window, counts, source `oura`, code/config hashes, checkpoint result,
  recompute boundary, and retention-policy reference. It must contain no raw
  payload, sleep metric, token, stable provider identifier, or mood value.
- Feature construction must continue to ignore every 8 Sleep row. S12 must not
  average, blend, reconcile, or use 8 Sleep as fallback HRV or stage data and
  must not mark it active. Only a future explicit provider-reopening slice may
  change that policy.

## Required Verification

- Authority readiness unit tests cover missing, malformed, untracked, dirty,
  insufficient-scope, wrong-hash, unsafe-mode, symlink, generic-prior-consent
  rejection, missing Section 4(d) or 6(g) supersession, a valid separate Oura
  agreement, and a legally confirmed non-API route without network access.
- OAuth tests use deterministic fakes and cover refresh rotation, atomic token
  replacement, expiry, revocation, redaction, pagination, and rate-limit
  failure. No test may use a live credential.
- Ingestion tests cover idempotency, partial failure rollback, wake-date/DST
  mapping, shared-lock contention, transaction plus `CHECKPOINT`, secure first
  creation, finite retention, and aggregate-attestation forbidden fields.
- Recompute tests cover earliest-changed-date propagation, chronological
  ordering, prior-only `hrv_z`, no sleep forward-fill, no mood imputation, and
  the Oura-only exclusion of 8 Sleep rows.
- Independent authority, security/privacy, and ingestion-integrity
  `autonomous_gate_review` artifacts must distinguish deterministic fixtures
  from real private source authority and real production evidence.

## Two-Boundary Completion

After the authority gate permits compilation, S12 still has two completion
boundaries. Hermetic ship acceptance runs deterministic fake-transport tests,
reviews, provider-policy verification, and tracked-data hygiene in the detached
ship worktree. It does not open private runtime evidence or call Oura. Only
after that passes may AutoKeel invoke the shipped read-only verifier through
the configured activation acceptance:

`python scripts/verify_s12_sync.py --runtime-root-env HEALTH_HUB_RUNTIME_ROOT --json`

`HEALTH_HUB_RUNTIME_ROOT` identifies the canonical runtime root and
`HEALTH_HUB_RUNTIME_READ_ONLY=1` prohibits verifier writes. Missing, malformed,
or non-`ok` aggregate production-sync evidence leaves S12
`blocked_external`; it does not replan verified code or fabricate a live run.
S06 readiness and final `verify_v1.py` must rerun current authority and this
read-only activation/sync proof. Expiry, revocation, a missing source, or a
missing, stale, malformed, or non-`ok` aggregate proof fails closed.

## Stop Conditions

- `verify_s12_readiness.py` is not `ok`: stop before compiler and provider use.
- A separate Oura agreement explicitly superseding Sections 4(d) and 6(g), or
  qualified legal confirmation of a non-API route outside those restrictions,
  is absent, expired, revoked, incomplete, or cannot be represented safely:
  `blocked_external`.
- Ordinary prior written consent under Section 4(a)(iii), including generic
  written provider permission, is offered without the separate agreement:
  stop.
- Any attempt to treat OAuth, a token, user acknowledgement, membership, or
  historical smoke evidence as legal authority: stop.
- Any attempt to treat self-authored JSON, a local digest, or arbitrary opaque
  bytes as issuer-authenticated authority: stop.
- Any raw payload, credential, health metric, stable provider identifier,
  warehouse, audit file, or backup enters git or general logs: stop.
- Any implementation activates 8 Sleep or changes the S03 Oura-only policy:
  stop; S12 is not provider reopening.

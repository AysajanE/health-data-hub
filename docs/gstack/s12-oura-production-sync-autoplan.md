# S12 Oura Production Sync Autoplan

Slice ID: S12
Lane: compiler
Risk: high
Revision: 1 (2026-08-16 authority-first production sync contract)

Deliverables and verification are frozen below. Manual gates are forbidden.
Every high-risk decision requires an `autonomous_gate_review`, deterministic
checks, and sanitized evidence; no artifact may claim human approval.

## Purpose and Authority

S12 adds the production Oura sync that S03 did not build, through only the
authority route accepted by the readiness gate. It does not reopen provider
policy: Oura is the sole active v1 sleep source, while 8 Sleep remains
fallback-only and excluded from feature construction.

The first gate is the official Oura API/MCP Agreement effective 2026-06-08.
Section 4(a)(iii) has a prior-written-consent exception, but that exception
does not override the independent Section 4(d) prohibition on using the Oura
API to develop, train, evaluate, or input data into an AI Model or the absolute
Section 6(g) User Data training prohibition. Its storage and retention terms
also remain controlling. Ordinary prior written consent under Section
4(a)(iii) is insufficient.

The accepted API route requires a separate written agreement with Oura that
explicitly authorizes and supersedes Sections 4(d) and 6(g) for acquisition,
local storage, finite retention, mood correlation, Ridge training and
evaluation, retrospective explanations, local backups, and raw audit
retention. The only alternative is a qualified legal confirmation of a
non-API acquisition route outside the Oura API/MCP Agreement restrictions.
This is the separate Oura agreement explicitly superseding Sections 4(d) and
6(g); no more generic permission can substitute for it.

Before compiler execution, OAuth, token exchange, network access, or provider
data handling, `python scripts/verify_s12_readiness.py --json` must return
`status: ok`. A user acknowledgement, Oura account or membership, OAuth grant,
access or refresh token, and historical smoke success are not authority. A
self-authored tracked decision plus arbitrary opaque local bytes, including a
self-authored SHA-256 claim, is also not authority. The current gate therefore
stays `blocked_external` even when those local assertions match. S12 can be
unblocked only by a separately reviewed amendment that implements an
issuer-authenticated validation mechanism for the actual authority artifact.
The verifier must not parse, hash, or disclose private source contents.
Generic written provider consent must fail closed.

## Immutable Sealed Inputs

The compiler and PO must consume, but must never write, these authority-control
inputs:

- `scripts/verify_s12_readiness.py`
- `ops/autonomy/schemas/provider_authority_decision.schema.json`
- `tests/autonomy/test_verify_s12_readiness.py`

They must be regular, non-symlink files tracked at and byte-identical to HEAD
before any compiler or PO step. The same exact offline readiness command must
be configured in both `pre_po_commands` and `pre_ship_commands`. Any change to
an immutable input requires a new reviewed control-plane commit and a fresh
readiness run; generated ship code may not revise its own authority gate.

## Deliverables

- `src/ingestion/oura_auth.py`: API-route-only OAuth lifecycle and atomic
  refresh-token rotation with redacted failures; it remains unused for an
  accepted non-API route.
- `src/ingestion/oura_sync.py`: minimum-field Oura v2 pull, pagination,
  idempotent mapping, retention enforcement, and aggregate result.
- `src/warehouse/locking.py`: shared `fcntl.flock()` warehouse write guard if
  S11 has not already supplied the identical primitive.
- `scripts/sync_oura.py`: fail-closed production entrypoint that runs S12
  readiness before any provider or token operation.
- `scripts/evidence/oura_sync_attestation.py`: private aggregate-only evidence
  writer.
- `scripts/verify_s12_sync.py`: deterministic production-sync contract gate.
- Focused auth, date-mapping, ingestion, retention, locking, recompute, and
  evidence-integrity tests.
- Authority, security/privacy, and ingestion-integrity
  `autonomous_gate_review` artifacts plus sanitized command evidence.

## Frozen Behavior

### Two-boundary completion

The first boundary is hermetic ship acceptance in the detached ship worktree:
deterministic fake-transport tests, all three reviews, Oura-only provider-policy
verification, and tracked-data hygiene. It performs no live provider or private
runtime access. The second boundary runs only after the first passes and uses
the exact configured activation command:

`python scripts/verify_s12_sync.py --runtime-root-env HEALTH_HUB_RUNTIME_ROOT --json`

Immediately before ship verification, `pre_ship_commands` reruns the exact
offline authority readiness command. The shipped verifier then reads aggregate production evidence from the canonical
runtime root with `HEALTH_HUB_RUNTIME_READ_ONLY=1`. It may not trigger a sync or
mutate the warehouse. Missing, malformed, or non-`ok` evidence keeps S12
`blocked_external` without recompiling or claiming activation.

S06 readiness and final `verify_v1.py` must re-run both current authority and
the read-only aggregate activation/sync proof. Expiry, revocation, missing
source metadata, missing evidence, stale evidence, or a non-`ok` verifier
result fails closed even if S12 previously activated.

### Authority and provider policy

- Validate `docs/evidence/s12-oura-provider-authority-decision.json` against
  `ops/autonomy/schemas/provider_authority_decision.schema.json` and the
  private source metadata before compilation or runtime access.
- For an API route, require the private source to be a separate written Oura
  agreement explicitly superseding the Section 4(d) AI Model prohibition and
  Section 6(g) User Data training prohibition for every covered S12 use.
- For an alternative route, require qualified legal confirmation that the
  acquisition is non-API and outside those restrictions; the route must never
  use Oura API OAuth or endpoints.
- Honor the decision's finite retention limits and purge-on-revocation rule.
  Expired, revoked, missing, dirty, incomplete, or hash-mismatched authority is
  `blocked_external`, never an inferred pass.
- Preserve Oura-only v1. Never activate, blend, reconcile, average, or use 8
  Sleep as fallback HRV, stages, or model input. S12 is not provider reopening.

### OAuth and token rotation for the separately authorized API route

- Use the minimum Oura API v2 OAuth scopes. Store access and refresh tokens
  only in an approved private local credential location, mode `0600` when
  file-backed. Never place tokens in query strings, git, logs, evidence, or
  exception bodies.
- Refresh before expiry; rotate refresh tokens by write-then-`fsync`-then
  atomic replace under the private directory. Keep no plaintext token backup.
- Authentication rejection, missing refresh token, revocation, scope loss, or
  failed atomic rotation stops the sync and requests reauthorization. It must
  not reuse an invalid token or emit partial success.
- For the legally confirmed non-API route, this entire OAuth branch is
  inapplicable and must not be invoked; route-specific acquisition still obeys
  the same mapping, locking, retention, and evidence constraints below.

### Mapping, warehouse write, and recompute

- Pull only the authorized window and minimum fields, exhaust pagination, and
  reject schema drift or incomplete pages before warehouse mutation.
- Derive each `sleep_date` from `waketime_utc` converted to `HOME_TIMEZONE` by
  `zoneinfo`. Provider request dates are not warehouse date authority. Test
  DST folds, DST gaps, offset changes, cross-midnight sleeps, and UTC boundary
  cases.
- Acquire the shared `data/.healthhub.lock` using `fcntl.flock()` before any
  live write. Hold it through idempotent upsert, chronological recompute,
  commit, and DuckDB `CHECKPOINT`. Lock timeout and checkpoint failure roll
  back or stop visibly; no unlocked fallback write exists.
- Recompute from the earliest changed `sleep_date` forward, strictly in
  chronological order. Preserve prior-only persisted `hrv_z`, morning-D sleep
  alignment to evening `feeling[D]`, `prior_day_feeling=feeling[D-1]`, no sleep
  forward-fill, and no mood-label imputation.

### Filesystem, retention, and attestation

- Create private directories with `0700`; create token, provider payload,
  warehouse, raw audit, evidence, and backup files with `0600`.
- Enforce the authority decision's separate maximum retention days for raw
  provider payloads, raw audit material, derived data, and backups. Purge on
  limit, authority revocation, or withdrawal; record aggregate counts only.
- The sync attestation may include status, bounded request window, aggregate
  counts, source `oura`, code/config hashes, earliest recompute date,
  checkpoint result, and retention-policy reference. It must exclude raw
  payloads, sleep or mood values, tokens, response bodies, and stable provider
  identifiers.

## Implementation Tasks

1. Wire every production entrypoint to the immutable external authority gate
   before any credential, network, or provider operation. Reject ordinary
   Section 4(a)(iii) prior consent and reject local self-attestation. The
   entrypoint consumes the sealed verifier; PO may not modify the verifier,
   its schema, or its authority-gate tests.
   Files: `scripts/sync_oura.py`; `tests/ingestion/test_oura_entrypoint.py`
   Verify: `python -m pytest tests/autonomy/test_verify_s12_readiness.py -q`; `python scripts/verify_s12_readiness.py --json`
2. For the separately authorized API route only, implement the private OAuth
   lifecycle, minimum scopes, expiry handling, refresh-token rotation,
   revocation stop, atomic replacement, and complete redaction using
   deterministic fake transports only in tests. For a non-API route, prove
   this module and every Oura API endpoint remain unused.
   Files: `src/ingestion/oura_auth.py`; `tests/ingestion/test_oura_auth.py`
   Verify: `python -m pytest tests/ingestion/test_oura_auth.py -q`
3. Implement the paginated minimum-field Oura v2 fetch and strict wake-date
   mapping through `HOME_TIMEZONE`, including DST and UTC boundary cases.
   Files: `src/ingestion/oura_sync.py`; `tests/ingestion/test_oura_date_mapping.py`; `tests/ingestion/test_oura_sync.py`
   Verify: `python -m pytest tests/ingestion/test_oura_date_mapping.py tests/ingestion/test_oura_sync.py -q`
4. Integrate the shared write lock, idempotent transaction, chronological
   earliest-change recompute, commit, and DuckDB `CHECKPOINT`; prove rollback,
   contention, and Oura-only feature construction.
   Files: `src/warehouse/locking.py`; `src/warehouse/warehouse.py`; `src/warehouse/features.py`; `tests/ingestion/test_oura_sync.py`; `tests/warehouse/test_locking.py`; `tests/warehouse/test_recompute.py`
   Verify: `python -m pytest tests/ingestion/test_oura_sync.py tests/warehouse/test_locking.py tests/warehouse/test_recompute.py tests/test_features.py -q`
5. Enforce secure creation and finite retention, and emit an aggregate-only
   sync attestation with forbidden-field scanning and no partial success.
   Files: `scripts/setup_permissions.py`; `scripts/evidence/oura_sync_attestation.py`; `tests/ingestion/test_oura_retention.py`; `tests/autonomy/test_oura_sync_attestation.py`
   Verify: `python -m pytest tests/ingestion/test_oura_retention.py tests/autonomy/test_oura_sync_attestation.py scripts/test_setup_permissions.py -q`
6. Add the fail-closed production entrypoint and full deterministic verifier.
   The entrypoint runs S12 readiness before touching credentials or network.
   Files: `scripts/sync_oura.py`; `scripts/verify_s12_sync.py`; `tests/ingestion/test_oura_entrypoint.py`
   Verify: `python -m pytest tests/ingestion/test_oura_entrypoint.py -q`; `python scripts/verify_s12_sync.py --json`
7. Produce authority, security/privacy, and ingestion-integrity
   `autonomous_gate_review` artifacts and sanitized command evidence that
   distinguish fake-transport coverage from real provider evidence.
   Files: `docs/reviews/s12-autonomous-authority-review.md`; `docs/reviews/s12-autonomous-security-privacy-review.md`; `docs/reviews/s12-autonomous-ingestion-integrity-review.md`; `docs/evidence/s12-oura-production-sync-command-evidence.json`
   Verify: `python scripts/check_autonomous_review_exists.py S12`; `python scripts/check_no_tracked_data.py`

## Verification Expectations

- `python -m pytest tests/autonomy/test_verify_s12_readiness.py -q` passes
  without network access.
- `python scripts/verify_s12_readiness.py --json` is `blocked_external` under
  the current implementation even when self-authored JSON and opaque bytes
  match. It may become `ok` only after a separately reviewed amendment adds
  issuer-authenticated validation for an actual authority artifact.
- All focused production-sync suites pass using deterministic fake transports.
- `python scripts/verify_s12_sync.py --json` passes only after real,
  privacy-minimized production evidence exists.
- `python scripts/check_autonomous_review_exists.py S12` passes with all three
  required high-risk reviews.
- `python scripts/check_no_tracked_data.py` passes; no provider payload,
  credential, warehouse, audit material, backup, or private authority source is
  tracked.

## Stop Conditions

- Authority readiness is not `ok`: `blocked_external`; do not compile or call
  the provider.
- A self-authored decision, local digest, or arbitrary opaque bytes are offered
  as issuer authority: stop.
- OAuth, token possession, user acknowledgement, account status, membership,
  or smoke history is offered as authority: stop.
- Ordinary prior written consent under Section 4(a)(iii), or generic provider
  permission that does not explicitly supersede Sections 4(d) and 6(g), is
  offered for an API route: stop.
- Authority or retention scope is incomplete, expired, revoked, dirty, or
  inconsistent with the private source metadata: stop.
- Token rotation, pagination, locking, transaction, `CHECKPOINT`, chronological
  recompute, secure-mode, retention, or aggregate-attestation verification
  fails: stop without partial-success evidence.
- Any 8 Sleep activation or provider-policy change appears: stop and require a
  separate explicit provider-reopening slice.

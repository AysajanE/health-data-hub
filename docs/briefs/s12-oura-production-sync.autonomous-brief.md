# S12 Oura Production Sync Autonomous Brief

Autonomy profile: guarded zero-supervision for S12 only.

S12 builds the missing production import for the owner's own Oura sleep data.
This is a private, single-user personal tool. It connects the completed
Oura-only provider choice to the local warehouse. Oura remains the only active
v1 sleep source, and 8 Sleep remains inactive and excluded from model features.

Manual gates are forbidden. High-risk acceptance uses deterministic checks,
real local activation evidence, and independent `autonomous_gate_review`
artifacts instead of human signoff.

## Scope

- Implement Oura API v2 authentication with private credential storage,
  refresh-token rotation, expiry handling, and complete redaction.
- Fetch only the sleep fields and date window needed by v1. Pagination,
  authentication errors, rate limits, and schema drift must stop without a
  partial-success claim.
- Raw provider responses are never written to disk. Process the minimum fields
  in memory, write only validated normalized warehouse rows, and discard the
  response body. Do not create raw payload, raw audit, quarantine, or backup
  copies.
- Derive `sleep_date` from `waketime_utc` converted through `HOME_TIMEZONE`
  with `zoneinfo`. Test daylight-saving changes, UTC boundaries, and sleeps
  crossing midnight. Provider request dates are not warehouse date authority.
- Hold `data/.healthhub.lock` with `fcntl.flock()` for every live warehouse
  write. Keep the transaction, idempotent upsert, chronological feature
  recompute, commit, and DuckDB `CHECKPOINT` inside that lock.
- Recompute from the earliest changed sleep date forward. Preserve prior-only
  `hrv_z`, morning-D sleep alignment to evening `feeling[D]`, no sleep
  forward-fill, and no mood-label imputation.
- Keep tokens, health rows, databases, and private evidence
  out of git and general logs. Private directories use mode `0700`; sensitive
  files use `0600`.
- Emit only a privacy-minimized aggregate sync attestation: status, bounded
  request window, counts, source `oura`, code/config hashes, recompute
  boundary, and checkpoint result. It contains no raw payload, sleep value,
  mood value, token, response body, or stable provider identifier.

## Deliverables

- `src/ingestion/oura_auth.py`
- `src/ingestion/oura_sync.py`
- `src/warehouse/locking.py`
- `scripts/sync_oura.py`
- `scripts/evidence/oura_sync_attestation.py`
- `scripts/verify_s12_sync.py`
- `tests/ingestion/test_oura_retention.py`
- focused authentication, mapping, ingestion, locking, recompute, and evidence tests
- `docs/reviews/s12-autonomous-security-privacy-review.md`
- `docs/reviews/s12-autonomous-ingestion-integrity-review.md`
- `docs/evidence/s12-oura-production-sync-command-evidence.json`

## Verification

- Authentication tests use deterministic fake transports. No test uses live
  credentials or makes a provider call.
- Ingestion tests cover idempotency, complete pagination, rollback after
  partial failure, wake-date and DST mapping, lock contention, transaction plus
  `CHECKPOINT`, secure first creation, zero raw-response retention, and
  aggregate-attestation forbidden fields.
- Recompute tests cover earliest-change propagation, chronological order,
  prior-only `hrv_z`, no sleep forward-fill, no mood imputation, and continued
  exclusion of all 8 Sleep rows.
- Independent security/privacy and ingestion-integrity
  `autonomous_gate_review` artifacts must distinguish deterministic fixtures
  from real production activation evidence.

## Two-Boundary Completion

Hermetic ship acceptance runs deterministic tests, reviews, provider-policy
verification, and tracked-data hygiene in the detached ship worktree. It does
not open private runtime evidence or call Oura.

Only after hermetic acceptance passes may the configured read-only activation
check inspect aggregate evidence at the canonical runtime root:

`python scripts/verify_s12_sync.py --runtime-root-env HEALTH_HUB_RUNTIME_ROOT --json`

`HEALTH_HUB_RUNTIME_READ_ONLY=1` prohibits verifier writes. Missing, stale,
malformed, or non-`ok` aggregate production-sync evidence keeps S12 incomplete.
The verifier must never trigger a sync, reveal credentials, or read raw health
rows into its output.

## Stop Conditions

- Authentication, pagination, mapping, locking, transaction, `CHECKPOINT`,
  chronological recompute, secure-mode, or aggregate-attestation verification
  fails: stop without partial-success evidence.
- A token, raw provider response, raw health value, database, or private evidence
  enters git or general logs: stop.
- Any raw provider response, raw audit copy, or raw-payload backup is written to
  disk: stop.
- Any implementation activates, blends, averages, reconciles, or substitutes
  8 Sleep data: stop and require a separate provider-reopening slice.
- Real activation evidence is absent: keep S12 incomplete; do not fabricate it.

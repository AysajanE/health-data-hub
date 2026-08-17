# S12 Oura Production Sync Autoplan

Slice ID: S12
Lane: compiler
Risk: high
Revision: 2 (2026-08-17 personal Oura production sync)

S12 builds the production Oura import for this private, single-user personal
tool. Oura remains the only active v1 sleep source. 8 Sleep remains inactive
and excluded from feature construction.

Manual gates are forbidden. High-risk decisions require deterministic checks,
sanitized evidence, and independent `autonomous_gate_review` artifacts.

## Deliverables

- `src/ingestion/oura_auth.py`: private Oura API v2 authentication, expiry
  handling, and atomic refresh-token rotation.
- `src/ingestion/oura_sync.py`: minimum-field paginated sleep import,
  wake-date mapping, idempotent writes, and chronological recompute.
- `src/warehouse/locking.py`: shared `fcntl.flock()` warehouse write guard if
  S11 has not already supplied the identical primitive.
- `scripts/sync_oura.py`: fail-closed production entrypoint.
- `scripts/evidence/oura_sync_attestation.py`: aggregate-only private evidence
  writer.
- `scripts/verify_s12_sync.py`: read-only production-sync activation check.
- `tests/ingestion/test_oura_retention.py`: proves raw provider responses are
  never written to disk on success or failure.
- Focused authentication, mapping, ingestion, locking, recompute, security,
  and evidence-integrity tests.
- Security/privacy and ingestion-integrity `autonomous_gate_review` artifacts
  plus sanitized command evidence.

## Frozen Behavior

### Authentication and transport

- Use the minimum Oura API v2 scopes needed for sleep import.
- Keep access and refresh tokens only in approved private local storage. A
  file-backed token is mode `0600`. Tokens never appear in query strings, git,
  logs, evidence, exception bodies, or test fixtures.
- Refresh before expiry and rotate refresh tokens with write, `fsync`, and
  atomic replacement. Keep no plaintext token backup.
- Authentication rejection, missing refresh token, scope loss, failed atomic
  rotation, rate limiting, incomplete pagination, or schema drift stops the
  sync and emits no partial-success attestation.

### Mapping, warehouse write, and recompute

- Pull only the needed date window and fields and exhaust pagination before
  warehouse mutation.
- Raw provider responses are never written to disk. Process the minimum fields
  in memory, persist only validated normalized rows, and create no raw payload,
  raw audit, quarantine, or backup copy.
- Derive `sleep_date` from `waketime_utc` converted to `HOME_TIMEZONE` with
  `zoneinfo`. Test daylight-saving folds and gaps, offset changes,
  cross-midnight sleeps, and UTC boundary cases.
- Acquire `data/.healthhub.lock` with `fcntl.flock()` before every live write.
  Hold it through the idempotent transaction, chronological recompute, commit,
  and DuckDB `CHECKPOINT`. There is no unlocked fallback.
- Recompute from the earliest changed sleep date forward. Preserve prior-only
  persisted `hrv_z`, morning-D sleep alignment to evening `feeling[D]`,
  `prior_day_feeling=feeling[D-1]`, no sleep forward-fill, and no mood-label
  imputation.
- Continue to reject all 8 Sleep rows from v1 feature and model inputs.

### Filesystem and attestation

- Create private directories with mode `0700` and sensitive files with mode
  `0600`.
- The aggregate sync attestation may include status, bounded request window,
  counts, source `oura`, code/config hashes, earliest recompute date, and
  checkpoint result. It excludes raw payloads, sleep values, mood values,
  tokens, response bodies, and stable provider identifiers.

### Two-boundary completion

The first boundary is hermetic ship acceptance using deterministic fake
transports. It performs no live provider or private-runtime access. The second
boundary is the exact read-only activation command:

`python scripts/verify_s12_sync.py --runtime-root-env HEALTH_HUB_RUNTIME_ROOT --json`

The activation verifier reads only the canonical aggregate evidence and does
not initiate a sync or mutate the warehouse. Missing, stale, malformed, or
non-`ok` evidence keeps S12 incomplete.

## Implementation Tasks

1. Implement private authentication, minimum scopes, expiry handling, atomic
   refresh-token rotation, and complete redaction with deterministic fake
   transports.
   Files: `src/ingestion/oura_auth.py`; `tests/ingestion/test_oura_auth.py`
   Verify: `python -m pytest tests/ingestion/test_oura_auth.py -q`
2. Implement the paginated minimum-field Oura v2 fetch and strict wake-date
   mapping through `HOME_TIMEZONE`.
   Files: `src/ingestion/oura_sync.py`; `tests/ingestion/test_oura_date_mapping.py`; `tests/ingestion/test_oura_sync.py`
   Verify: `python -m pytest tests/ingestion/test_oura_date_mapping.py tests/ingestion/test_oura_sync.py -q`
3. Integrate the shared write lock, idempotent transaction, chronological
   earliest-change recompute, commit, and DuckDB `CHECKPOINT`.
   Files: `src/warehouse/locking.py`; `src/warehouse/warehouse.py`; `src/warehouse/features.py`; `tests/ingestion/test_oura_sync.py`; `tests/warehouse/test_locking.py`; `tests/warehouse/test_recompute.py`
   Verify: `python -m pytest tests/ingestion/test_oura_sync.py tests/warehouse/test_locking.py tests/warehouse/test_recompute.py tests/test_features.py -q`
4. Add secure creation, zero raw-response retention, and the aggregate-only
   sync attestation with forbidden-field scanning and no partial success.
   Files: `scripts/setup_permissions.py`; `scripts/evidence/oura_sync_attestation.py`; `tests/ingestion/test_oura_retention.py`; `tests/autonomy/test_oura_sync_attestation.py`
   Verify: `python -m pytest tests/ingestion/test_oura_retention.py tests/autonomy/test_oura_sync_attestation.py scripts/test_setup_permissions.py -q`
5. Add the production entrypoint and read-only activation verifier.
   Files: `scripts/sync_oura.py`; `scripts/verify_s12_sync.py`; `tests/ingestion/test_oura_entrypoint.py`
   Verify: `python -m pytest tests/ingestion/test_oura_entrypoint.py -q`; `python scripts/verify_s12_sync.py --runtime-root-env HEALTH_HUB_RUNTIME_ROOT --json`
6. Produce independent security/privacy and ingestion-integrity reviews plus
   sanitized command evidence.
   Files: `docs/reviews/s12-autonomous-security-privacy-review.md`; `docs/reviews/s12-autonomous-ingestion-integrity-review.md`; `docs/evidence/s12-oura-production-sync-command-evidence.json`
   Verify: `python scripts/check_autonomous_review_exists.py S12`; `python scripts/check_no_tracked_data.py`

## Verification Expectations

- All focused tests pass without live credentials or network access.
- `python scripts/verify_s05_provider_policy.py --json` still proves Oura-only
  model provenance.
- `python scripts/verify_s12_sync.py --runtime-root-env HEALTH_HUB_RUNTIME_ROOT --json`
  passes only after real, privacy-minimized production evidence exists.
- Both required independent reviews and tracked-data hygiene pass.

## Stop Conditions

- Authentication, pagination, mapping, locking, transaction, `CHECKPOINT`,
  chronological recompute, secure-mode, or aggregate-attestation verification
  fails: stop without partial-success evidence.
- Any token, raw provider response, raw health value, database, or private evidence
  enters git or general logs: stop.
- Any raw provider response, raw audit copy, or raw-payload backup is written to
  disk: stop.
- Any 8 Sleep activation or provider-policy change appears: stop and require a
  separate explicit provider-reopening slice.

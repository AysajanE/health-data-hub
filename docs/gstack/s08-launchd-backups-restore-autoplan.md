# S08 launchd Backups Restore Autoplan

Slice ID: S08
Lane: swr_preferred
Risk: high
Revision: 2 (2026-06-11 readiness enrichment: added the design's launchd/backup/restore surfaces, reconciled the encrypted-bundle-vs-redaction wording with the design, and added write roots and hermetic-test discipline)

## Scope

Implement the v1 operational layer per the design: two launchd agents
(morning sync at 08:00 invoking the data sync, evening retrain at 23:00
invoking scripts/retrain_model.py), encrypted backups of the local data plane
to a configurable target directory (iCloud Drive by default), and a verified
restore path. Preserve 8 Sleep fallback-only behavior and never track secrets
or health data.

Authority: docs/gstack/health-data-hub-office-hours.md ("Local-first /
user-owned data": backups to iCloud Drive encrypted with age; "Model
Lifecycle": the two launchd plists and their separation; "Testing Strategy":
freshness checks and quarantine exclusion).

## Constraints

- Manual gates are forbidden; use autonomous_gate_review artifacts for restore and secrets-logging behavior.
- ENCRYPTION VS REDACTION (reconciled with the design): the encrypted backup archive MAY contain the data plane including secrets-bearing files (data/warehouse.duckdb, models/, optionally .env.local) because the archive itself is encrypted at rest; everything OUTSIDE the archive - logs, backup manifests, restore evidence, command evidence, test output - must contain only file NAMES and digests, never credential values or raw health data. Quarantine payloads (data/quarantine/) are excluded from backups by design.
- Encryption uses the `age` tool when present, with a passphrase or recipient key supplied via environment variable names only; when `age` is absent the backup script must fail closed with a clear message naming the dependency - tests must be hermetic and must not require `age`, network access, or iCloud (use a temp directory target and a stub cipher layer or skip-with-reason for the cipher step).
- launchd plists are tracked TEMPLATES under ops/launchd/ plus an installer script that renders absolute paths at install time; the slice must not install or load agents into the live LaunchAgents directory during tests, and no test may invoke launchctl against the user session.
- Treat `PYEIGHT_EMAIL`, `PYEIGHT_PASSWORD`, `PYEIGHT_CLIENT_ID`, `PYEIGHT_CLIENT_SECRET`, `EIGHT_SLEEP_TOKEN`, and `EIGHT_SLEEP_PASSWORD` as sensitive if present; their absence must not fail v1, and S08 must not require pyEight or 8 Sleep credentials.
- Restore must be verified round-trip in tests: back up a synthetic data plane, restore into a fresh directory, and compare digests; restore must never overwrite an existing live data plane without an explicit force flag.
- Do not commit raw health data, provider payloads, tokens, DuckDB files, snapshots, quarantine payloads, or `.env`.

## Allowed Write Roots

- `ops/launchd/`
- `scripts/backup_data_plane.py`
- `scripts/restore_data_plane.py`
- `scripts/install_launchd_agents.py`
- `tests/backup/`
- `docs/reviews/s08-autonomous-restore-review.md`
- `docs/reviews/s08-autonomous-secrets-logging-review.md`

## Out of Scope

- Changing what the sync or retrain entrypoints DO (S03/S05 own those); S08 only schedules them.
- Cloud services beyond a directory target (no APIs, no accounts); Tailscale; remote restore.
- Modifying scripts/check_no_tracked_data.py beyond what redaction tests require.
- Any UI changes (S07) or model changes (S05/S06).

## Deliverables

- `ops/launchd/health.sync.plist.template` and `ops/launchd/health.retrain.plist.template`
- `scripts/install_launchd_agents.py` (renders and installs templates; prints next steps; never auto-loads during tests)
- `scripts/backup_data_plane.py` (encrypted archive of the data plane to the configured target; quarantine excluded; manifest with names and digests only)
- `scripts/restore_data_plane.py` (round-trip verified restore with force-flag protection)
- `tests/backup/` (hermetic round-trip, redaction, exclusion, and template-rendering tests)
- `docs/reviews/s08-autonomous-restore-review.md`
- `docs/reviews/s08-autonomous-secrets-logging-review.md`

## Implementation Tasks

### launchd agent templates and installer

- [ ] Add the two plist templates (08:00 sync, 23:00 retrain per the design) and the installer that renders absolute paths and writes install instructions without loading agents in tests.
  Files: `ops/launchd/`; `scripts/install_launchd_agents.py`; `tests/backup`
  Verify: `python -m pytest tests/backup -q`

### Encrypted backup and verified restore

- [ ] Implement backup and restore scripts per the encryption-vs-redaction constraint: encrypted archive containing the data plane, quarantine excluded, names-and-digests-only manifest, fail-closed when the cipher dependency is missing, round-trip restore with force-flag protection.
  Files: `scripts/backup_data_plane.py`; `scripts/restore_data_plane.py`; `tests/backup`
  Verify: `python -m pytest tests/backup -q`

### Provider credential redaction

- [ ] Ensure logs, manifests, and evidence redact the sensitive credential names' values, and that absence of 8 Sleep credentials does not fail any S08 path.
  Files: `tests/backup`
  Verify: `python -m pytest tests/backup -q`; `python scripts/check_no_tracked_data.py`

### Autonomous restore and secrets reviews

- [ ] Produce autonomous_gate_review artifacts for restore behavior and secrets logging, each with a command-evidence JSON whose commands all exit zero, following the S05 review-artifact pattern.
  Files: `docs/reviews/s08-autonomous-restore-review.md`; `docs/reviews/s08-autonomous-secrets-logging-review.md`
  Verify: `python scripts/check_autonomous_review_exists.py S08`

## Verification Expectations

- `python -m pytest tests/backup -q` passes.
- `python scripts/check_autonomous_review_exists.py S08` passes.
- `python scripts/check_no_tracked_data.py` passes.

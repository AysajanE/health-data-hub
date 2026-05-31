# S08 launchd Backups Restore Autoplan

Slice ID: S08
Lane: swr_preferred
Risk: high

## Scope

Implement local launchd, backup, and restore workflows for v1 without tracking secrets or health data. Preserve 8 Sleep fallback-only behavior.

## Constraints

- Manual gates are forbidden; use autonomous_gate_review artifacts for restore and secrets-logging behavior.
- Treat `PYEIGHT_EMAIL`, `PYEIGHT_PASSWORD`, `PYEIGHT_CLIENT_ID`, `PYEIGHT_CLIENT_SECRET`, `EIGHT_SLEEP_TOKEN`, and `EIGHT_SLEEP_PASSWORD` as sensitive if present.
- Absence of 8 Sleep credentials must not fail v1.
- S08 must not require pyEight or 8 Sleep credentials.
- Logs, backups, restore evidence, and command evidence must redact provider credential values.
- Do not commit raw health data, provider payloads, tokens, DuckDB files, snapshots, quarantine payloads, or `.env`.

## Deliverables

- `scripts/check_no_tracked_data.py`
- `tests/backup/`
- `docs/reviews/s08-autonomous-restore-review.md`
- `docs/reviews/s08-autonomous-secrets-logging-review.md`

## Implementation Tasks

### Provider credential redaction

- [ ] Ensure S08 redacts optional 8 Sleep credential names and does not require their presence.
  Files: `scripts/check_no_tracked_data.py`; `tests/backup`
  Verify: `python -m pytest tests/backup -q`; `python scripts/check_no_tracked_data.py`

### Autonomous restore and secrets reviews

- [ ] Produce autonomous_gate_review artifacts for restore behavior and secrets logging.
  Files: `docs/reviews/s08-autonomous-restore-review.md`; `docs/reviews/s08-autonomous-secrets-logging-review.md`
  Verify: `python scripts/check_autonomous_review_exists.py S08`

## Verification Expectations

- `python -m pytest tests/backup -q` passes.
- `python scripts/check_autonomous_review_exists.py S08` passes.
- `python scripts/check_no_tracked_data.py` passes.

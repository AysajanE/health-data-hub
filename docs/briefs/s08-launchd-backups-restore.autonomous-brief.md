# S08 launchd Backups Restore Autonomous Brief

Autonomy profile: guarded zero-supervision for S08 only.

Manual gates are forbidden. S08 handles local launchd, backup, and restore workflows without exposing secrets or raw health data.

## Provider Credential Redaction

The following names must be treated as sensitive if they are present, but their absence must not fail v1 because 8 Sleep is fallback-only:

- `PYEIGHT_EMAIL`
- `PYEIGHT_PASSWORD`
- `PYEIGHT_CLIENT_ID`
- `PYEIGHT_CLIENT_SECRET`
- `EIGHT_SLEEP_TOKEN`
- `EIGHT_SLEEP_PASSWORD`

S08 must not require these credentials. Logs, backups, restore evidence, and command evidence must redact them.

## Safety Requirements

- Do not commit data, private evidence, raw provider payloads, snapshots, DuckDB files, tokens, or `.env`.
- Backup and restore evidence must be aggregate and sanitized.
- High-risk restore behavior must be covered by autonomous_gate_review artifacts.

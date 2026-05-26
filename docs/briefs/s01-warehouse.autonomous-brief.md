# S01 Warehouse foundation Autonomous Brief

Autonomy profile: true.

Manual gates are forbidden for this autonomous run. Any former signoff must be represented as an `autonomous_gate_review` artifact, deterministic tests, and recorded evidence.

## Deliverables

- `src/db/schema.sql`
- `src/warehouse/warehouse.py`
- `src/warehouse/models.py`
- `scripts/setup_permissions.py`
- `tests/warehouse/test_schema.py`
- `tests/warehouse/test_mood_correction.py`
- `tests/warehouse/test_quarantine.py`
- `docs/reviews/s01-autonomous-schema-review.md`

## Hard Constraints

- exactly five core tables
- no v2 feature columns in daily_features
- hrv_z persisted
- hrv_avg_ms display-only
- mood_entries plus mood_current correction flow
- no sleep forward fill
- quarantine payloads chmod 0600
- data secrets quarantine snapshots gitignored
- no manual_gate rows

## Required Policy

- Never emit active `manual_gate` rows.
- Never call `keel-run mark-manual-gate`.
- Use narrow repo-relative write roots only.
- Keep raw health data, secrets, tokens, quarantine payloads, snapshots, and DuckDB files out of git and general logs.
- Preserve the retrospective-only v1 scope and statistical gates from the design document.

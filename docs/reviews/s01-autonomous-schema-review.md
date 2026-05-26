# S01 Autonomous Schema Review

Autonomous slice review provenance: independent reviewer for the S01 warehouse foundation slice.

Slice: S01
Verdict: pass
Result: pass
Blocking findings: none

## Scope reviewed

This autonomous review covers the S01 warehouse foundation artifacts for the local-first Health Data Hub v1 sleep and mood warehouse only. The review checks table inventory, locked v1 feature scope, mood correction behavior, quarantine handling, and deterministic verification coverage. No human approval is represented here.

## Evidence files checked

- `docs/gstack/health-data-hub-office-hours.md`
- `docs/gstack/s01-warehouse-autoplan.md`
- `docs/briefs/s01-warehouse.autonomous-brief.md`
- `src/db/schema.sql`
- `src/warehouse/models.py`
- `src/warehouse/warehouse.py`
- `scripts/check_schema_contract.py`
- `tests/warehouse/test_schema.py`
- `tests/warehouse/test_mood_correction.py`
- `tests/warehouse/test_quarantine.py`
- `docs/evidence/s01-po-status-20260524.json`
- `docs/evidence/s01-command-evidence-20260526.json`

## Exact commands run

- `python scripts/check_schema_contract.py`
- `python -m pytest tests/warehouse/test_schema.py tests/warehouse/test_mood_correction.py tests/warehouse/test_quarantine.py -q`
- `python scripts/check_no_tracked_data.py`
- `python scripts/check_autonomous_review_exists.py S01`

Command evidence: docs/evidence/s01-command-evidence-20260526.json

## Table inventory

The reviewed schema defines exactly five core warehouse tables, matching the S01 contract:

1. `sleep_nights`
   Stores source-scoped nightly sleep facts keyed by `(source, sleep_date)`.
2. `mood_entries`
   Stores immutable mood logs, including correction lineage through `supersedes_log_id`.
3. `mood_current`
   Stores the current canonical mood log per `mood_date`.
4. `daily_features`
   Stores the locked v1 model feature row plus display metadata and merge diagnostics.
5. `sleep_merge_diagnostics`
   Stores same-day sleep-source reconciliation metadata and warnings.

No extra core tables were found in `src/db/schema.sql`.

## V1-scope checks

- `daily_features` keeps the locked v1 model inputs only: `total_sleep_min`, `hrv_z`, `deep_sleep_pct`, and `prior_day_feeling`.
- `hrv_avg_ms` is present as display metadata alongside `hrv_z`, which remains a persisted warehouse column.
- No v2 feature columns were found in `daily_features`; the schema contract checker explicitly rejects columns such as training-load, nutrition, or generic imputation fields.
- `compute_daily_features` reads only `sleep_nights` rows whose `sleep_date` exactly matches the requested feature day. When no row exists for that date, it returns `None`, so the S01 implementation does not forward-fill sleep into missing training days.
- Mood labels are sourced through `mood_current` joined to `mood_entries`; no mood-label imputation path was found in the S01 warehouse implementation.
- The implementation remains retrospective-only. This slice contains warehouse and review plumbing only, with no tomorrow-prediction or intervention logic.

## Mood correction checks

- `insert_mood_entry` preserves the original mood log in `mood_entries` and automatically links a later correction back to the previously current row through `supersedes_log_id` when a current entry already exists for the same `mood_date`.
- `mood_current` is updated with `INSERT OR REPLACE`, so training-time joins resolve to the latest accepted mood log for a day.
- `select_current_mood_entries` reads from `mood_current` joined to `mood_entries`, which keeps superseded rows out of the canonical training set.
- `tests/warehouse/test_mood_correction.py` verifies the correction flow by asserting that the second entry supersedes the first, `mood_current` points to the corrected row, and the current-entry query returns only the corrected label.

## Quarantine checks

- Validation failures for sleep and mood payloads are routed through `ValidationFailureMetadata`, which computes a deterministic payload hash and redacted error summary for general logging.
- Private quarantine payloads are written under the configured quarantine directory as JSON files created with mode `0600`.
- General logs receive only `source`, `detected_at_utc`, `error_summary`, and `payload_hash`; raw payload content, tokens, and notes are intentionally excluded.
- `tests/warehouse/test_quarantine.py` covers both sleep and mood validation failures, including the `0600` permission check and the requirement that sensitive fields stay out of the general log while remaining available in the private quarantine payload.

## Deterministic test references

- `scripts/check_schema_contract.py` validates the five-table contract, required columns, forbidden v2 columns, and the `hrv_z` plus `hrv_avg_ms` requirement.
- `tests/warehouse/test_schema.py`
  Includes deterministic checks for:
  - schema creation in an in-memory DuckDB database,
  - Pydantic model validation boundaries,
  - persisted `hrv_z` metadata requirements,
  - warehouse writes, and
  - `compute_daily_features` creation of a v1-aligned `daily_features` row plus merge diagnostics.
- `tests/warehouse/test_mood_correction.py`
  Verifies canonical mood correction semantics for `mood_entries` and `mood_current`.
- `tests/warehouse/test_quarantine.py`
  Verifies quarantine permissions and redacted logging behavior for invalid payloads.

## Review conclusion

The S01 warehouse foundation currently matches the autonomous schema-review contract: table inventory is locked to five core tables, the `daily_features` row stays within v1 scope, mood correction behavior preserves a single canonical label per day, quarantine handling keeps raw payloads private with `0600` permissions, and deterministic validation coverage exists for the reviewed behaviors.

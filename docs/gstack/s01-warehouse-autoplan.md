# S01 Warehouse Foundation Autoplan

Slice ID: S01
Lane: compiler
Risk: medium

## Scope

Build the Health Data Hub v1 warehouse foundation only. The slice deliverables are:

- `src/db/schema.sql`
- `src/warehouse/models.py`
- `src/warehouse/warehouse.py`
- `scripts/setup_permissions.py`
- `tests/warehouse/test_schema.py`
- `tests/warehouse/test_mood_correction.py`
- `tests/warehouse/test_quarantine.py`
- `docs/reviews/s01-autonomous-schema-review.md`

## Constraints

- Manual gates are forbidden; no `manual_gate` rows and no `keel-run mark-manual-gate`.
- Use `docs/reviews/s01-autonomous-schema-review.md` as the autonomous_gate_review artifact instead of human approval.
- Keep write roots narrow and repo-relative.
- Use exactly five core warehouse tables: `sleep_nights`, `mood_entries`, `mood_current`, `daily_features`, and `sleep_merge_diagnostics`.
- Persist `hrv_z`; keep `hrv_avg_ms` as display metadata only.
- Do not add v2 feature columns to `daily_features`.
- Do not forward-fill sleep rows. Mood labels are never imputed.
- Quarantine invalid raw payloads under `data/quarantine/` with file mode `0600`; do not track `data/`, `private/`, `.env`, DuckDB, SQLite, or Parquet files.

## Implementation Tasks

### Warehouse schema

- [ ] Author the canonical DuckDB schema with exactly five core tables and the locked v1 `daily_features` feature set.
  Include a bounded test that opens an in-memory DuckDB database, executes `src/db/schema.sql`, and asserts the five expected tables exist.
  Files: `src/db/schema.sql`; `tests/warehouse/test_schema.py`
  Verify: `python scripts/check_schema_contract.py`; `python -m pytest tests/warehouse/test_schema.py -q`

### Warehouse models

- [ ] Add Pydantic models for the warehouse rows and validation failure metadata.
  Files: `src/warehouse/models.py`; `tests/warehouse/test_schema.py`
  Verify: `python -m pytest tests/warehouse/test_schema.py -q`

### Warehouse write API

- [ ] Implement the local DuckDB connection, schema application, sleep insert, mood insert, mood correction, and daily-feature row helpers.
  Files: `src/warehouse/warehouse.py`; `tests/warehouse/test_schema.py`; `tests/warehouse/test_mood_correction.py`
  Verify: `python -m pytest tests/warehouse/test_schema.py tests/warehouse/test_mood_correction.py -q`

### Quarantine handling

- [ ] Add validation-failure quarantine behavior with `0600` payload files and redacted general logs.
  Files: `src/warehouse/warehouse.py`; `tests/warehouse/test_quarantine.py`
  Verify: `python -m pytest tests/warehouse/test_quarantine.py -q`; `python scripts/check_no_tracked_data.py`

### Permissions and tracked-data safety

- [ ] Add an idempotent permissions script for local data directories and sensitive local files.
  Files: `scripts/setup_permissions.py`
  Verify: `python scripts/setup_permissions.py`; `python scripts/check_no_tracked_data.py`

### Autonomous schema review

- [ ] Generate the S01 autonomous schema review artifact with table inventory, v1-scope checks, mood correction checks, quarantine checks, and deterministic-test references.
  Files: `docs/reviews/s01-autonomous-schema-review.md`
  Verify: `python scripts/check_autonomous_review_exists.py S01`

## Verification Expectations

The slice is complete only when all S01 acceptance commands pass:

- `python -m pytest tests/warehouse -q`
- `python scripts/check_schema_contract.py`
- `python scripts/check_no_tracked_data.py`
- `python scripts/check_autonomous_review_exists.py S01`

## Out Of Scope

- S02 FastAPI mood endpoint and iOS Shortcut transport.
- S03 Oura or 8 Sleep evidence collection.
- S04 source reconciliation and full feature engineering.
- S05 model training, baseline gates, and SHAP.
- S06 counterfactual search.
- S07 Streamlit UI.
- S08 backup/restore.
- Prospective predictions, recommendations, Autopilot, Coach, Garmin, Withings, chest strap, nutrition, or medical advice.

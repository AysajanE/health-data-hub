<!--
playbook_contract: markdown_playbook_v1
compiled_by: gstack_to_markdown_playbook_v1
compiled_at: 2026-05-24T19:28:40+00:00
human_approved_by: AUTO-KEEL-AUTONOMOUS-NOT-HUMAN
source_artifacts:
  - kind: office_hours, path: docs/gstack/health-data-hub-office-hours.md, sha256: 548af49f81b7063427df5bd1758ac6c0d5a38d985d04ca5a3c23120eb605229e
  - kind: autoplan, path: docs/gstack/s01-warehouse-autoplan.md, sha256: 4527ccece946ef98c6c07cf9e62cb61dd9c10602a06ab347b06a338c15985fd5
  - kind: approved_brief, path: docs/briefs/s01-warehouse.autonomous-brief.md, sha256: 1dfef68c749175ffb646e858aaa133bcd216114cdbf4219a61725f1097809179
-->


## 1. Plan Context

S01 builds the local-first warehouse foundation for Health Data Hub v1.

## 2. Ordered Execution Plan

| step_id | phase | action | why_now | owner_type | prerequisites | repo_surfaces | deliverable | exit_criteria | allowed_write_roots | requires_red_green | manual_gate | manual_gate_reason | manual_gate_evidence | external_check | external_dependencies | consult_paths | required_verification_commands | required_verification_artifacts | notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 01 | Warehouse schema | Create the canonical DuckDB schema with exactly five core tables and the locked v1 `daily_features` feature set. | Required for the S01 warehouse foundation acceptance contract. | operator | none | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s01-warehouse-autoplan.md`; `docs/briefs/s01-warehouse.autonomous-brief.md`; `tests` | `src/db/schema.sql`; `tests/warehouse/test_schema.py` | src/db/schema.sql, tests/warehouse/test_schema.py exist with the required S01 behavior; Include a bounded test that opens an in-memory DuckDB database, executes `src/db/schema.sql`, and asserts the five expected tables exist.; python scripts/check_schema_contract.py, python -m pytest tests/warehouse/test_schema.py -q passes | src/db; tests/warehouse | true | none |  |  | none |  | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s01-warehouse-autoplan.md`; `docs/briefs/s01-warehouse.autonomous-brief.md` | python scripts/check_schema_contract.py; python -m pytest tests/warehouse/test_schema.py -q |  | source_task: task_001; Include a bounded test that opens an in-memory DuckDB database, executes `src/db/schema.sql`, and asserts the five expected tables exist.; autokeel deterministic row author |
| 02 | Warehouse models | Add Pydantic models for the warehouse rows and validation failure metadata. | Required for the S01 warehouse foundation acceptance contract. | operator | 01 | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s01-warehouse-autoplan.md`; `docs/briefs/s01-warehouse.autonomous-brief.md`; `tests` | `src/warehouse/models.py`; `tests/warehouse/test_schema.py` | src/warehouse/models.py, tests/warehouse/test_schema.py exist with the required S01 behavior; python -m pytest tests/warehouse/test_schema.py -q passes | src/warehouse; tests/warehouse | true | none |  |  | none |  | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s01-warehouse-autoplan.md`; `docs/briefs/s01-warehouse.autonomous-brief.md` | python -m pytest tests/warehouse/test_schema.py -q |  | source_task: task_002; autokeel deterministic row author |
| 03 | Warehouse write API | Implement the local DuckDB connection, schema application, sleep insert, mood insert, mood correction, and daily-feature row helpers. | Required for the S01 warehouse foundation acceptance contract. | operator | 02 | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s01-warehouse-autoplan.md`; `docs/briefs/s01-warehouse.autonomous-brief.md`; `tests` | `src/warehouse/warehouse.py`; `tests/warehouse/test_schema.py`; `tests/warehouse/test_mood_correction.py` | src/warehouse/warehouse.py, tests/warehouse/test_schema.py, tests/warehouse/test_mood_correction.py exist with the required S01 behavior; python -m pytest tests/warehouse/test_schema.py tests/warehouse/test_mood_correction.py -q passes | src/warehouse; tests/warehouse | true | none |  |  | none |  | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s01-warehouse-autoplan.md`; `docs/briefs/s01-warehouse.autonomous-brief.md` | python -m pytest tests/warehouse/test_schema.py tests/warehouse/test_mood_correction.py -q |  | source_task: task_003; autokeel deterministic row author |
| 04 | Quarantine handling | Add validation-failure quarantine behavior with `0600` payload files and redacted general logs. | Required for the S01 warehouse foundation acceptance contract. | operator | 03 | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s01-warehouse-autoplan.md`; `docs/briefs/s01-warehouse.autonomous-brief.md`; `tests` | `src/warehouse/warehouse.py`; `tests/warehouse/test_quarantine.py` | src/warehouse/warehouse.py, tests/warehouse/test_quarantine.py exist with the required S01 behavior; python -m pytest tests/warehouse/test_quarantine.py -q, python scripts/check_no_tracked_data.py passes | src/warehouse; tests/warehouse | true | none |  |  | none |  | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s01-warehouse-autoplan.md`; `docs/briefs/s01-warehouse.autonomous-brief.md` | python -m pytest tests/warehouse/test_quarantine.py -q; python scripts/check_no_tracked_data.py |  | source_task: task_004; autokeel deterministic row author |
| 05 | Permissions and tracked-data safety | Add an idempotent permissions script for local data directories and sensitive local files. | Required for the S01 warehouse foundation acceptance contract. | operator | 04 | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s01-warehouse-autoplan.md`; `docs/briefs/s01-warehouse.autonomous-brief.md`; `scripts` | `scripts/setup_permissions.py` | scripts/setup_permissions.py exist with the required S01 behavior; python scripts/setup_permissions.py, python scripts/check_no_tracked_data.py passes | scripts | true | none |  |  | none |  | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s01-warehouse-autoplan.md`; `docs/briefs/s01-warehouse.autonomous-brief.md` | python scripts/setup_permissions.py; python scripts/check_no_tracked_data.py |  | source_task: task_005; autokeel deterministic row author |
| 06 | Autonomous schema review | Generate the S01 autonomous schema review artifact with table inventory, v1-scope checks, mood correction checks, quarantine checks, and deterministic-test references. | Required for the S01 warehouse foundation acceptance contract. | operator | 05 | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s01-warehouse-autoplan.md`; `docs/briefs/s01-warehouse.autonomous-brief.md`; `docs/reviews` | `docs/reviews/s01-autonomous-schema-review.md` | docs/reviews/s01-autonomous-schema-review.md exist with the required S01 behavior; python scripts/check_autonomous_review_exists.py S01 passes | docs/reviews | true | none |  |  | none |  | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s01-warehouse-autoplan.md`; `docs/briefs/s01-warehouse.autonomous-brief.md` | python scripts/check_autonomous_review_exists.py S01 |  | source_task: task_006; autokeel deterministic row author |

## 3. Phase Details

### 3.1 Warehouse schema

Executes S01 task rows for Warehouse schema.

### 3.2 Warehouse models

Executes S01 task rows for Warehouse models.

### 3.3 Warehouse write API

Executes S01 task rows for Warehouse write API.

### 3.4 Quarantine handling

Executes S01 task rows for Quarantine handling.

### 3.5 Permissions and tracked-data safety

Executes S01 task rows for Permissions and tracked-data safety.

### 3.6 Autonomous schema review

Executes S01 task rows for Autonomous schema review.

## 4. Shared Guidance

### 4.1 Autonomous Gate Policy

Manual gates are forbidden; deterministic tests and autonomous review artifacts are required instead.

### 4.2 Health Data Safety

Do not commit raw health data, provider payloads, secrets, DuckDB files, snapshots, or quarantine payloads.

## 5. Risks And Contingencies

If verification fails, stop and record an AutoKeel failure instead of fabricating evidence.

## 6. Immediate Next Actions

Run S01 acceptance commands only after all rows complete.

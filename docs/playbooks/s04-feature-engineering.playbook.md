<!--
playbook_contract: markdown_playbook_v1
compiled_by: gstack_to_markdown_playbook_v1
compiled_at: 2026-05-30T19:13:28+00:00
human_approved_by: AUTO-KEEL-AUTONOMOUS-NOT-HUMAN
source_artifacts:
  - kind: office_hours, path: docs/gstack/health-data-hub-office-hours.md, sha256: 548af49f81b7063427df5bd1758ac6c0d5a38d985d04ca5a3c23120eb605229e
  - kind: autoplan, path: docs/gstack/s04-feature-engineering-autoplan.md, sha256: c38528c9bda2264e6e88384cb6e02028ab5f36469fe58015991ecbd5e14d68af
  - kind: approved_brief, path: docs/briefs/s04-feature-engineering.autonomous-brief.md, sha256: 6f4215a78823d1bda9a7fd31304ff7d51bab93a82562b1e4844dc6b5f3850725
-->


## 1. Plan Context

S04 builds one Health Data Hub v1 slice under the autonomous AutoKeel policy.

## 2. Ordered Execution Plan

| step_id | phase | action | why_now | owner_type | prerequisites | repo_surfaces | deliverable | exit_criteria | allowed_write_roots | requires_red_green | manual_gate | manual_gate_reason | manual_gate_evidence | external_check | external_dependencies | consult_paths | required_verification_commands | required_verification_artifacts | notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 01 | S04 readiness and provider-decision guard | Add or update tests proving feature engineering reads the active S03 provider decision and treats Oura-only v1 as the first-class feature source. The tests must fail if pyEight evidence is required or 8 Sleep is treated as active without a | Required for the S04 acceptance contract. | operator | none | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s04-feature-engineering-autoplan.md`; `docs/briefs/s04-feature-engineering.autonomous-brief.md`; `tests` | `tests/test_features.py` | tests/test_features.py exist with the required S04 behavior; python scripts/verify_s04_readiness.py --json, python -m pytest tests/test_features.py -q passes | tests/test_features.py | true | none |  |  | none |  | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s04-feature-engineering-autoplan.md`; `docs/briefs/s04-feature-engineering.autonomous-brief.md` | python scripts/verify_s04_readiness.py --json; python -m pytest tests/test_features.py -q |  | source_task: task_001; autokeel deterministic row author |
| 02 | Daily feature construction | Implement deterministic daily feature construction from warehouse mood and Oura sleep rows. For date `D`, use the sleep night ending on morning `D`, join the same-day mood label `feeling[D]`, and join `prior_day_feeling` from `D-1`. Do not | Required for the S04 acceptance contract. | operator | 01 | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s04-feature-engineering-autoplan.md`; `docs/briefs/s04-feature-engineering.autonomous-brief.md`; `src/warehouse`; `tests` | `src/warehouse/features.py`; `tests/test_features.py` | src/warehouse/features.py, tests/test_features.py exist with the required S04 behavior; python -m pytest tests/test_features.py -q passes | src/warehouse; tests/test_features.py | true | none |  |  | none |  | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s04-feature-engineering-autoplan.md`; `docs/briefs/s04-feature-engineering.autonomous-brief.md` | python -m pytest tests/test_features.py -q |  | source_task: task_002; autokeel deterministic row author |
| 03 | Prior-only HRV z-score persistence | Implement prior-only `hrv_z` calculation and persistence. Each day may use only earlier eligible days for its HRV baseline; the current day must not contribute to its own z-score. Keep `hrv_avg_ms` as display metadata only, not a model feat | Required for the S04 acceptance contract. | operator | 02 | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s04-feature-engineering-autoplan.md`; `docs/briefs/s04-feature-engineering.autonomous-brief.md`; `src/warehouse`; `tests` | `src/warehouse/features.py`; `tests/test_features.py` | src/warehouse/features.py, tests/test_features.py exist with the required S04 behavior; python -m pytest tests/test_features.py -q passes | src/warehouse; tests/test_features.py | true | none |  |  | none |  | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s04-feature-engineering-autoplan.md`; `docs/briefs/s04-feature-engineering.autonomous-brief.md` | python -m pytest tests/test_features.py -q |  | source_task: task_003; autokeel deterministic row author |
| 04 | Sleep merge diagnostics under Oura-only v1 | Implement sleep-source diagnostics that collapse to Oura-only identity under the active S03 fallback. Diagnostics may state that 8 Sleep is absent/fallback, but must not blend or average 8 Sleep values into v1 features. | Required for the S04 acceptance contract. | operator | 03 | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s04-feature-engineering-autoplan.md`; `docs/briefs/s04-feature-engineering.autonomous-brief.md`; `src/warehouse`; `tests` | `src/warehouse/features.py`; `tests/test_features.py` | src/warehouse/features.py, tests/test_features.py exist with the required S04 behavior; python -m pytest tests/test_features.py -q passes | src/warehouse; tests/test_features.py | true | none |  |  | none |  | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s04-feature-engineering-autoplan.md`; `docs/briefs/s04-feature-engineering.autonomous-brief.md` | python -m pytest tests/test_features.py -q |  | source_task: task_004; autokeel deterministic row author |
| 05 | Hygiene and acceptance evidence | Implement record sanitized S04 command evidence if useful for review, then run the S04 acceptance contract. No raw health data, provider payloads, private evidence contents, tokens, or DuckDB files may be committed. | Required for the S04 acceptance contract. | operator | 04 | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s04-feature-engineering-autoplan.md`; `docs/briefs/s04-feature-engineering.autonomous-brief.md`; `docs/evidence`; `tests` | `docs/evidence/s04-feature-engineering-command-evidence.json`; `tests/test_features.py` | docs/evidence/s04-feature-engineering-command-evidence.json, tests/test_features.py exist with the required S04 behavior; python scripts/verify_s04_readiness.py --json, python -m pytest tests/test_features.py -q, python scripts/check_no_tracked_data.py passes | docs/evidence; tests/test_features.py | true | none |  |  | none |  | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s04-feature-engineering-autoplan.md`; `docs/briefs/s04-feature-engineering.autonomous-brief.md` | python scripts/verify_s04_readiness.py --json; python -m pytest tests/test_features.py -q; python scripts/check_no_tracked_data.py |  | source_task: task_005; autokeel deterministic row author |

## 3. Phase Details

### 3.1 S04 readiness and provider-decision guard

Executes S04 task rows for S04 readiness and provider-decision guard.

### 3.2 Daily feature construction

Executes S04 task rows for Daily feature construction.

### 3.3 Prior-only HRV z-score persistence

Executes S04 task rows for Prior-only HRV z-score persistence.

### 3.4 Sleep merge diagnostics under Oura-only v1

Executes S04 task rows for Sleep merge diagnostics under Oura-only v1.

### 3.5 Hygiene and acceptance evidence

Executes S04 task rows for Hygiene and acceptance evidence.

## 4. Shared Guidance

### 4.1 Autonomous Gate Policy

Manual gates are forbidden; autonomous_gate_review artifacts, deterministic tests, and recorded evidence are required instead.

### 4.2 Health Data Safety

Do not commit raw health data, provider payloads, secrets, DuckDB files, snapshots, or quarantine payloads.

## 5. Risks And Contingencies

If verification fails, stop and record an AutoKeel failure instead of fabricating evidence.

## 6. Immediate Next Actions

Run S04 acceptance commands only after all rows complete.

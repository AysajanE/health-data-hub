<!--
playbook_contract: markdown_playbook_v1
compiled_by: gstack_to_markdown_playbook_v1
compiled_at: 2026-05-29T21:27:29+00:00
human_approved_by: AUTO-KEEL-AUTONOMOUS-NOT-HUMAN
source_artifacts:
  - kind: office_hours, path: docs/gstack/health-data-hub-office-hours.md, sha256: 548af49f81b7063427df5bd1758ac6c0d5a38d985d04ca5a3c23120eb605229e
  - kind: autoplan, path: docs/gstack/s03-ingestion-provider-autoplan.md, sha256: f17f0e60a7486f902fd152f58e0ac97d6a5389e79f5ebe1bdbcabb43dea3c2a6
  - kind: approved_brief, path: docs/briefs/s03-ingestion-provider.autonomous-brief.md, sha256: e62d7ef98ecfe651d4a2e3096d46bac079713719b002f65c2bdef4f1b38e28b3
-->


## 1. Plan Context

S03 builds one Health Data Hub v1 slice under the autonomous AutoKeel policy.

## 2. Ordered Execution Plan

| step_id | phase | action | why_now | owner_type | prerequisites | repo_surfaces | deliverable | exit_criteria | allowed_write_roots | requires_red_green | manual_gate | manual_gate_reason | manual_gate_evidence | external_check | external_dependencies | consult_paths | required_verification_commands | required_verification_artifacts | notes |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 01 | Oura smoke evidence collector and provider-path decision | Implement provide the Oura sleep smoke collector and its shared redaction/report helper. The collector pulls a 7-day sleep window from the live local Oura API v2 using `OURA_ACCESS_TOKEN`, writes a redacted, `0600`, aggregate-only report un | Required for the S03 acceptance contract. | operator | none | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s03-ingestion-provider-autoplan.md`; `docs/briefs/s03-ingestion-provider.autonomous-brief.md`; `scripts/evidence`; `scripts/evidence/oura_smoke.py`; `scripts/evidence/_collector_common.py` | `scripts/evidence/oura_smoke.py`; `scripts/evidence/_collector_common.py` | scripts/evidence/oura_smoke.py, scripts/evidence/_collector_common.py exist with the required S03 behavior; python scripts/evidence/oura_smoke.py --json passes | scripts/evidence | true | none |  |  | none |  | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s03-ingestion-provider-autoplan.md`; `docs/briefs/s03-ingestion-provider.autonomous-brief.md` | python scripts/evidence/oura_smoke.py --json |  | source_task: task_001; autokeel deterministic row author |
| 02 | Fail-closed handling for missing Oura credentials | Implement ensure the collector returns `status: blocked_external` and exits non-zero when `OURA_ACCESS_TOKEN` is absent or offline mode is requested, writing only redacted env markers (no secret values). On this path the run appends an **op | Required for the S03 acceptance contract. | operator | 01 | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s03-ingestion-provider-autoplan.md`; `docs/briefs/s03-ingestion-provider.autonomous-brief.md`; `scripts/evidence`; `scripts/evidence/oura_smoke.py`; `ops/autonomy`; `ops/autonomy/failure_ledger.jsonl` | `scripts/evidence/oura_smoke.py`; `ops/autonomy/failure_ledger.jsonl` | scripts/evidence/oura_smoke.py, ops/autonomy/failure_ledger.jsonl exist with the required S03 behavior; python scripts/verify_s03_readiness.py --json passes | scripts/evidence; ops/autonomy | true | none |  |  | none |  | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s03-ingestion-provider-autoplan.md`; `docs/briefs/s03-ingestion-provider.autonomous-brief.md` | python scripts/verify_s03_readiness.py --json |  | source_task: task_002; autokeel deterministic row author |
| 03 | 8 Sleep optional smoke and week-2 tripwire fallback | Implement provide the optional 8 Sleep (`pyEight`) smoke collector. It authenticates via the current 8 Sleep token flow using `PYEIGHT_*` env, confirms recent sleep-trend reachability, and writes an aggregate-only, `0600` report under `priv | Required for the S03 acceptance contract. | operator | 02 | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s03-ingestion-provider-autoplan.md`; `docs/briefs/s03-ingestion-provider.autonomous-brief.md`; `scripts/evidence`; `scripts/evidence/pyeight_smoke.py`; `ops/autonomy/decisions` | `scripts/evidence/pyeight_smoke.py`; `ops/autonomy/decisions/` | scripts/evidence/pyeight_smoke.py exists and ops/autonomy/decisions/ contains the current pyEight provider decision; fallback decision files are required only when the command returns fallback_accepted; python scripts/evidence/pyeight_smoke.py --json passes with status ok or fallback_accepted | scripts/evidence; ops/autonomy/decisions | true | none |  |  | none |  | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s03-ingestion-provider-autoplan.md`; `docs/briefs/s03-ingestion-provider.autonomous-brief.md` | python scripts/evidence/pyeight_smoke.py --json |  | source_task: task_003; autokeel deterministic row author |
| 04 | Committed redacted ingestion evidence summary and command evidence | Create the committed, fully redacted ingestion evidence summary that records the provider decision-of-record (Oura path; 8 Sleep included vs `oura_only_v1` fallback) and points at the runtime evidence reports by relative path. Author the ma | Required for the S03 acceptance contract. | operator | 03 | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s03-ingestion-provider-autoplan.md`; `docs/briefs/s03-ingestion-provider.autonomous-brief.md`; `docs/evidence` | `docs/evidence/ingestion/s03-ingestion-evidence.md`; `docs/evidence/ingestion/s03-command-evidence.json` | docs/evidence/ingestion/s03-ingestion-evidence.md, docs/evidence/ingestion/s03-command-evidence.json exist with the required S03 behavior; python scripts/check_no_tracked_data.py passes | docs/evidence/ingestion | true | none |  |  | none |  | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s03-ingestion-provider-autoplan.md`; `docs/briefs/s03-ingestion-provider.autonomous-brief.md` | python scripts/check_no_tracked_data.py |  | source_task: task_004; autokeel deterministic row author |
| 05 | Autonomous ingestion evidence review | Generate the S03 autonomous review artifact. It must state autonomous-reviewer provenance, carry `Verdict: pass` (and no failing verdict), list the evidence files checked, list the exact commands run, include at least one `Command evidence: | Required for the S03 acceptance contract. | operator | 04 | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s03-ingestion-provider-autoplan.md`; `docs/briefs/s03-ingestion-provider.autonomous-brief.md`; `docs/reviews` | `docs/reviews/s03-autonomous-ingestion-evidence-review.md` | docs/reviews/s03-autonomous-ingestion-evidence-review.md exist with the required S03 behavior; python scripts/check_autonomous_review_exists.py S03 passes | docs/reviews | true | none |  |  | none |  | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s03-ingestion-provider-autoplan.md`; `docs/briefs/s03-ingestion-provider.autonomous-brief.md` | python scripts/check_autonomous_review_exists.py S03 |  | source_task: task_005; autokeel deterministic row author |
| 06 | Data-hygiene and S03 readiness gate | Implement confirm no health data, tokens, raw payloads, snapshots, or DuckDB/Parquet files are tracked, that `private/evidence/S03/` stays gitignored, and that the full S03 readiness contract holds (S01/S02 complete; Oura evidence present o | Required for the S03 acceptance contract. | operator | 05 | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s03-ingestion-provider-autoplan.md`; `docs/briefs/s03-ingestion-provider.autonomous-brief.md`; `docs/evidence` | `docs/evidence/ingestion/s03-ingestion-evidence.md` | docs/evidence/ingestion/s03-ingestion-evidence.md exist with the required S03 behavior; python scripts/check_no_tracked_data.py, python scripts/verify_s03_readiness.py --json passes | docs/evidence/ingestion | true | none |  |  | none |  | `docs/gstack/health-data-hub-office-hours.md`; `docs/gstack/s03-ingestion-provider-autoplan.md`; `docs/briefs/s03-ingestion-provider.autonomous-brief.md` | python scripts/check_no_tracked_data.py; python scripts/verify_s03_readiness.py --json |  | source_task: task_006; autokeel deterministic row author |

## 3. Phase Details

### 3.1 Oura smoke evidence collector and provider-path decision

Executes S03 task rows for Oura smoke evidence collector and provider-path decision.

### 3.2 Fail-closed handling for missing Oura credentials

Executes S03 task rows for Fail-closed handling for missing Oura credentials.

### 3.3 8 Sleep optional smoke and week-2 tripwire fallback

Executes S03 task rows for 8 Sleep optional smoke and week-2 tripwire fallback.

### 3.4 Committed redacted ingestion evidence summary and command evidence

Executes S03 task rows for Committed redacted ingestion evidence summary and command evidence.

### 3.5 Autonomous ingestion evidence review

Executes S03 task rows for Autonomous ingestion evidence review.

### 3.6 Data-hygiene and S03 readiness gate

Executes S03 task rows for Data-hygiene and S03 readiness gate.

## 4. Shared Guidance

### 4.1 Autonomous Gate Policy

Manual gates are forbidden; autonomous_gate_review artifacts, deterministic tests, and recorded evidence are required instead.

### 4.2 Health Data Safety

Do not commit raw health data, provider payloads, secrets, DuckDB files, snapshots, or quarantine payloads.

## 5. Risks And Contingencies

If verification fails, stop and record an AutoKeel failure instead of fabricating evidence.

## 6. Immediate Next Actions

Run S03 acceptance commands only after all rows complete.

# S03 Ingestion Provider Decision Autoplan

Slice ID: S03
Lane: compiler_external_evidence
Risk: high

## Scope

Make and record the Health Data Hub v1 **ingestion-provider decision** from real, locally collected external evidence — nothing more. This slice runs the Oura sleep smoke collector against the live local Oura API, runs the optional 8 Sleep (`pyEight`) smoke collector, resolves the week-1 Oura provider tripwire and the week-2 8 Sleep tripwire, and lands the autonomous review artifact that substitutes for a human "is ingestion viable?" gate. It writes no downstream ingestion pipeline: no warehouse inserts, no feature engineering, no model code. The slice deliverables are:

- `scripts/evidence/oura_smoke.py`
- `scripts/evidence/pyeight_smoke.py`
- `scripts/evidence/_collector_common.py`
- `docs/evidence/ingestion/s03-ingestion-evidence.md`
- `docs/evidence/ingestion/s03-command-evidence.json`
- `docs/reviews/s03-autonomous-ingestion-evidence-review.md`
- `ops/autonomy/decisions/S03-pyeight-fallback-<timestamp>.json` (written **only** when the week-2 8 Sleep tripwire fires)

Runtime, non-committed evidence is written under `private/evidence/S03/{oura_smoke,pyeight_smoke}/` (gitignored) by the collectors; the `blocked_external` failure path appends to `ops/autonomy/failure_ledger.jsonl`.

## Constraints

- Manual gates are forbidden; no `manual_gate` rows and no `keel-run mark-manual-gate`.
- Use `docs/reviews/s03-autonomous-ingestion-evidence-review.md` as the `autonomous_gate_review` artifact in lieu of human approval. It must reference the deterministic collector runs and committed command evidence, and must never represent itself as human signoff.
- Write only under narrow repo-relative roots: `scripts/evidence/`, `docs/evidence/ingestion/`, `docs/reviews/`, `ops/autonomy/decisions/`, and `ops/autonomy/failure_ledger.jsonl`. Runtime collector output goes only to `private/evidence/S03/` (gitignored). Do not touch `src/warehouse/`, `src/api/`, `src/db/`, model or UI code, `data/`, `private/` (except as collector output), `.env`, or any secrets file.
- **Fail closed.** When required external credentials are absent (`OURA_ACCESS_TOKEN`), the Oura collector must return `status: blocked_external` and exit non-zero, and the run must append an **open** `blocked_external_missing_evidence` failure row for S03 to `ops/autonomy/failure_ledger.jsonl`. Never fabricate an `ok` status, never simulate a gate, and never mark the slice complete on the blocked path.
- Keep raw provider payloads, bearer/refresh tokens, 8 Sleep email/password/session cookies, account identifiers, device IDs, exact dates, and exact metric values out of git and out of general logs. Collector reports may contain only aggregate counts, booleans, and coarse freshness buckets; environment values are redacted via the `SECRET_KEYS` markers; every report file is written with mode `0600`.
- Preserve Health Data Hub v1 ingestion scope: **periodic-pull only, no webhooks**; Oura + (optional) 8 Sleep only. No Garmin, Withings, or chest-strap HRM ingestion. No prospective predictions, recommendations, Autopilot, or Coach surfaces.
- Resolve the two pre-committed tripwires explicitly and record the resolution:
  - **Week-1 Oura tripwire:** record the chosen Oura provider path (direct Oura API v2 periodic pull via bearer/OAuth2 token; Open Wearables not adopted for v1 Oura ingestion). The decision-of-record lives in `docs/evidence/ingestion/s03-ingestion-evidence.md`.
  - **Week-2 8 Sleep tripwire:** if `pyEight` does not return `ok` (or a prior fallback is in force), the slice records the `oura_only_v1` fallback as an explicit decision file under `ops/autonomy/decisions/` matching `*pyeight*json` with `"status": "fallback_accepted"` and `"action": "oura_only_v1"`. Under fallback, sleep-source reconciliation collapses to the Oura-only identity — this is a first-class v1 path, not a degraded exception.

## Implementation Tasks

### Oura smoke evidence collector and provider-path decision

- [ ] Provide the Oura sleep smoke collector and its shared redaction/report helper. The collector pulls a 7-day sleep window from the live local Oura API v2 using `OURA_ACCESS_TOKEN`, writes a redacted, `0600`, aggregate-only report under `private/evidence/S03/oura_smoke/`, and returns `status: ok` (exit 0) only when a non-empty `data` list is returned. Record the resolved Oura provider path (direct Oura API v2 periodic pull) as the week-1 tripwire decision in the committed evidence summary. Never log the token or raw payload.
  Files: `scripts/evidence/oura_smoke.py`; `scripts/evidence/_collector_common.py`
  Verify: `python scripts/evidence/oura_smoke.py --json`

### Fail-closed handling for missing Oura credentials

- [ ] Ensure the collector returns `status: blocked_external` and exits non-zero when `OURA_ACCESS_TOKEN` is absent or offline mode is requested, writing only redacted env markers (no secret values). On this path the run appends an **open** failure row `{"slice": "S03", "failure_class": "blocked_external_missing_evidence", "open": true}` to the failure ledger so the readiness gate accepts the controlled-degraded state without a fabricated pass.
  Files: `scripts/evidence/oura_smoke.py`; `ops/autonomy/failure_ledger.jsonl`
  Verify: `python scripts/verify_s03_readiness.py --json`

### 8 Sleep optional smoke and week-2 tripwire fallback

- [ ] Provide the optional 8 Sleep (`pyEight`) smoke collector. It authenticates via the current 8 Sleep token flow using `PYEIGHT_*` env, confirms recent sleep-trend reachability, and writes an aggregate-only, `0600` report under `private/evidence/S03/pyeight_smoke/` emitting only `ok`, `blocked_external`, `error`, or `fallback_accepted` — never raw payloads or identifiers. When `pyEight` is not stable, write the explicit `oura_only_v1` fallback decision file so the week-2 tripwire is recorded, after which the collector reports `fallback_accepted` (exit 0).
  Files: `scripts/evidence/pyeight_smoke.py`; `ops/autonomy/decisions/` (fallback decision file written only when the week-2 tripwire fires)
  Verify: `python scripts/evidence/pyeight_smoke.py --json`

### Committed redacted ingestion evidence summary and command evidence

- [ ] Author the committed, fully redacted ingestion evidence summary that records the provider decision-of-record (Oura path; 8 Sleep included vs `oura_only_v1` fallback) and points at the runtime evidence reports by relative path. Author the machine-checked command-evidence JSON: a single object with a non-empty `commands` list, each row carrying the exact command string, an integer `exit_code` of `0`, and redacted `stdout_tail`/`stderr_tail` containing no secret markers. These are the only S03 artifacts that may be committed to git.
  Files: `docs/evidence/ingestion/s03-ingestion-evidence.md`; `docs/evidence/ingestion/s03-command-evidence.json`
  Verify: `python scripts/check_no_tracked_data.py`

### Autonomous ingestion evidence review

- [ ] Generate the S03 autonomous review artifact. It must state autonomous-reviewer provenance, carry `Verdict: pass` (and no failing verdict), list the evidence files checked, list the exact commands run, include at least one `Command evidence: docs/evidence/ingestion/s03-command-evidence.json` line, and explicitly state `Blocking findings: none`. The review certifies the provider decision and the tripwire resolutions; it must not claim human approval.
  Files: `docs/reviews/s03-autonomous-ingestion-evidence-review.md`
  Verify: `python scripts/check_autonomous_review_exists.py S03`

### Data-hygiene and S03 readiness gate

- [ ] Confirm no health data, tokens, raw payloads, snapshots, or DuckDB/Parquet files are tracked, that `private/evidence/S03/` stays gitignored, and that the full S03 readiness contract holds (S01/S02 complete; Oura evidence present or `blocked_external_missing_evidence` recorded; pyEight `ok` or explicit fallback; review artifact path planned in `slices.json`).
  Files: `docs/evidence/ingestion/s03-ingestion-evidence.md`
  Verify: `python scripts/check_no_tracked_data.py`; `python scripts/verify_s03_readiness.py --json`

## Verification Expectations

The slice is complete only when all S03 acceptance commands pass:

- `python scripts/evidence/oura_smoke.py --json` → exit 0 with `status: ok` (real Oura evidence captured under `private/evidence/S03/oura_smoke/`).
- `python scripts/check_autonomous_review_exists.py S03` → exit 0 (the review artifact satisfies the verdict, evidence-list, commands-list, command-evidence, and no-blockers contract).
- `python scripts/check_no_tracked_data.py` → exit 0 (no health data, secrets, raw payloads, snapshots, or DuckDB/Parquet files tracked).

Supporting gates that must also hold:

- `python scripts/evidence/pyeight_smoke.py --json` → exit 0 with `status: ok` **or** `status: fallback_accepted` (week-2 tripwire resolved either way).
- `python scripts/verify_s03_readiness.py --json` → `status: ok` (readiness preflight, including the gitignore probe on `private/evidence/S03/`).

Autonomous gate substitution: `docs/reviews/s03-autonomous-ingestion-evidence-review.md` must exist, certify the provider decision and tripwire resolutions, and reference the committed command evidence. No `manual_gate` rows may be emitted at any point in the run. If `OURA_ACCESS_TOKEN` is unavailable, the run records an open `blocked_external_missing_evidence` failure and does **not** mark the slice complete — the blocked-external state is surfaced honestly rather than passed.

## Out Of Scope

- S01 warehouse schema and inserts; S02 FastAPI mood endpoint and iOS Shortcut transport.
- S04 sleep-source reconciliation, `merge_sleep_sources()`, `hrv_z` computation, and full feature engineering (the ingestion *pipeline* that writes `sleep_nights`/`daily_features` lives downstream of this decision slice).
- S05 model training, baseline gates, sign-stability bootstrap, and SHAP.
- S06 counterfactual generation; S07 Streamlit UI; S08 launchd schedules, snapshot encryption, and restore.
- Webhook ingestion, Open Wearables adoption for v1 Oura, Garmin / Withings / chest-strap HRM, nutrition logging, Tailscale transport.
- Prospective predictions, recommendations, Autopilot, Coach, or any v2 feature columns and forward-looking guidance.

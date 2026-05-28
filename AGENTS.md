# AGENTS.md — Health Data Hub + AutoKeel

This repository builds **Health Data Hub v1** through the **Keel** toolchain using the **AutoKeel autonomous supervisor**.

The goal is not just to produce code. The goal is to produce a local-first, auditable, zero-human build trail that shows what the autonomous operator did, where it failed, and why each slice was allowed to continue.

## Repository Layout

Important paths:

- `ops/autonomy/autokeel.py` — AutoKeel supervisor.
- `ops/autonomy/policy.yaml` — autonomous operating policy.
- `ops/autonomy/authorization_policy.yaml` — deterministic zero-human authorization criteria for SWR repair, PO repair, and terminal recovery.
- `ops/autonomy/slices.json` — durable slice/task state.
- `ops/autonomy/autonomy_state.json` — runtime state.
- `ops/autonomy/events.jsonl` — append-only event log.
- `ops/autonomy/failure_ledger.jsonl` — append-only failure ledger.
- `ops/autonomy/prompts/` — operator/reviewer/failure-diagnoser prompts.
- `ops/autonomy/schemas/` — policy/state/slice schemas.
- `scripts/` — verification, status, preflight, dashboard, and project checks.
- `scripts/evidence/` — external evidence collectors.
- `docs/reviews/` — autonomous review artifacts.
- `docs/local/` — local-only docs, scratch notes, and non-public review inputs; ignored except for its README.
- `docs/briefs/` — slice briefs.
- `docs/playbooks/` — Keel playbooks.
- `docs/gstack/` — promoted design/autoplan artifacts.
- `private/evidence/` — local sensitive evidence, never committed.
- `data/` — health data, secrets, warehouse, quarantine, snapshots; never committed.

## Absolute Safety Rules

Follow these rules every time:

1. Never call `keel-run mark-manual-gate`.
2. Never represent an AI decision as a human approval.
3. Never clear or simulate a human manual gate.
4. If PO reaches `awaiting_human_gate`, record `manual_gate_leak`, mark the slice for replan, and stop using that playbook.
5. Autonomous gate substitution means: deterministic verification + review artifacts + event/failure logs. It does not mean fake human signoff.
6. External evidence must be real local evidence under `private/evidence/` or sanitized evidence under `docs/evidence/`.
7. Do not fabricate device/API/browser evidence.
8. Do not commit or log raw health data, provider payloads, tokens, `.env`, DuckDB files, snapshots, quarantine payloads, or secrets.
9. Do not weaken Health Data Hub model gates, security gates, mood-first gates, or UI-language gates.
10. Work one slice at a time.

## Product Scope: Health Data Hub v1

v1 is a **Sleep + Mood Retrospective Explainer**.

Required in v1:

- Oura + mood log.
- 8 Sleep only if stable under tripwire.
- Local-first DuckDB storage.
- FastAPI mood endpoint if mood Shortcut path survives tripwire.
- Streamlit retrospective UI.
- No hosted backend.
- No multi-tenant infrastructure.

Out of scope for v1:

- Autopilot recommendations.
- Tomorrow predictions.
- Prospective counterfactuals.
- Coach/LLM chat.
- Garmin, Withings, chest strap, nutrition.
- Medical advice.
- Causal claims.

## Health Data Hub Invariants

Preserve these invariants:

- v1 target is same-day evening `feeling[D]`.
- Sleep features for `feeling[D]` come from sleep ending on morning `D`.
- `prior_day_feeling` is `feeling[D-1]`.
- Model features are exactly:
  - `total_sleep_min`
  - `hrv_z`
  - `deep_sleep_pct`
  - `prior_day_feeling`
- `hrv_avg_ms` is display metadata only.
- `hrv_z` must be prior-only and persisted.
- No sleep forward-fill for training.
- Mood labels are never imputed.
- UI must not show model output for date `D` until `feeling[D]` exists.
- Counterfactuals may vary only mutable/recommendable features.
- In v1, the only counterfactual feature is `total_sleep_min`.
- Sleep counterfactual is increase-only and must respect the safe floor.

## Required UI Language

Use:

- `top model contributors`
- `patterns associated with this rating`
- `model-estimated change in your past data`
- `correlation, not proven causation`
- `insufficient stable signal`
- `collecting model-ready days`

Do not use:

- `drivers`
- `biggest drivers`
- `caused`
- `what made you tired`
- `you should`
- `you would have felt`
- `tomorrow prediction`
- `recommendations today`
- prospective intervention language in v1

## AutoKeel Operating Loop

AutoKeel must preserve Keel as the execution kernel.

Expected loop:

1. Read `ops/autonomy/policy.yaml`.
2. Read `ops/autonomy/slices.json`.
3. Read `ops/autonomy/autonomy_state.json`.
4. Run `scripts/verify_v1.py`.
5. Select the next actionable required slice.
6. Ensure slice brief exists.
7. Ensure autoplan exists or generate/record missing autoplan evidence.
8. Compile playbook with Keel or SWR according to lane.
9. Validate playbook with `scripts/validate_playbook_autonomous.py`.
10. Run real PO `list-items` and `doctor` contract validation before PO.
11. Run PO under supervision.
12. Inspect PO status with `scripts/keel_status_digest.py`.
13. Handle terminal states:
    - `passed` → create ship branch → run `scripts/verify_slice.py` → mark complete only if verification passes.
    - `blocked_external` → collect/request local evidence; do not fabricate evidence.
    - `awaiting_human_gate` → record `manual_gate_leak`; replan under autonomous gate policy.
    - `escalated` → record failure; diagnose; replan.
14. Append `events.jsonl`, `failure_ledger.jsonl`, and `progress.md`.
15. Continue until `scripts/verify_v1.py` passes.

## Commands

Run from repository root.

Preflight:

```bash
python -m ops.autonomy.autokeel --doctor
python -m ops.autonomy.autokeel --doctor --strict
python -m ops.autonomy.autokeel --doctor --strict-swr S05
python scripts/verify_autonomy_preflight.py --json
python scripts/verify_failure_ledger.py --json
python scripts/verify_autokeel_invariants.py --json
python scripts/verify_s03_readiness.py --json
```

One dry-run iteration:

```bash
python -m ops.autonomy.autokeel --once --dry-run
```

One real iteration:

```bash
python -m ops.autonomy.autokeel --once
```

Status:

```bash
python -m ops.autonomy.autokeel --status --failures
python -m ops.autonomy.autokeel --replay-events
```

Tests:

```bash
python -m pytest tests/autonomy -q
python -m pytest -q
```

Project safety checks:

```bash
python scripts/check_no_tracked_data.py
python scripts/verify_failure_ledger.py --json
python scripts/verify_autokeel_invariants.py --json
python scripts/verify_v1.py --json
```

Playbook validation:

```bash
python scripts/validate_playbook_autonomous.py docs/playbooks/s01-warehouse.playbook.md --json
python automation/run_plan_orchestrator.py list-items --playbook docs/playbooks/s01-warehouse.playbook.md --format json
python automation/run_plan_orchestrator.py doctor --playbook docs/playbooks/s01-warehouse.playbook.md --format json
python scripts/validate_swr_review_bundle.py <bundle>.json --json
python scripts/verify_run_retarget_evidence.py docs/evidence/<slice>-run-retarget-<timestamp>.json --json
```

Slice verification:

```bash
python scripts/verify_slice.py S01 --json
python scripts/verify_ship_invariants.py S01 --json
```

Close a failure only with local evidence:

```bash
python -m ops.autonomy.autokeel --close-failure S01 manual_gate_leak \
  --closure-evidence docs/reviews/<evidence>.md \
  --closure-note "Why this failure is now resolved."
```

## Coding Conventions

Use simple Python first.

Prefer:

* small functions
* explicit return dictionaries for scripts
* JSON output with `--json`
* deterministic checks
* no shell=True
* no broad filesystem writes
* no hidden network calls in verification scripts unless the script is explicitly an evidence collector

When changing AutoKeel:

* update tests in `tests/autonomy/`
* update `ops/autonomy/README.md` if operator behavior changes
* update `policy.yaml` if new policy is required
* update `slices.json` only if slice workflow changes
* preserve event and failure logging

## Verification Expectations

A slice is not complete because an agent says it is complete.

A slice is complete only when:

1. PO reaches `passed`.
2. A ship branch exists.
3. `scripts/verify_slice.py <SLICE_ID> --json` passes.
4. Required review artifacts pass validation.
5. No tracked health data or secrets are detected.
6. AutoKeel records the completion in `slices.json`, `autonomy_state.json`, `events.jsonl`, and `progress.md`.

The full project is complete only when:

```bash
python scripts/verify_v1.py --json
```

returns success.

## Failure Handling

When a failure occurs:

1. Classify it.
2. Record it in `ops/autonomy/failure_ledger.jsonl`.
3. Create a Markdown failure artifact under `ops/autonomy/failures/`.
4. Do not silently retry the same failed artifact forever.
5. Do not close the failure without local evidence.
6. Do not continue through manual gates.

Failure classes include:

* `manual_gate_leak`
* `blocked_external_missing_evidence`
* `provider_auth_failure`
* `test_failure`
* `audit_failure`
* `unsafe_write_root`
* `secret_leak_risk`
* `forbidden_ui_language`
* `model_gate_failed`
* `tripwire_triggered`
* `stale_run`
* `agent_false_done`
* `state_divergence`
* `ship_failure`
* `compile_failure`

## External Evidence

Evidence collectors must not fabricate success.

Evidence reports must state one of:

* `ok`
* `blocked_external`
* `error`
* `fallback_accepted`

Evidence must be written under:

```text
private/evidence/
```

or, if sanitized and safe:

```text
docs/evidence/
```

Secrets must be redacted. Evidence files containing sensitive local data should use file mode `0600`.

## Git and Data Rules

Never track:

* `data/`
* `private/`
* `.env`
* `*.duckdb`
* `*.duckdb.wal`
* `*.sqlite`
* `*.parquet`
* `ops/autonomy/.autokeel.lock`
* raw provider payloads
* quarantine payloads
* snapshots
* tokens

Before any ship/complete decision, run:

```bash
python scripts/check_no_tracked_data.py
```

## What Not To Do

Do not:

* bypass Keel by directly building large features outside playbooks
* run multiple active slices in parallel
* approve human gates
* convert missing evidence into success
* weaken statistical gates to make the UI more interesting
* add v2 features to v1 model
* commit runtime lock files
* mark slices complete without `verify_slice.py`
* mark v1 complete without `verify_v1.py`

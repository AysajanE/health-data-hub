# AGENTS.md — Health Data Hub

This repository is **Health Data Hub v1**, a local-first Sleep + Mood Retrospective Explainer. v1 is built and runs every day on the owner's Mac under five launchd agents. [`README.md`](README.md) describes the product and its architecture; this file is the set of rules for anyone, human or agent, changing it.

## How work happens

Since 2026-09-10 the project is built directly on `main`: a change, its tests, and a review before it counts as done.

The AutoKeel slice workflow is retired. Do not:

- run `ops/autonomy/autokeel.py`, Keel compile, SWR, or plan-orchestrator;
- route work through `slices.json`, slice briefs, playbooks, readiness verifiers, activation acceptance, or receipt refreshes;
- edit the historical ledgers (`slices.json`, `autonomy_state.json`, `events.jsonl`, `failure_ledger.jsonl`, `progress.md`) to bring them up to date. The retrain still reads `slices.json`.

See [Historical: AutoKeel control plane](#historical-autokeel-control-plane) for what must stay in place.

## Repository layout

Product:

- `app/mood_form.py` — Streamlit mood form, bound to `LAN_BIND_IP`, token-gated.
- `app/explainer.py` — Streamlit explainer page, bound to `127.0.0.1`.
- `src/config/env_file.py` — reads named keys from `.env.local`.
- `src/ingestion/` — Oura OAuth (`oura_auth.py`) and sync (`oura_sync.py`).
- `src/db/schema.sql` — DuckDB schema (five tables).
- `src/warehouse/` — writes, reads, the shared write lock, daily features, chronological recompute.
- `src/model/` — ridge model, baseline gate, counterfactual generator, display states, eval log.
- `src/backup/snapshot.py` — encrypted snapshots, mirror, verify, restore.
- `src/api/` — `mood_date.py` (the 4 AM cutoff rule, used by both pages) and the FastAPI mood endpoint, which is kept for v1.1 and not served.
- `scripts/` — product entry points: `sync_oura.py`, `oura_authorize.py`, `retrain_model.py`, `nightly_retrain.py`, `run_mood_form.py`, `run_explainer.py`, `backup_snapshot.py`, `restore_snapshot.py`, `install_launchd.py`, `setup_permissions.py`, `check_no_tracked_data.py`, plus `verify_s05_provider_policy.py` (the retrain preflight). Most other scripts belong to AutoKeel.
- `tests/` — product tests; `tests/autonomy/` covers the historical AutoKeel code.

Local only, never committed:

- `data/` — warehouse, `secrets/` (Oura tokens, backup passphrase), quarantine, sync status, restores.
- `models/` — `eval.jsonl` and the nightly model pickles.
- `private/` — sensitive local evidence.
- `.env.local` — settings and Oura client credentials.
- `docs/local/` — scratch notes and non-public review inputs (except its README).

## Absolute safety rules

1. Never commit or log raw health data, provider payloads, tokens, `.env*`, DuckDB files, snapshots, quarantine payloads, or the backup passphrase.
2. Logs (`~/Library/Logs/healthhub*.log`) carry counts and statuses only. Provider and database errors are reported with fixed messages, because exception text can embed rows, URLs, or tokens. The launchd wrappers print redacted summaries, but `scripts/retrain_model.py` run directly prints the latest feature values and rating, and can print raw DuckDB errors: treat its output as health data and keep it out of logs, issues, commits, and review artifacts.
3. Oura tokens and the backup passphrase live only in `data/secrets/` with mode 0600; the Oura client ID and secret live only in `.env.local`. No command prints a token, a client secret, or the passphrase.
4. Private files are 0600 and private directories 0700. Warehouse, token, and snapshot paths refuse symlinks.
5. Every warehouse write goes through `warehouse_write_lock`, and the DuckDB connection is opened only while the lock is held and closed before it is released. Open connections with `connect_duckdb`, which sets the session to UTC.
6. Do not weaken the model gates, the mood-first gate, the UI language rules, or the network bindings (mood form on the LAN address with a token; explainer on `127.0.0.1`).
7. Tests use fake transports and temporary directories. They never use real credentials, the network, or the live `data/` and `models/` directories.
8. Do not touch the live data plane or the running agents unless the owner asks: `data/`, `models/`, `~/Library/LaunchAgents/com.healthhub.*`, and the snapshot folders. Restores go to a fresh directory under `data/restore/` unless `--in-place --force` is explicitly requested.
9. Do not fabricate device, API, or browser evidence, and do not say something works without having run it.

## Product scope: Health Data Hub v1

v1 is a **Sleep + Mood Retrospective Explainer**.

In v1:

- Oura sleep data and a manual evening mood log.
- 8 Sleep inactive: fallback-only under the S03 provider decision.
- Local DuckDB storage.
- Mood logging through the Streamlit form. The FastAPI endpoint is deferred to v1.1.
- A Streamlit retrospective page.
- Encrypted local snapshots with a best-effort iCloud Drive mirror.
- No hosted backend. No multi-tenant infrastructure.

Out of scope for v1:

- Autopilot recommendations.
- Tomorrow predictions.
- Prospective counterfactuals.
- Coach/LLM chat.
- Garmin, Withings, chest strap, nutrition.
- Medical advice.
- Causal claims.

Oura data use: an S12 `provider_terms_conflict` entry about the Oura API agreement is still open in the failure ledger. Do not widen how Oura data is used (new collections, new destinations, sharing, or hosted processing) without the owner's decision.

## Health Data Hub invariants

Model and features:

- The v1 target is same-day evening `feeling[D]`.
- Sleep features for `feeling[D]` come from sleep ending on the morning of `D`.
- `prior_day_feeling` is `feeling[D-1]`.
- Model features are exactly:
  - `total_sleep_min`
  - `hrv_z`
  - `deep_sleep_pct`
  - `prior_day_feeling`
- `hrv_avg_ms` is display metadata only.
- `hrv_z` is prior-only (never includes day `D`) and persisted.
- No sleep forward-fill for training.
- Mood labels are never imputed. Rows with an imputed `prior_day_feeling` are excluded from training.
- The model is shown only after 37 model-ready days and only when the walk-forward baseline gate passes. Contributors with sign stability below 80% are hidden.
- The UI must not show model output for date `D` until `feeling[D]` exists.

Counterfactual:

- Counterfactuals may vary only mutable, recommendable features. In v1 that is only `total_sleep_min`.
- The sleep counterfactual is increase-only and never goes below the 7-hour safe floor.
- It is reported only when the bootstrapped delta interval excludes zero and the median change is at least 0.5 points; otherwise it records a suppression reason.

Data and time:

- A rating saved before 04:00 local time counts for the previous day (`src/api/mood_date.py`).
- Mood history is append-only. A correction inserts a new `mood_entries` row that links to the one it replaces, and `mood_current` points to the live entry.
- An Oura night's `sleep_date` is its wake date in `HOME_TIMEZONE`. Only the longest `long_sleep` record per wake date is kept.
- Oura's `end_date` query parameter is exclusive, so the sync requests through `end_date + 1`.
- After writing, the sync recomputes daily features in date order over at least the last 45 days.
- Stored timestamps are naive UTC.

8 Sleep: feature construction ignores 8 Sleep rows even if they exist in the warehouse. Diagnostics may record that they were present and ignored. 8 Sleep must not be averaged, blended, reconciled, used as fallback HRV or sleep-stage source, or counted as an active sleep source unless a future explicit provider decision supersedes S03.

## Required UI language

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

Tests in `tests/ui/` and `tests/model/test_display_gate.py` enforce these rules.

## Commands

Run from the repository root.

Tests and checks (safe at any time):

```bash
.venv/bin/python -m pytest -q
.venv/bin/python -m pytest -q --ignore=tests/autonomy
.venv/bin/python scripts/check_no_tracked_data.py
.venv/bin/python scripts/run_explainer.py --check
.venv/bin/python scripts/install_launchd.py --status
.venv/bin/python scripts/install_launchd.py --dry-run
.venv/bin/python scripts/restore_snapshot.py --snapshot latest --verify-only
```

These write to the live data plane or the running agents. Run them only when the owner asks. `run_mood_form.py --check` is here because it resets the permissions on `data/`, `models/`, and `private/` before it checks the settings.

```bash
.venv/bin/python scripts/run_mood_form.py --check
.venv/bin/python scripts/sync_oura.py --json
.venv/bin/python scripts/nightly_retrain.py
.venv/bin/python scripts/backup_snapshot.py --json
.venv/bin/python scripts/install_launchd.py --install
.venv/bin/python scripts/oura_authorize.py
```

## Coding conventions

Use simple Python first.

Prefer:

- small functions
- explicit return dictionaries for scripts
- JSON output with `--json`
- deterministic checks
- injected transports, clocks, and paths, so tests never need the network or live data
- no `shell=True`
- no broad filesystem writes
- no network calls outside `src/ingestion/`

When changing product code:

- add or update the tests next to it (`tests/warehouse/`, `tests/ingestion/`, `tests/model/`, `tests/ui/`, `tests/backup/`, or `tests/test_*.py`);
- keep the full suite passing;
- update `README.md` when user-visible behavior, settings, or schedules change.

## Git and data rules

Never track:

- `data/`
- `models/`
- `private/`
- `.env*`
- `*.duckdb`
- `*.duckdb.wal`
- `*.sqlite`
- `*.parquet`
- raw provider payloads
- quarantine payloads
- snapshots
- tokens

Treat optional 8 Sleep credential names as sensitive if they are present: `PYEIGHT_EMAIL`, `PYEIGHT_PASSWORD`, `PYEIGHT_CLIENT_ID`, `PYEIGHT_CLIENT_SECRET`, `EIGHT_SLEEP_TOKEN`, and `EIGHT_SLEEP_PASSWORD`. Their absence must not fail v1.

Before committing, run:

```bash
.venv/bin/python scripts/check_no_tracked_data.py
```

## Historical: AutoKeel control plane

From May to August 2026 an autonomous supervisor, AutoKeel (`ops/autonomy/autokeel.py`), drove the Keel toolchain slice by slice and completed S01–S05. S11, S12, S06, S07, and S08 were later built directly on `main`. S09's code-level tests are on `main`, but its week-16 evaluation from the design doc has not run; that is the remaining v1 work. There is no S10. The control plane is kept as a record and is not run.

Three pieces are still on the live path and must stay as they are:

- `ops/autonomy/decisions/`: `load_sleep_provider_policy` in `src/warehouse/features.py` reads the active S03 decision from it. The Oura sync and the retrain fail if it is missing or conflicting.
- `scripts/verify_s05_provider_policy.py`: `scripts/retrain_model.py` runs it as a preflight before every retrain and imports its checks.
- `ops/autonomy/slices.json`: that preflight requires S03 and S04 to show `complete`. Removing or rewriting the file stops the nightly retrain.

Everything else is a record:

- `ops/autonomy/` (policy, state, events, failure ledger, failure write-ups, prompts, schemas), `docs/briefs/`, `docs/playbooks/`, `docs/reviews/`, `docs/evidence/`, `docs/gstack/`, and the other AutoKeel scripts in `scripts/` (the `verify_*`, `swr_*`, `autokeel_*`, slice, lane, tripwire, and evidence scripts) are historical. Do not edit or delete them unless the owner asks.
- `slices.json` still lists S06–S12 as pending. That is expected; the code on `main` is the truth.
- `tests/autonomy/` still runs in the full suite. Some of its tests intermittently fail while deleting a temporary git repository (`OSError: [Errno 66] Directory not empty`); a different test fails each time and a rerun passes. Any other failure there after a product change: report it and ask the owner before changing or deleting the historical test.
- The one open failure-ledger entry, S12 `provider_terms_conflict`, is the owner's to resolve. Do not close it or edit it.

`ops/autonomy/README.md` and `docs/keel-walkthrough_v1.html` describe how the supervisor worked.

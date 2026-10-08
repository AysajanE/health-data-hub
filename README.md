# Health Data Hub

> **Your sleep, your mood, your model — all on your Mac.**
> A personal health app that learns which patterns in your past nights *correlate* with how you actually felt.

You wear an Oura Ring. Maybe an 8 Sleep cover. You feel different from day to day and you wish *something* could tell you which of last night's numbers usually tracks with how you feel. Vendor apps can't: Oura sees only Oura, 8 Sleep sees only 8 Sleep, and none of them know how you actually rated the day.

Health Data Hub fuses those signals with a one-tap evening mood log and gives you a single, honest explainer card on your own machine. No hosted backend. No account. No coach trying to sell you anything.

```text
┌──────────────────────────────────────────────────────────────────┐
│  Yesterday — Mon May 25                              you: 4 / 10 │
│                                                                  │
│  Top model contributors (patterns associated with this rating)   │
│    • Total sleep ........ 6h 12m   ▼ below your usual 7h 30m     │
│    • HRV (z-score) ...... −0.9     ▼ below your prior baseline   │
│    • Yesterday's feeling . 6 / 10                                │
│                                                                  │
│  Model-estimated change in your past data:                       │
│    Holding the other inputs fixed, a total sleep nearer your     │
│    usual upper range (7h 30m) was associated with a +0.6 to      │
│    +1.2 point higher rating.                                     │
│                                                                  │
│  Confidence: medium · correlation, not proven causation          │
└──────────────────────────────────────────────────────────────────┘
```

That card is the product. Everything else in this repo exists to render it honestly.

## Status

v1 is built and runs every day on an always-on Mac: the Oura sync, the evening mood form, the nightly retrain with its baseline gate and sleep counterfactual, the explainer page, and encrypted backups, all scheduled by launchd.

What v1 still needs is time. The model says nothing until it has 37 model-ready days, and then it speaks only on nights when it beats two simple baselines. The week-16 evaluation described in the design doc has not been run yet.

The first five slices were built by AutoKeel, an experiment in fully autonomous development. Since 2026-09-10 the project is built directly on `main` with ordinary tests and code review. See [History: AutoKeel](#history-autokeel).

## How it works

```text
Oura Ring ──▶ Oura API v2
                  │   scripts/sync_oura.py, 08:00 and 19:30
                  ▼
Phone ──▶ mood form ──▶ DuckDB warehouse ──▶ daily features
          :8501, home Wi-Fi                      │
                                                 ▼   scripts/nightly_retrain.py, 23:00
                        ridge model ──▶ baseline gate ──▶ sleep counterfactual
                                                 │
                                                 ▼
                                         models/eval.jsonl
                                                 │
                                                 ▼
                                explainer page :8502, this Mac only

23:30: warehouse + tokens + eval log ──▶ encrypted snapshot in
       ~/Library/Application Support/HealthDataHub ──▶ best-effort copy to iCloud Drive
```

| Part | Code | What it does |
|---|---|---|
| Oura ingestion | [`src/ingestion/`](src/ingestion/), [`scripts/sync_oura.py`](scripts/sync_oura.py), [`scripts/oura_authorize.py`](scripts/oura_authorize.py) | Signs in with OAuth and rotates tokens on every refresh. Keeps one main sleep per night, dated by the morning you woke up in your home timezone. Never writes Oura's raw responses to disk. |
| Warehouse | [`src/db/schema.sql`](src/db/schema.sql), [`src/warehouse/`](src/warehouse/) | Five DuckDB tables behind one shared write lock. Mood corrections add a new row instead of overwriting. Invalid rows are set aside in a private quarantine folder. Features use only past data: the HRV z-score compares a night with the nights before it, never with itself. |
| Model | [`src/model/`](src/model/), [`scripts/retrain_model.py`](scripts/retrain_model.py) | Ridge regression on four inputs: total sleep, HRV z-score, deep sleep %, and yesterday's rating. Checks that each input's direction holds across 200 resamples, runs the baseline gate and the counterfactual, then appends one record to `models/eval.jsonl`. |
| Pages | [`app/mood_form.py`](app/mood_form.py), [`app/explainer.py`](app/explainer.py) | Streamlit. The mood form listens on the home Wi-Fi address and asks for a token. The explainer listens on `127.0.0.1` only. |
| Backups | [`src/backup/snapshot.py`](src/backup/snapshot.py), [`scripts/backup_snapshot.py`](scripts/backup_snapshot.py), [`scripts/restore_snapshot.py`](scripts/restore_snapshot.py) | Encrypted, fingerprinted snapshots of the data plane, with a verified restore path. |
| Scheduling | [`scripts/install_launchd.py`](scripts/install_launchd.py) | Six per-user launchd agents. |

- **Local-first.** A DuckDB file on your Mac. No hosted backend. No SaaS. Your data stays on your Mac and your home network; the only copy that goes further is an encrypted snapshot in your own iCloud Drive.
- **You own the model.** It learns your baseline from your own data. Nobody else's.
- **Honest by design.** Until there are 37 model-ready days, the page shows *"Collecting model-ready days"*. After that, the model is shown only when, on days it hasn't seen, it beats two simple guesses: yesterday's rating and the 7-day average. Any input whose direction isn't consistent across resamples is hidden.
- **Words chosen carefully.** `top model contributors`, `patterns associated with this rating`, `correlation, not proven causation`. Never `drivers`, `caused`, `you should`, `tomorrow prediction`. Tests in `tests/ui/` and `tests/model/` enforce this. Hyper-health-conscious users deserve to not be nocebo'd by their own app.
- **Oura only.** 8 Sleep is inactive in v1. Its rows, if any, are recorded as diagnostics and never used as model inputs.

## What this is not

- **Not medical advice.** v1 explains correlations in *your* past data. It does not predict your future, recommend interventions, or make any clinical claim.
- **Not a hosted service.** Everything runs on your Mac, against your data, with your credentials on your filesystem.
- **Not multi-tenant.** Single user, single device, single dataset by design.

If you wanted a coach in your pocket, that's not this. The Autopilot tier (action features, N-of-1 experiments, prospective recommendations) lives in the v2+ vision — explicitly out of scope here because at this data scale, prospective recommendations are exactly where false precision and nocebo loops do the most damage.

## Setup

You need macOS, Python 3.12+, an Oura account, and an Oura API application (client ID and secret).

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Settings live in `.env.local`, which is never committed. Each script reads only the keys it needs.

| Key | Used for |
|---|---|
| `OURA_CLIENT_ID`, `OURA_CLIENT_SECRET` | Your Oura API application |
| `OURA_REDIRECT_URI` | Optional. Defaults to `http://localhost:8765/oauth/oura/callback` |
| `LAN_BIND_IP` | The Mac's home Wi-Fi address, where the mood form listens |
| `MOOD_FORM_TOKEN` | Any private string. The mood form asks for it, and so does the explainer when you log a rating there |
| `HOME_TIMEZONE` | For example `America/Toronto`. Decides which day a night or a rating belongs to |
| `HEALTH_HUB_DATABASE_PATH`, `HEALTH_HUB_MODEL_DIR` | Leave these out of `.env.local`. The Oura sync and the pages would follow them, but the scheduled retrain refuses a non-default database and the backup always snapshots `data/warehouse.duckdb`. They exist for viewing a restored copy from one hand-started explainer (see [Backups and restore](#backups-and-restore)) |

First run, in order:

```bash
.venv/bin/python scripts/oura_authorize.py                # once, in a browser
.venv/bin/python scripts/sync_oura.py --start 2026-01-01  # one-time history backfill
.venv/bin/python scripts/backup_snapshot.py --init-key    # once; copy the passphrase somewhere safe
.venv/bin/python scripts/install_launchd.py --install     # start the six agents
```

## Log your mood (every evening)

The mood log is the only part of the product that needs you every day. It is a small Streamlit page served only on your home Wi-Fi, and it writes straight into the local DuckDB warehouse.

1. Set `LAN_BIND_IP`, `MOOD_FORM_TOKEN`, and `HOME_TIMEZONE` in `.env.local`.
2. The launchd agent keeps the form running. To run it by hand instead:

   ```bash
   .venv/bin/python scripts/run_mood_form.py
   ```

3. On the phone, on the same Wi-Fi, open `http://<LAN_BIND_IP>:8501`, enter the token once, pick a number from 1 to 10 for *"How did I feel overall today?"*, and tap **Save**. Energy, context, and notes are optional.
4. Log in the evening. Anything saved between midnight and 4:00 AM counts for the day that just ended. Saving twice for the same day replaces the rating and keeps the earlier one in history.

A day is model-ready when it has your rating, the previous evening's rating, and a full Oura night. One missed evening therefore costs up to two model-ready days.

`.venv/bin/python scripts/run_mood_form.py --check` validates the settings without starting the server.

### Evening reminder dialog

So the log does not depend on remembering the form, the `com.healthhub.mood-prompt` agent runs [`scripts/mood_prompt.py`](scripts/mood_prompt.py) at 18:20 and 21:30. If today already has a rating it exits silently. Otherwise it plays a sound and shows a native dialog: type the feeling and energy scores (`7 6`, `7,6`, or `7/6`), optionally followed by a short note, and press **Log**. The rating is saved through [`scripts/log_mood.py`](scripts/log_mood.py), the same locked write path, and a notification confirms the date and the model-ready count. **Later** or no answer within 30 minutes closes it; the 21:30 run asks again if the day is still missing. If the Mac is asleep at the scheduled time, launchd runs it on wake.

```bash
.venv/bin/python scripts/mood_prompt.py          # ask now if today is not logged
.venv/bin/python scripts/log_mood.py --status    # today's logging state, read-only
```

## Oura sync

The Oura sync pulls your own sleep records from the Oura API v2, keeps only the main sleep episode per wake date, and writes normalized rows into the warehouse. Raw responses are never written to disk. Tokens live only in `data/secrets/oura_tokens.json` (mode 0600) and rotate on every refresh. After each sync, daily features are rebuilt in date order over the last 45 days, because Oura sometimes re-delivers older nights.

```bash
.venv/bin/python scripts/sync_oura.py --json            # last 14 days, recompute features
.venv/bin/python scripts/sync_oura.py --start 2026-01-01 # one-time history backfill
```

If the sync exits with `auth_required`, the refresh token is dead. Re-authorize once in a browser:

```bash
.venv/bin/python scripts/oura_authorize.py
```

## The explainer page

The retrospective card lives on a second Streamlit page that binds to `127.0.0.1` only, so it is readable on the Mac and nowhere else. Open `http://127.0.0.1:8502`. If today's rating is missing, the page shows the mood form first and hides model output for today until you rate it. Until 37 model-ready days exist it shows "Collecting model-ready days", and after that it only shows contributors on nights when the model beats the simple baselines. It warns when the newest Oura night is more than 36 hours old.

The card's "model-estimated change in your past data" line comes from the retrospective counterfactual generator in `src/model/counterfactual.py`, run by the nightly retrain. It varies exactly one feature, total sleep, increase-only and never below 7 hours, only among candidates that resemble nights you have actually had, and only reports a bootstrapped delta interval that excludes zero and a median change of at least half a rating point. Otherwise the card says why it stayed quiet. It is a description of association in your own past data, never a prediction or a recommendation.

```bash
.venv/bin/python scripts/run_explainer.py
```

## Backups and restore

Every night at 23:30 the data plane (the DuckDB warehouse after a checkpoint, the Oura token file, the sync status, and the model evaluation log) is packed with a names-and-digests manifest, encrypted with the system OpenSSL (AES-256, PBKDF2) under a private passphrase, and written to `~/Library/Application Support/HealthDataHub/snapshots/`. It is then mirrored to iCloud Drive under `HealthDataHub/snapshots/` as a best-effort second copy. The newest 30 snapshots are kept in both places. Quarantine payloads and `.env.local` are excluded by default.

macOS privacy protection blocks background jobs from iCloud Drive until the program is granted access. Until then the nightly report shows `mirror=error` and a notification says the mirror failed, while the local snapshot is still taken. To allow the mirror: System Settings, Privacy & Security, Full Disk Access, add the Python interpreter the agents run (`readlink -f .venv/bin/python` prints its path; press Cmd+Shift+G in the file picker to type it). Running the backup by hand from a terminal that already has iCloud access mirrors fine.

```bash
.venv/bin/python scripts/backup_snapshot.py --init-key           # once; then copy data/secrets/backup_passphrase into your password manager
.venv/bin/python scripts/backup_snapshot.py --json               # take a snapshot now
.venv/bin/python scripts/restore_snapshot.py --snapshot latest --verify-only
```

The passphrase is never printed by any command. Without a copy of it outside this Mac, the snapshots cannot be opened after a disk loss.

Restore never touches the live data by default. It decrypts the newest snapshot into a fresh directory under `data/restore/`, verifies every digest, and prints aggregate counts. To look at a restored copy, start a second explainer on another port with the overrides set for that one process only. Don't put them in `.env.local`, where the scheduled sync would start writing into the restored copy and the retrain would stop:

```bash
HEALTH_HUB_DATABASE_PATH="$PWD/data/restore/<restore-dir>/data/warehouse.duckdb" \
HEALTH_HUB_MODEL_DIR="$PWD/data/restore/<restore-dir>/models" \
  .venv/bin/python scripts/run_explainer.py --port 8503
```

Don't log a rating on that page. If the restored copy has no rating for today, the page offers the mood form, and a save there goes into the restored copy, not your live data.

Replacing the live data plane requires `--in-place --force`, which first moves the current files aside.

## Scheduling

Six per-user launchd agents keep everything running on an always-on Mac: the mood form and the explainer page (kept alive), the Oura sync at 08:00 and 19:30, the mood reminder dialog at 18:20 and 21:30, the model retrain at 23:00 after the evening log, and the encrypted backup at 23:30.

```bash
.venv/bin/python scripts/install_launchd.py --install
.venv/bin/python scripts/install_launchd.py --status
.venv/bin/python scripts/install_launchd.py --install --only com.healthhub.mood-prompt   # one agent, others untouched
```

Logs go to `~/Library/Logs/healthhub-*.log` and contain no tokens or health values.

## Development

Work lands directly on `main`. Every change comes with tests, and changes are reviewed before they count as done.

```bash
.venv/bin/python -m pytest -q                            # full suite
.venv/bin/python -m pytest -q --ignore=tests/autonomy    # product tests only
.venv/bin/python scripts/check_no_tracked_data.py        # no health data or secrets tracked
```

Tests use fake network transports and temporary directories, and never need real Oura credentials.

## Non-negotiables

- No raw health data, tokens, DuckDB files, snapshots, quarantine payloads, or provider payloads are tracked. `data/`, `models/`, `private/`, `.env*`, `*.duckdb`, `*.sqlite`, and `*.parquet` are gitignored.
- Logs carry counts and statuses, never health values or tokens.
- The model gates, the mood-first rule, and the UI language rules are never weakened to make the page more interesting.
- Required UI language (`patterns associated with this rating`, `correlation, not proven causation`, `insufficient stable signal`) is used; causal language (`drivers`, `caused`, `tomorrow prediction`, `you would have felt`) is rejected by tests.

The full rules for anyone, human or agent, changing this code are in [`AGENTS.md`](AGENTS.md).

## Repository layout

```text
health-data-hub/
├── app/                  Streamlit pages: mood_form.py (home Wi-Fi), explainer.py (this Mac only)
├── src/
│   ├── config/           .env.local reader
│   ├── ingestion/        Oura OAuth and sync
│   ├── db/schema.sql     DuckDB schema
│   ├── warehouse/        writes, reads, write lock, daily features
│   ├── model/            ridge model, baseline gate, counterfactual, display states, eval log
│   ├── backup/           encrypted snapshots and restore
│   └── api/              mood-date rule; the FastAPI mood endpoint is kept but not served
├── scripts/              product entry points, plus the historical AutoKeel verifiers
├── tests/                product tests; tests/autonomy/ covers the historical AutoKeel code
├── ops/autonomy/         historical AutoKeel state and logs (decisions/ and slices.json are still read at runtime)
├── docs/                 design doc, legal, and the AutoKeel-era briefs, playbooks, reviews, evidence
├── data/ models/ private/   warehouse, secrets, model output, sensitive evidence (never committed)
├── AGENTS.md             rules for anyone changing this code
└── CLAUDE.md             Claude Code project memory
```

## History: AutoKeel

From May to August 2026 this repo was also an experiment: could an autonomous supervisor build a real product end to end without lying about it? AutoKeel drove the [Keel](https://github.com/AysajanE/keel) toolchain one slice at a time. It never approved a human gate, marked a slice complete only when `scripts/verify_slice.py` passed, and wrote every decision and failure to files instead of chat memory.

It completed S01–S05. On 2026-09-10 the project switched to building directly on `main`, and the rest of v1 was built that way:

| Slice | What it covers | Built by | Code |
|---|---|---|---|
| S01 | Warehouse foundation | AutoKeel | `src/db/`, `src/warehouse/` |
| S02 | Mood API | AutoKeel | `src/api/` (not served; the S11 form replaced it) |
| S03 | Ingestion provider decision: Oura only | AutoKeel | `ops/autonomy/decisions/` |
| S04 | Feature engineering | AutoKeel | `src/warehouse/features.py` |
| S05 | Model lifecycle and gates | AutoKeel | `src/model/ridge.py`, `src/model/baseline_gate.py` |
| S11 | Mood logging | directly on `main` | `app/mood_form.py` |
| S12 | Oura production sync | directly on `main` | `src/ingestion/` |
| S06 | Counterfactual generator | directly on `main` | `src/model/counterfactual.py` |
| S07 | Explainer page | directly on `main` | `app/explainer.py` |
| S08 | Backups, restore, launchd | directly on `main` | `src/backup/`, `scripts/install_launchd.py` |
| S09 | Testing and v1 evaluation | tests on `main`; week-16 evaluation not yet run | `tests/` |

The record stays in the repo, unchanged:

- `ops/autonomy/events.jsonl`: 1,550 events.
- `ops/autonomy/failure_ledger.jsonl`: 85 classified failures, with a write-up for each in `ops/autonomy/failures/`. 84 were closed with local evidence. One is still open: an S12 `provider_terms_conflict` note from 2026-08-16 about whether the Oura API agreement allows API data to feed a local model.
- `ops/autonomy/slices.json` and `autonomy_state.json` were not updated after the switch. They still show S06–S12 as pending; the code on `main` is the truth. `slices.json` must stay anyway: the nightly retrain's provider-policy preflight checks that S03 and S04 are complete.

The supervisor itself, `ops/autonomy/autokeel.py`, is no longer run. Its tests remain in the full suite. For a click-through tour of how Keel and AutoKeel built a slice, open [`docs/keel-walkthrough_v1.html`](docs/keel-walkthrough_v1.html) in a browser; the operator notes are in [`ops/autonomy/README.md`](ops/autonomy/README.md).

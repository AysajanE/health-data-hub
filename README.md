# Health Data Hub

> **Your sleep, your mood, your model — all on your Mac.**
> A personal health app that learns which patterns in your past nights *correlate* with how you actually felt — built end to end by an autonomous agent that keeps a truthful audit trail of its own work.

You wear an Oura Ring. Maybe an 8 Sleep cover. You feel different from day to day and you wish *something* could tell you which of last night's numbers usually tracks with how you feel. Vendor apps can't: Oura sees only Oura, 8 Sleep sees only 8 Sleep, and none of them know how you actually rated the day.

Health Data Hub fuses those signals with a one-tap evening mood log and gives you a single, honest explainer card on your own machine. No cloud. No account. No coach trying to sell you anything.

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

## Two things in one repo

1. **The product — Health Data Hub v1.** A local-first **Sleep + Mood Retrospective Explainer**. Oura → DuckDB on your Mac → a small model → a Streamlit page that explains yesterday. 8 Sleep is fallback-only and inactive in the current v1 provider path. Nothing leaves your machine.
2. **The experiment — AutoKeel.** A zero-human supervisor that drives the [Keel](https://github.com/AysajanE/keel) toolchain to build the product, slice by slice, and writes a truthful audit trail of where it succeeded, where it failed, and why. AutoKeel never approves a human gate. Manual gates are substituted with deterministic verification + review artifacts, not faked.

You can run the app without caring about AutoKeel. You can study AutoKeel without using the app. Most readers care about one of these; pick yours.

## The product, at a glance

```text
       Your devices                  Your Mac (everything below stays local)
   ┌────────────────┐         ┌─────────────────────────────────────────────┐
   │  Oura Ring     │ ──┐     │  Ingestion → Warehouse → Features → Model   │
   │  8 Sleep       │ ──┼──▶  │  (launchd 8am)   (DuckDB)   (Ridge + SHAP)  │
   └────────────────┘   │     │                                       │     │
                        │     │                                       ▼     │
   ┌────────────────┐   │     │              FastAPI (token-gated, LAN)     │
   │  iPhone        │ ──┘     │                       │                     │
   │  one-tap mood  │         │                       ▼                     │
   │  iOS Shortcut  │ ──────▶ │              Streamlit explainer card       │
   └────────────────┘         │              localhost:8501                 │
                              └─────────────────────────────────────────────┘
                              encrypted snapshots → iCloud Drive backups
```

The diagram is the target shape of the original Shortcut branch. The active
tripwire recovery uses a tiny Streamlit mobile form and defers FastAPI to v1.1;
8 Sleep remains inactive. S12 owns the still-unbuilt production Oura sync for
this private, single-user personal tool.

- **Local-first.** DuckDB file on your laptop. No hosted backend. No SaaS.
- **You own the model.** It learns your baseline from your own data. Nobody else's.
- **Honest by design.** Until the model beats two trivial baselines on walk-forward evaluation, the UI shows *"Collecting model-ready days"* — not made-up insights.
- **Words chosen carefully.** `top model contributors`, `patterns associated with this rating`, `correlation, not proven causation`. Never `drivers`, `caused`, `you should`, `tomorrow prediction`. Hyper-health-conscious users deserve to not be nocebo'd by their own app.

## What's in v1, and where we are

v1 currently has eleven required slices: the original S01–S09, the S11 mood-logging recovery inserted after the real transport tripwire fired, and S12 for the previously unowned production Oura sync. The slice ledger (`ops/autonomy/slices.json`) is the truth; this table is a snapshot. “Recorded complete” means its code/control-plane slice passed its historical gate, not that the full product is operational on real data.

| | Slice | What you get when it ships | Status |
|---|---|---|---|
| ✅ | **S01** Warehouse foundation | DuckDB schema, validated ingestion, quarantine for bad payloads | recorded complete |
| ✅ | **S02** Mood API loop | Tested FastAPI mood validation/persistence surface; real Shortcut/LAN activation was not proven | recorded complete |
| ✅ | **S03** Ingestion provider decision | Oura-only v1 decision and provider evidence; no production Oura-to-warehouse sync yet | recorded complete |
| ✅ | **S04** Feature engineering | Daily features your model trains on (`total_sleep_min`, `hrv_z`, `deep_sleep_pct`, `prior_day_feeling`) | recorded complete; continuation lineage requires canonical reconciliation |
| ✅ | **S05** Model lifecycle + gates | The model — but only allowed to speak after it beats baselines | recorded complete |
| ☐ | **S11** Tripwire recovery | Tiny authenticated Streamlit form, real mobile/LAN persistence, typed compliance evidence, and an independently verified collecting-state guard | next required slice |
| ☐ | **S12** Oura production sync | Private OAuth, wake-date/DST mapping, locked warehouse sync, chronological recompute, and aggregate attestation | pending behind S11 |
| ☐ | **S06** Counterfactual generator | The "a sleep duration nearer your usual upper range was associated with…" line | pending |
| ☐ | **S07** Read API + Streamlit UI | The explainer card you saw at the top, rendered against your data | pending |
| ☐ | **S08** Backups + restore | launchd-scheduled encrypted snapshots to iCloud, verified restore path | pending |
| ☐ | **S09** Testing + v1 evaluation | The end-to-end gate that says v1 is real | pending |

## What this is not

- **Not medical advice.** v1 explains correlations in *your* past data. It does not predict your future, recommend interventions, or make any clinical claim.
- **Not a hosted service.** Everything runs on your Mac, against your data, with your credentials on your filesystem.
- **Not multi-tenant.** Single user, single device, single dataset by design.
- **Not finished.** S01–S05 are recorded complete. Before S11 may spend on compilation, the control plane still needs a trusted outer activation validator and enforceable isolation from repo-local secrets; S11 then needs real mobile evidence. S12 is ordinary unfinished product work and follows S11 in dependency order.

If you wanted a coach in your pocket, that's not this. The Autopilot tier (action features, N-of-1 experiments, prospective recommendations) lives in the v2+ vision — explicitly out of scope here because at this data scale, prospective recommendations are exactly where false precision and nocebo loops do the most damage.

## Start here

**If you just want to understand the system** — open [`docs/keel-walkthrough_v1.html`](docs/keel-walkthrough_v1.html) in a browser. It's the click-through tour of how Keel + AutoKeel build a real feature end to end.

**If you want to run the product on your own data** — daily mood logging works today (see [Log your mood](#log-your-mood-every-evening) below). The production Oura sync, the explainer page, and backups are being built directly on `main`.

## Log your mood (every evening)

The mood log is the only part of the product that needs you every day. It is a small Streamlit page served only on your home Wi-Fi, and it writes straight into the local DuckDB warehouse.

1. Put `LAN_BIND_IP` (the Mac's home Wi-Fi address), `MOOD_FORM_TOKEN` (any private string), and `HOME_TIMEZONE` in `.env.local`. That file is never committed.
2. Start the form on the Mac and leave it running:

   ```bash
   .venv/bin/python scripts/run_mood_form.py
   ```

3. On the phone, on the same Wi-Fi, open `http://<LAN_BIND_IP>:8501`, enter the token once, pick a number from 1 to 10 for *"How did I feel overall today?"*, and tap **Save**. Energy, context, and notes are optional.
4. Log in the evening. Anything saved between midnight and 4:00 AM counts for the day that just ended. Saving twice for the same day replaces the rating and keeps the earlier one in history.

`.venv/bin/python scripts/run_mood_form.py --check` validates the settings without starting the server.

## Oura sync and scheduling

The Oura sync pulls your own sleep records from the Oura API v2, keeps only the main sleep episode per wake date, and writes normalized rows into the warehouse. Raw responses are never written to disk. Tokens live only in `data/secrets/oura_tokens.json` (mode 0600) and rotate on every refresh.

```bash
.venv/bin/python scripts/sync_oura.py --json            # last 14 days, recompute features
.venv/bin/python scripts/sync_oura.py --start 2026-01-01 # one-time history backfill
```

If the sync exits with `auth_required`, the refresh token is dead. Re-authorize once in a browser:

```bash
.venv/bin/python scripts/oura_authorize.py
```

## The explainer page

The retrospective card lives on a second Streamlit page that binds to `127.0.0.1` only, so it is readable on the Mac and nowhere else. Open `http://127.0.0.1:8502`. If today's rating is missing, the page shows the mood form first and hides model output for today until you rate it. Until 37 model-ready days exist it shows "Collecting model-ready days", and after that it only shows contributors on nights when the model beats the simple baselines.

The card's "model-estimated change in your past data" line comes from the retrospective counterfactual generator in `src/model/counterfactual.py`, run by the nightly retrain. It varies exactly one feature, total sleep, increase-only and never below 7 hours, only among candidates that resemble nights you have actually had, and only reports a bootstrapped delta interval that excludes zero and a median change of at least half a rating point. Otherwise the card says why it stayed quiet. It is a description of association in your own past data, never a prediction or a recommendation.

```bash
.venv/bin/python scripts/run_explainer.py
```

Four per-user launchd agents keep everything running on an always-on Mac: the mood form and the explainer page (kept alive), the Oura sync at 08:00 and 19:30, and the model retrain at 23:00 after the evening log.

```bash
.venv/bin/python scripts/install_launchd.py --install
.venv/bin/python scripts/install_launchd.py --status
```

Logs go to `~/Library/Logs/healthhub-*.log` and contain no tokens or health values.

**If you want to study the autonomous build** — read on.

## The honest-AI-build experiment

If you've tried running coding agents autonomously, you've seen the same failure: agents declare victory. They auto-approve gates that were meant for a human. They mark work done without verification. They fabricate evidence when reality doesn't cooperate. The audit trail you wanted as proof of safety becomes proof that the experiment was lying.

AutoKeel addresses each one with structural rules, not prompts.

- **Never simulate a human gate.** Reaching `awaiting_human_gate` is recorded as a `manual_gate_leak` failure. The slice is replanned, not approved. AutoKeel does not call `keel-run mark-manual-gate`. Ever.
- **Verification is the source of truth.** A slice is complete only when `scripts/verify_slice.py <SLICE_ID> --json` passes. Self-reports do not count.
- **Evidence is real or it is absent.** It's collected from local sources, written under `private/evidence/`, or sanitized into `docs/evidence/`. Fabricating evidence is its own failure class.
- **State lives outside the agent.** `slices.json`, `autonomy_state.json`, `events.jsonl`, `failure_ledger.jsonl`. Every decision is replay-able from files alone — never from chat memory.

Inside that scaffolding, Keel's safety boundaries are unchanged: isolated git worktrees per slice, dual independent audit (Codex + Claude) on the same evidence, fail-closed verification before any ship.

```text
   slices.json ──▶ AutoKeel ──▶ Keel pipeline ──▶ verify_slice.py ──▶ slice done
                  (picks the    (gstack → compile                    (or failure
                   next slice)   → plan-orchestrator                  recorded
                                 → ship)                              + replan)

                    events.jsonl  ·  failure_ledger.jsonl  ·  slices.json
                          every decision lands in a file — never in chat memory
```

### Run a single iteration

```bash
pip install -r requirements.txt

# Preflight — verify environment + Keel wiring
python -m ops.autonomy.autokeel --doctor
python scripts/verify_autonomy_preflight.py --json

# Dry-run one iteration (pick next slice, plan, don't execute)
python -m ops.autonomy.autokeel --once --dry-run

# Run one real iteration
python -m ops.autonomy.autokeel --once

# Inspect what happened
python -m ops.autonomy.autokeel --status --failures
python -m ops.autonomy.autokeel --replay-events
```

One iteration touches exactly one slice. AutoKeel reads `policy.yaml`, picks the next pending slice, ensures its brief exists, compiles a playbook with Keel, runs it under plan-orchestrator's supervisor, decides whether to ship. Every decision lands in `events.jsonl`. Every failure is classified in `failure_ledger.jsonl`.

### Requirements

- macOS, Python 3.12+
- [Keel](https://github.com/AysajanE/keel) installed and on PATH
- Codex CLI and Claude Code, installed and authenticated
- An Oura account and credentials are needed only when the future production
  sync is activated. Deterministic development and tests must use fake
  transports and must never expose real credentials.

## Non-negotiables

The experiment is meaningful only if it stays honest.

- AutoKeel never calls `keel-run mark-manual-gate`, and never approves a human gate by any other path.
- A slice is complete only when `scripts/verify_slice.py <SLICE_ID> --json` passes — not because an agent says so.
- Evidence is real local evidence under `private/evidence/`, or sanitized evidence under `docs/evidence/`. Never fabricated.
- No raw health data, tokens, DuckDB files, snapshots, quarantine payloads, or provider payloads are tracked — `data/`, `private/`, `.env*`, `*.duckdb`, `*.sqlite`, `*.parquet` are gitignored.
- Required UI language (`patterns associated with this rating`, `correlation, not proven causation`, `insufficient stable signal`) is enforced. Causal language (`drivers`, `caused`, `tomorrow prediction`, `you would have felt`) is rejected at gate time.

The full safety contract is in [`AGENTS.md`](AGENTS.md).

## Repository layout

```text
health-data-hub/
├── ops/autonomy/             AutoKeel supervisor + policy + state + event/failure logs
├── src/
│   ├── db/schema.sql         DuckDB schema (S01, shipped)
│   └── warehouse/            warehouse.py, models.py — insert / aggregate / validate
├── tests/
│   ├── warehouse/            warehouse-layer tests (schema, quarantine, mood correction)
│   └── autonomy/             AutoKeel itself is tested
├── scripts/                  verification, preflight, dashboard, evidence collectors
├── docs/
│   ├── briefs/               slice briefs (input to Keel compile)
│   ├── gstack/               promoted design/autoplan artifacts
│   ├── playbooks/            generated Keel playbooks
│   ├── reviews/              sanitized autonomous review artifacts
│   ├── evidence/             sanitized external evidence
│   ├── local/                local-only docs (gitignored except README)
│   ├── health_data_hub_full_autonomous_design.md   the autonomous-mode research
│   └── keel-walkthrough_v*.html                    interactive Keel walkthroughs
├── private/evidence/         local sensitive evidence (never committed)
├── data/                     DuckDB warehouse and raw payloads (never committed)
├── AGENTS.md                 canonical agent/operator doc
└── CLAUDE.md                 Claude Code's project memory
```

## Read next

- **[`AGENTS.md`](AGENTS.md)** — operator doc. Repository layout, every safety rule, every failure class, full command catalogue. Start here if you're about to run AutoKeel.
- **[`docs/keel-walkthrough_v1.html`](docs/keel-walkthrough_v1.html)** — interactive end-to-end walkthrough of the Keel toolchain AutoKeel drives. Open in a browser; click anything.

---

Built on [Keel](https://github.com/AysajanE/keel) with autonomous gate substitution — a research test of whether an AI operator can drive a real build end to end without lying about it.

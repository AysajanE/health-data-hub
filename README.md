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

1. **The product — Health Data Hub v1.** A local-first **Sleep + Mood Retrospective Explainer**. Oura (+ 8 Sleep when stable) → DuckDB on your Mac → a small model → a Streamlit page that explains yesterday. Nothing leaves your machine.
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

- **Local-first.** DuckDB file on your laptop. No hosted backend. No SaaS.
- **You own the model.** It learns your baseline from your own data. Nobody else's.
- **Honest by design.** Until the model beats two trivial baselines on walk-forward evaluation, the UI shows *"Collecting model-ready days"* — not made-up insights.
- **Words chosen carefully.** `top model contributors`, `patterns associated with this rating`, `correlation, not proven causation`. Never `drivers`, `caused`, `you should`, `tomorrow prediction`. Hyper-health-conscious users deserve to not be nocebo'd by their own app.

## What's in v1, and where we are

v1 is nine slices. The slice ledger (`ops/autonomy/slices.json`) is the truth; this table is a snapshot.

| | Slice | What you get when it ships | Status |
|---|---|---|---|
| ✅ | **S01** Warehouse foundation | DuckDB schema, validated ingestion, quarantine for bad payloads | shipped on `ship/s01` |
| ☐ | **S02** Mood API loop | One-tap evening mood log from your phone via iOS Shortcut | pending |
| ☐ | **S03** Ingestion provider | Oura sleep flowing nightly (+ 8 Sleep if `pyEight` stays stable) | pending |
| ☐ | **S04** Feature engineering | Daily features your model trains on (`total_sleep_min`, `hrv_z`, `deep_sleep_pct`, `prior_day_feeling`) | pending |
| ☐ | **S05** Model lifecycle + gates | The model — but only allowed to speak after it beats baselines | pending |
| ☐ | **S06** Counterfactual generator | The "a sleep duration nearer your usual upper range was associated with…" line | pending |
| ☐ | **S07** Read API + Streamlit UI | The explainer card you saw at the top, rendered against your data | pending |
| ☐ | **S08** Backups + restore | launchd-scheduled encrypted snapshots to iCloud, verified restore path | pending |
| ☐ | **S09** Testing + v1 evaluation | The end-to-end gate that says v1 is real | pending |

## What this is not

- **Not medical advice.** v1 explains correlations in *your* past data. It does not predict your future, recommend interventions, or make any clinical claim.
- **Not a hosted service.** Everything runs on your Mac, against your data, with your credentials on your filesystem.
- **Not multi-tenant.** Single user, single device, single dataset by design.
- **Not finished.** S01 is done. Eight slices remain. An honest audit trail says so.

If you wanted a coach in your pocket, that's not this. The Autopilot tier (action features, N-of-1 experiments, prospective recommendations) lives in the v2+ vision — explicitly out of scope here because at this data scale, prospective recommendations are exactly where false precision and nocebo loops do the most damage.

## Start here

**If you just want to understand the system** — open [`docs/keel-walkthrough_v1.html`](docs/keel-walkthrough_v1.html) in a browser. It's the click-through tour of how Keel + AutoKeel build a real feature end to end.

**If you want to run the product on your own data** — wait until S02–S07 land, then follow the (then-real) Quickstart. You're early. Watch the slice ledger:

```bash
python -m ops.autonomy.autokeel --status --failures
```

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
- An Oura account (for the eventual sleep pull — local-only; token stays on your machine)

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

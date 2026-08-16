# S11 Mood Logging Recovery Autonomous Brief

Autonomy profile: guarded zero-supervision for S11 only.

Manual gates are forbidden. S11 is the narrow pre-S06 recovery for the expired
mood-transport, mood-compliance, and baseline-display tripwires. It builds the precommitted tiny
Streamlit-on-home-Wi-Fi form, proves one real non-synthetic persistence flow,
enforces the exact collecting state until a live baseline passes, and replaces
proxy Markdown gates with privacy-minimized typed JSON evidence.

## Required Outcomes

- A mobile-friendly Streamlit form writes a validated mood entry directly to
  the canonical DuckDB warehouse with source `manual` and the existing 04:00
  local mood-date cutoff; the private proof uses the aggregate marker
  `streamlit_mobile_form`. The active fallback defers the Shortcut and FastAPI
  runtime to v1.1.
- The form is LAN-bound, requires a session token from the environment, never
  logs the token or submitted mood value, and uses the canonical correction
  semantics.
- Every live warehouse write holds `data/.healthhub.lock`; first creation of
  `data/`, `private/`, `models/`, the warehouse, model artifacts, and evidence
  paths uses private permissions. Existing FastAPI security, mood-date, and
  correction behavior must remain green, and an API-path lock-contention test
  must prove there is no unlocked or duplicate fallback write.
- Transport evidence represents exactly seven completed local-date
  opportunities. A missing canonical mood row is a failed post; a successful
  row must be attributable to the expected logger source and timestamp.
- Fallback acceptance requires a real mobile-form submission whose exact
  persistence in both `mood_entries` and `mood_current` is verified locally.
  Synthetic mood writes are forbidden.
- Compliance evidence is `not_due` until the real logger activation date plus
  28 days. At maturity it requires at least 23 unique, non-backfill logged days
  out of 28 completed local days. Ratings, energy, notes, tokens, identifiers,
  and raw rows must never enter the report.
- S11 may not mark any tripwire `ok` or `fallback_accepted` without the
  corresponding deterministic evidence. Missing phone/LAN proof is
  `blocked_external`, never success.
- AutoKeel may not manufacture baseline evidence. A shared display guard and
  independent UI/runtime verifier must prove that every non-passing baseline
  state shows only `collecting model-ready days` and suppresses contributors,
  intervals, estimates, and counterfactuals. S07 must reuse this guard.

## Two-Boundary Completion Contract

S11 uses a two-boundary runtime/evidence bridge. Boundary one is hermetic ship
acceptance: the ship code, deterministic tests, reviews, and tracked-data gate
run in the detached ship worktree without opening the live warehouse or any
private evidence. Boundary two is the separately configured
`activation_acceptance`, which runs only after boundary one passes and invokes
the shipped verifier with the exact command:

`python scripts/verify_mood_logging_recovery.py --runtime-root-env HEALTH_HUB_RUNTIME_ROOT --json`

At boundary two, `HEALTH_HUB_RUNTIME_ROOT` identifies the canonical runtime
root. The verifier may read aggregate/private runtime evidence and access the
live warehouse read-only; AutoKeel sets `HEALTH_HUB_RUNTIME_READ_ONLY=1`. It
must not modify health rows, synthesize phone evidence, or treat missing
evidence as success. If the activation result is missing, malformed, nonzero,
or anything other than `status: ok`, S11 must not complete: it remains
`blocked_external` without replanning the already verified ship code.

## Scope Boundary

S11 does not build model fitting, counterfactual, read-API, explainer, ingestion-sync,
backup, or launchd functionality. S06 remains blocked until S11 is complete,
the typed tripwire evaluator is green, completed-slice integration is durable,
the state digest matches, and global invariants pass.

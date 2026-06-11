# S07 Read API and Streamlit UI Autoplan

Slice ID: S07
Lane: compiler
Risk: medium
Revision: 2 (2026-06-11 readiness enrichment: added the design's read-API and Streamlit surfaces, write roots, and out-of-scope discipline)

## Scope

Build the v1 read API and the Streamlit retrospective UI per the design doc
sections "API Contract (v1)", "UI Language Discipline", and the model-display
rules in "Model Lifecycle" and "Counterfactual Algorithm (v1)". Display model
output for a date only after that date's mood is logged, and preserve the
Oura-only v1 provider policy.

Authority: docs/gstack/health-data-hub-office-hours.md; S02 mood API
(src/api/app.py, src/api/security.py); S05 model surfaces (src/model/);
S06 counterfactual generator (src/model/counterfactual.py).

## Constraints

- Manual gates are forbidden.
- Read endpoints (date-explicit, retrospective only): `GET /api/health`, `GET /api/insights/latest_logged_day`, `GET /api/insights/{date}`, `GET /api/counterfactuals/latest_logged_day`, `GET /api/counterfactuals/{date}`. No prediction or intervention endpoints of any kind (v2+ surface is out of scope).
- Bind and auth policy is the design's Option B same-host-only model: one FastAPI process bound to `LAN_BIND_IP`; GET requests allowed only from loopback or the same LAN IP (403 otherwise); token via constant-time comparison; `LAN_BIND_IP` read from the environment, never resolved at startup.
- Mood-first rule: the UI must not show model output for date D until feeling for D is logged; when today's mood is missing, render the compact mood logger at the top, keep older days' insights and raw timelines visible, and refresh after logging.
- Contributor display tiers: sign stability >= 90 percent shows normally; 80-89 shows with the low-confidence label; below 80 is suppressed; when none survive, show the collecting message from the design.
- Baseline-gate rule: when the gate fails for the date, show the design's not-yet-better-than-baseline message and no contributors or counterfactuals; below N_model 37 show the collecting-progress message with the N/37 count.
- Prediction-interval display uses the design's bucket scheme (high at full width 2.0, medium to 3.5, low above) with the bucket prominent and the numeric interval secondary.
- Allowed provider labels: `Sleep source: Oura` and `8 Sleep: not active in v1 provider path`. Forbidden provider labels: `Merged from Oura + 8 Sleep`, `8 Sleep-adjusted sleep score`, and `8 Sleep says...`. Do not imply 8 Sleep was averaged, blended, reconciled, or used as fallback for v1 model features.
- All UI text must use the design's explanation-framed wording and satisfy the repository UI-language validator; assert positive markers in tests and keep any avoided-phrase checking list inside tests/ only (the validator scans src/ and app/ for the avoided phrases).
- Read API must source all data through the verified warehouse and model interfaces; no alternate input paths.

## Allowed Write Roots

- `src/api/read.py`
- `src/api/app.py`
- `app/streamlit_app.py`
- `tests/ui/`
- `docs/reviews/s07-autonomous-ui-language-review.md`

## Out of Scope

- Any write endpoint changes beyond wiring the existing mood logger into the page (S02 owns mood ingestion).
- Model or counterfactual logic changes (S05/S06 own those).
- launchd, backups, restore (S08).
- Tailscale or any network expansion beyond home Wi-Fi same-host policy.
- Drift-detection UI, context-chip modeling, or any v2 display surface.

## Deliverables

- `src/api/read.py` (read endpoints with same-host middleware and token dependency, wired into the existing app)
- `app/streamlit_app.py` (retrospective explainer page: mood-first gating, contributor tiers, gate/early-N messages, interval buckets, provider labels, counterfactual card with caveat)
- `tests/ui/` (endpoint auth/IP matrix per the design's test list; UI language assertions; mood-first gating; tier and gate-message behavior)
- `docs/reviews/s07-autonomous-ui-language-review.md`

## Implementation Tasks

### Read API endpoints

- [ ] Implement the five read endpoints with Option B same-host enforcement and constant-time token validation, returning insights and gated counterfactual payloads only for dates whose mood is logged.
  Files: `src/api/read.py`; `src/api/app.py`; `tests/ui`
  Verify: `python -m pytest tests/ui -q`

### Streamlit retrospective page

- [ ] Implement `app/streamlit_app.py` with the mood-first rule, compact mood logger, contributor tiers with the low-confidence label, baseline-gate and collecting messages, interval buckets, allowed provider labels, and the counterfactual card rendered from the S06 payload with its caveat and delta interval.
  Files: `app/streamlit_app.py`; `tests/ui`
  Verify: `python -m pytest tests/ui -q`

### Provider-policy and UI language tests

- [ ] Add UI tests asserting only the allowed provider labels appear, the forbidden labels never appear, and all rendered text passes the repository UI-language validator's rules.
  Files: `tests/ui`
  Verify: `python -m pytest tests/ui -q`

### Autonomous UI language review

- [ ] Produce the autonomous UI language review artifact with a command-evidence JSON whose commands all exit zero, following the S05 review-artifact pattern.
  Files: `docs/reviews/s07-autonomous-ui-language-review.md`
  Verify: `python scripts/check_autonomous_review_exists.py S07`

## Verification Expectations

- `python -m pytest tests/ui -q` passes.
- `python scripts/check_autonomous_review_exists.py S07` passes.
- `python scripts/check_no_tracked_data.py` passes.

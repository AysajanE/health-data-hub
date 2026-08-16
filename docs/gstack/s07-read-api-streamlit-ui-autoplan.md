# S07 Read API and Streamlit UI Autoplan

Slice ID: S07
Lane: compiler
Risk: medium
Revision: 3 (2026-08-16 tripwire recovery alignment: consume S11's verified mood form and defer FastAPI under the active Streamlit fallback)

## Scope

Build the v1 read API and the Streamlit retrospective UI per the design doc
sections "API Contract (v1)", "UI Language Discipline", and the model-display
rules in "Model Lifecycle" and "Counterfactual Algorithm (v1)". Display model
output for a date only after that date's mood is logged, and preserve the
Oura-only v1 provider policy.

Authority: docs/gstack/health-data-hub-office-hours.md; the active S11
mood-transport decision and form; S02 mood validation/persistence interfaces;
S05 model surfaces (src/model/); S06 counterfactual generator
(src/model/counterfactual.py).

## Constraints

- Manual gates are forbidden.
- Under the active S11 `streamlit_mobile_form` fallback, do not add or operate FastAPI read endpoints. The design explicitly defers FastAPI to v1.1 on this path. Streamlit reads through the verified local warehouse/model interfaces while keeping network and persistence policy from S11.
- If a future explicit transport-reopening slice supersedes S11 before S07, the date-explicit retrospective read endpoints may be restored under the design's Option B same-host/token rules. S07 itself cannot reopen that decision.
- Mood-first rule: the UI must not show model output for date D until feeling for D is logged; when today's mood is missing, render S11's verified compact mood form at the top, keep older days' insights and raw timelines visible, and refresh after logging. Do not implement a second write path.
- Contributor display tiers: sign stability >= 90 percent shows normally; 80-89 shows with the low-confidence label; below 80 is suppressed; when none survive, show the collecting message from the design.
- Baseline-gate rule: when the gate fails for the date, show the design's not-yet-better-than-baseline message and no contributors or counterfactuals; below N_model 37 show the collecting-progress message with the N/37 count.
- Prediction-interval display uses the design's bucket scheme (high at full width 2.0, medium to 3.5, low above) with the bucket prominent and the numeric interval secondary.
- Allowed provider labels: `Sleep source: Oura` and `8 Sleep: not active in v1 provider path`. Forbidden provider labels: `Merged from Oura + 8 Sleep`, `8 Sleep-adjusted sleep score`, and `8 Sleep says...`. Do not imply 8 Sleep was averaged, blended, reconciled, or used as fallback for v1 model features.
- All UI text must use the design's explanation-framed wording and satisfy the repository UI-language validator; assert positive markers in tests and keep any avoided-phrase checking list inside tests/ only (the validator scans src/ and app/ for the avoided phrases).
- All reads must use the verified warehouse and model interfaces; no alternate input paths.

## Allowed Write Roots

- `src/api/read.py` (only after an explicit transport-reopening decision)
- `src/api/app.py` (only after an explicit transport-reopening decision)
- `app/mood_form.py` (consume only; S11 owns its behavior)
- `app/streamlit_app.py`
- `tests/ui/`
- `docs/reviews/s07-autonomous-ui-language-review.md`

## Out of Scope

- Any mood-write behavior change (S11 owns the active form; S02 retains the deferred API implementation).
- Model or counterfactual logic changes (S05/S06 own those).
- launchd, backups, restore (S08).
- Tailscale or any network expansion beyond home Wi-Fi same-host policy.
- Drift-detection UI, context-chip modeling, or any v2 display surface.

## Deliverables

- `app/streamlit_app.py` (retrospective explainer page: S11 mood-form integration, mood-first gating, contributor tiers, gate/early-N messages, interval buckets, provider labels, counterfactual card with caveat)
- `src/api/read.py` only if a separately recorded transport-reopening decision exists before S07; otherwise FastAPI is explicitly deferred and this is not a missing deliverable
- `tests/ui/` (UI language assertions, mood-first gating, local-reader boundaries,
  and tier/gate-message behavior; include the endpoint auth/IP matrix only if a
  separately recorded reopening decision activates the FastAPI branch)
- `docs/reviews/s07-autonomous-ui-language-review.md`

## Implementation Tasks

### Read surface selection

- [ ] Verify the active S11 transport decision. On the expected fallback path, implement local date-explicit read functions for Streamlit and assert that no FastAPI runtime is required. Only a separately recorded reopening decision may select the five Option B endpoints.
  Files: `app/streamlit_app.py`; conditionally `src/api/read.py`; conditionally `src/api/app.py`; `tests/ui`
  Verify: `python -m pytest tests/ui -q`

### Streamlit retrospective page

- [ ] Implement `app/streamlit_app.py` with the mood-first rule, the imported S11 compact mood form, contributor tiers with the low-confidence label, baseline-gate and collecting messages, interval buckets, allowed provider labels, and the counterfactual card rendered from the S06 payload with its caveat and delta interval.
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

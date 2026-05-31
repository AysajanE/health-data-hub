# S07 Read API and Streamlit UI Autoplan

Slice ID: S07
Lane: compiler
Risk: medium

## Scope

Build the v1 read API and Streamlit retrospective UI. Display model output only for dates with same-day mood labels and preserve the Oura-only v1 provider policy.

## Constraints

- Manual gates are forbidden.
- Allowed provider labels: `Sleep source: Oura` and `8 Sleep: not active in v1 provider path`.
- Forbidden provider labels: `Merged from Oura + 8 Sleep`, `8 Sleep-adjusted sleep score`, and `8 Sleep says...`.
- Do not imply 8 Sleep was averaged, blended, reconciled, or used as fallback for v1 model features.
- Preserve all required v1 UI language restrictions.

## Deliverables

- `tests/ui/`
- `docs/reviews/s07-autonomous-ui-language-review.md`

## Implementation Tasks

### Provider-policy UI language restrictions

- [ ] Add UI tests that allow only Oura-active and 8 Sleep fallback-only language.
  Files: `tests/ui`
  Verify: `python -m pytest tests/ui -q`

### Autonomous UI language review

- [ ] Produce the autonomous UI language review artifact.
  Files: `docs/reviews/s07-autonomous-ui-language-review.md`
  Verify: `python scripts/check_autonomous_review_exists.py S07`

## Verification Expectations

- `python -m pytest tests/ui -q` passes.
- `python scripts/check_autonomous_review_exists.py S07` passes.
- `python scripts/check_no_tracked_data.py` passes.

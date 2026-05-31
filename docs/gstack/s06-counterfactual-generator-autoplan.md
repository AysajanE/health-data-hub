# S06 Counterfactual Generator Autoplan

Slice ID: S06
Lane: swr_preferred
Risk: high

## Scope

Build retrospective v1 counterfactual explanations from S05 model outputs. Preserve Oura-only v1 provider policy and do not introduce active 8 Sleep controls.

## Constraints

- Manual gates are forbidden; use autonomous_gate_review artifacts for high-risk counterfactual safety.
- The only mutable v1 counterfactual feature is `total_sleep_min`.
- Counterfactual `total_sleep_min` must come from Oura-derived eligible rows.
- Do not include 8 Sleep temperature, Autopilot, bed controls, room temperature, Pod controls, sleep score, sleep stages, or HRV as mutable features or action targets.
- Do not add recommendations, tomorrow predictions, medical advice, or prospective intervention language.

## Deliverables

- `tests/test_counterfactual.py`
- `docs/reviews/s06-autonomous-causal-framing-review.md`
- `docs/reviews/s06-autonomous-counterfactual-safety-review.md`

## Implementation Tasks

### Counterfactual scope restriction

- [ ] Enforce that v1 counterfactuals vary only Oura-derived `total_sleep_min` and never any 8 Sleep field or control.
  Files: `tests/test_counterfactual.py`
  Verify: `python -m pytest tests/test_counterfactual.py -q`

### Autonomous safety reviews

- [ ] Produce autonomous_gate_review artifacts for causal framing and counterfactual safety.
  Files: `docs/reviews/s06-autonomous-causal-framing-review.md`; `docs/reviews/s06-autonomous-counterfactual-safety-review.md`
  Verify: `python scripts/check_autonomous_review_exists.py S06`

## Verification Expectations

- `python -m pytest tests/test_counterfactual.py -q` passes.
- `python scripts/check_autonomous_review_exists.py S06` passes.
- `python scripts/check_no_tracked_data.py` passes.

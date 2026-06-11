# S06 Counterfactual Generator Autoplan

Slice ID: S06
Lane: swr_preferred
Risk: high
Revision: 2 (2026-06-11 readiness enrichment: added the design's full counterfactual algorithm surface, src deliverables, write roots, and out-of-scope discipline so the SWR pipeline does not regenerate against an under-specified plan)

## Scope

Implement the v1 retrospective counterfactual generator over S05 model outputs:
the single-feature, mutability- and safety-gated 1D scan defined in the design
doc section "Counterfactual Algorithm (v1)" of
docs/gstack/health-data-hub-office-hours.md. Preserve the Oura-only v1
provider policy and do not introduce active 8 Sleep controls.

Authority: docs/gstack/health-data-hub-office-hours.md sections
"Counterfactual Algorithm (v1)", "Feature Mutability Taxonomy", "Testing
Strategy", and "UI Language Discipline"; S05 model surfaces
(src/model/ridge.py, src/model/baseline_gate.py) tracked at HEAD.

## Constraints

- Manual gates are forbidden; use autonomous_gate_review artifacts for high-risk counterfactual safety.
- The only mutable v1 counterfactual feature is `total_sleep_min`; it must come from Oura-derived eligible rows.
- Run gates (ALL must hold before any candidate is generated): N_model >= 37; the baseline gate passed for the target date; `total_sleep_min` is sign-stable at >= 90 percent; the bootstrap delta interval requirement in the trivial-effect rule below.
- Candidate envelope: the unconditional 5th-95th percentile of `total_sleep_min` history, intersected with the safe floor.
- Increase-only: never propose a candidate below yesterday's actual value.
- Safe floor: candidates must be >= 420 minutes (configurable per user, default fixed at 420).
- Already-high suppression: when yesterday's actual sleep is at or above the user's recent median, suppress with the design's no-useful-suggestion message.
- Ten candidates per scan; the plausibility filter rejects any full candidate vector whose standardized Euclidean distance to the nearest historical day exceeds 2.0, and requires at least one historical day with similar context AND `total_sleep_min` within 30 minutes of the candidate.
- Bootstrap delta interval: 200 resamples, refit scaler plus ridge per resample, report the 5th-95th percentile of predicted deltas.
- Trivial-effect suppression: reject when the interval's lower bound is at or below zero, or the median delta is below 0.5 mood points.
- Selection: maximize median predicted delta minus 0.1 times the standardized jump from yesterday's actual value.
- Rendering must use the design's explanation-framed template with the delta interval (never a point estimate) and the correlation-not-causality caveat; all generated text must satisfy the repository UI-language validator (assert positive markers only; never embed the design's avoided-phrase list as literal strings anywhere outside tests/).
- Do not include 8 Sleep temperature, Autopilot, bed controls, room temperature, Pod controls, sleep score, sleep stages, or HRV as mutable features or action targets.
- Do not add tomorrow predictions, medical advice, or intervention language; the output is a retrospective model-explanation statement.
- Counterfactual code must consume model artifacts and feature rows ONLY through the verified S05 interfaces (RidgePredictor, baseline gate results, verified feature-row loading); no alternate data input paths.

## Allowed Write Roots

- `src/model/counterfactual.py`
- `tests/test_counterfactual.py`
- `docs/reviews/s06-autonomous-causal-framing-review.md`
- `docs/reviews/s06-autonomous-counterfactual-safety-review.md`

## Out of Scope

- Multi-feature counterfactuals, DiCE-style methods, causal inference claims (v2+).
- Any UI rendering or API endpoint (S07 owns the read API and Streamlit surfaces).
- launchd, backups, restore (S08).
- Changes to S05 model code, warehouse schema, ingestion, or provider policy.
- Intervention experiments or Autopilot behavior of any kind.

## Deliverables

- `src/model/counterfactual.py` (gated single-feature scan: run gates, envelope, increase-only and safe-floor constraints, plausibility filter, bootstrap delta interval, trivial-effect suppression, selection, render payload with caveat)
- `tests/test_counterfactual.py`
- `docs/reviews/s06-autonomous-causal-framing-review.md`
- `docs/reviews/s06-autonomous-counterfactual-safety-review.md`

## Implementation Tasks

### Gated counterfactual scan

- [ ] Implement `src/model/counterfactual.py` per the design algorithm: run gates (N_model, baseline gate, sign stability), the 5th-95th percentile envelope with the 420-minute safe floor and increase-only rule, the already-high suppression, ten candidates, the corrected full-vector plausibility filter (2.0 standardized-distance cap plus the similar-context day requirement), the 200-resample bootstrap delta interval, trivial-effect suppression, and the penalized selection rule. Return None whenever any gate or filter leaves no candidate.
  Files: `src/model/counterfactual.py`
  Verify: `python -m pytest tests/test_counterfactual.py -q`

### Counterfactual behavior tests

- [ ] Add `tests/test_counterfactual.py` covering the design's test list: returns None when gates fail, when no candidate passes plausibility, when the delta interval crosses zero, and when the median delta is below 0.5; returns a valid candidate on synthetic data with a known positive sleep effect; never proposes `total_sleep_min` below 420 minutes or below yesterday's actual value; varies only Oura-derived `total_sleep_min` and never any 8 Sleep field or control.
  Files: `tests/test_counterfactual.py`
  Verify: `python -m pytest tests/test_counterfactual.py -q`

### Autonomous safety reviews

- [ ] Produce autonomous_gate_review artifacts for causal framing and counterfactual safety, each with a command-evidence JSON whose commands all exit zero, following the S05 review-artifact pattern.
  Files: `docs/reviews/s06-autonomous-causal-framing-review.md`; `docs/reviews/s06-autonomous-counterfactual-safety-review.md`
  Verify: `python scripts/check_autonomous_review_exists.py S06`

## Verification Expectations

- `python -m pytest tests/test_counterfactual.py -q` passes.
- `python scripts/check_autonomous_review_exists.py S06` passes.
- `python scripts/check_no_tracked_data.py` passes.

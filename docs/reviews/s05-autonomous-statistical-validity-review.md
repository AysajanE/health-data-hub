# Autonomous Slice Review: S05 Statistical Validity Review

Autonomous slice review provenance: independent reviewer for the S05 model-lifecycle autonomous gate closure.

Review mode: autonomous_gate_review
Slice: S05
Review type: statistical validity
Verdict: pass
Result: pass
Blocking findings: none

## Scope and limits

This autonomous_gate_review evaluates the S05 statistical validity posture for the retrospective model lifecycle. The review covers the fixed four-feature contract, no sleep forward-fill for training, no mood-label imputation, bootstrap sign stability, conservative prediction intervals, ablation logging, derived health artifact handling, deterministic command references, and correlational framing limits.

No human signoff was performed.

This artifact is an autonomous review substitution. It is not a medical claim, causal proof, or a substitute for rerunning the required S05 verification commands.

## Evidence files checked

- `docs/gstack/s05-model-lifecycle-autoplan.md`
- `docs/briefs/s05-model-lifecycle.autonomous-brief.md`
- `docs/gstack/health-data-hub-office-hours.md`
- `scripts/retrain_model.py`
- `src/model/ridge.py`
- `src/model/baseline_gate.py`
- `src/model/eval_log.py`
- `tests/model/test_ridge.py`
- `tests/model/test_baseline_gate.py`
- `tests/model/test_retrain_entrypoint.py`
- `.local/plan_orchestrator/packet/artifacts/verification_report/verification_report.execute.round-0.json`
- `ops/autonomy/decisions/S05-lane-decision-20260531T190554-0400.json`
- `scripts/check_autonomous_review_exists.py`
- `scripts/check_no_tracked_data.py`

## Exact commands run

Attempt-2 row-05 verification checkpoint commands:

- `python scripts/check_autonomous_review_exists.py S05`
- `python -c "from pathlib import Path; a=Path('docs/reviews/s05-autonomous-model-gate-review.md').read_text(encoding='utf-8').lower(); b=Path('docs/reviews/s05-autonomous-statistical-validity-review.md').read_text(encoding='utf-8').lower(); assert 'autonomous_gate_review' in a and 'autonomous_gate_review' in b; assert 'baseline gate' in a; assert 'statistical' in b; assert 'deterministic' in a and 'deterministic' in b; assert 'retrospective' in a and 'retrospective' in b; assert 'correlational' in a and 'correlational' in b; assert 'derived health' in a and 'derived health' in b; assert 'limitations' in a and 'limitations' in b"`
- `python scripts/verify_s05_provider_policy.py --json`
- `python -m pytest tests/model -q`
- `python scripts/check_no_tracked_data.py`

Attempt-2 checkpoint reference: `.local/plan_orchestrator/packet/artifacts/verification_report/verification_report.execute.round-0.json`

Tracked command-evidence artifacts reused by this checkpoint:

- `python scripts/verify_s05_provider_policy.py --json` via `ops/autonomy/decisions/S05-lane-decision-20260531T190554-0400.json`
- `python scripts/check_no_tracked_data.py --json` via `ops/autonomy/decisions/S05-lane-decision-20260531T190554-0400.json`

Command evidence: ops/autonomy/decisions/S05-lane-decision-20260531T190554-0400.json

## Review findings

### Fixed feature contract and retrospective target

- `src/model/ridge.py` enforces exactly four model features: `total_sleep_min`, `hrv_z`, `deep_sleep_pct`, and `prior_day_feeling`. `hrv_avg_ms` remains display metadata only and is excluded from prediction and contribution calculations.
- `docs/gstack/health-data-hub-office-hours.md` and `docs/briefs/s05-model-lifecycle.autonomous-brief.md` keep the target retrospective: same-day evening `feeling[D]` is explained from sleep ending on morning `D`, and the framing remains retrospective rather than prospective.
- `scripts/retrain_model.py` loads only rows where the modeled feature fields are non-null, excludes `prior_day_feeling_imputed` rows, and filters out rows whose diagnostics violate the provider policy. That preserves no sleep forward-fill for training and no mood-label imputation in the model-ready training set.

Review conclusion: the implemented training contract matches the stated v1 retrospective scope and fixed-feature boundary.

### Statistical machinery and conservative intervals

- `src/model/ridge.py` uses `StandardScaler` plus `Ridge(alpha=1.0)` and computes bootstrap sign stability with 200 resamples on `feature_date`.
- The stability tiers remain conservative and deterministic: 90 percent or higher is `stable`, 80 to 89 percent is `low_confidence_signal`, and below 80 percent is `suppressed`.
- Prediction intervals use a 90 percent interval with a 2.0 mood-point full-width floor until `N >= 60`. `tests/model/test_ridge.py` exercises feature enforcement, contribution math, sign-stability tiers, and the interval-floor behavior. The attempt-2 verification checkpoint records `python -m pytest tests/model -q` as passing.

Review conclusion: the S05 statistical machinery is intentionally conservative and suitable for correlational retrospective explanation, not for fine-grained certainty claims.

### Baselines, ablations, and logged outputs

- `src/model/baseline_gate.py` evaluates ridge against both the rolling 7-day mean and prior-day baselines, which gives the model a statistical hurdle before any retrospective output may surface.
- `scripts/retrain_model.py` records baseline metrics together with ablation RMSEs for prior-mood-only and sleep-features-only variants, plus sign-stability outputs and latest linear contributions.
- `src/model/eval_log.py` serializes those runtime outputs to JSONL without requiring the review docs to embed any raw or derived health values.

Review conclusion: the recorded ablations and baselines improve statistical auditability, but they remain correlational diagnostics rather than causal identification.

### Correlational framing and derived health privacy

- The consulted design sources consistently require correlational wording and forbid causal, prospective, or recommendation language in v1. This review found the implemented S05 surfaces aligned to that constraint.
- Runtime artifacts under `models/` are derived health artifacts. They remain outside tracked review output, and `python scripts/check_no_tracked_data.py` returned `ok`.
- The attempt-2 row-05 verification checkpoint is the current packet-scoped proof for the review-checker invocation, the review-content assertion, and the `python -m pytest tests/model -q` pass status, while the tracked command-evidence JSON above provides reusable provenance for the provider-policy and tracked-data commands.
- This review document contains no raw health data, no provider payloads, no model coefficients, and no user-level derived values.

Review conclusion: the statistical-validity review preserves privacy and keeps the model framed as retrospective and correlational.

## Statistical validity decision

S05 is acceptable for autonomous_gate_review statistical-validity closure because the fixed-feature contract is enforced in code, training excludes imputed or policy-breaking rows, bootstrap sign stability and interval rules stay conservative, baseline and ablation outputs remain audit-ready, and the documented posture stays retrospective, correlational, and privacy-preserving.

## Residual limitations

- This review does not claim external validity, medical validity, or causal validity. The model is correlational and retrospective only.
- This review does not prove that real-world model performance will stay stable as more data accumulates; it confirms the implemented statistical controls and deterministic tests.
- Synthetic fixtures and local commands cannot eliminate all drift or confounding risk, so the baseline gate and confidence labels remain necessary limitations rather than cosmetic output.

## Review result

Verdict: pass
Blocking findings: none

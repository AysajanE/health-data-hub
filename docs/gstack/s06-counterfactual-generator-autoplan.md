# S06 Counterfactual Generator Autoplan

Slice ID: S06
Lane: swr_preferred
Risk: high
Revision: 3 (2026-08-16 frozen implementation contract: verified context builder, same-cohort baseline recomputation, one-fit bootstrap helper, exact public result and provenance types, canonical feature policy, deterministic date/history/candidate/bootstrap rules, and repository-required retrospective wording)

## Scope

Implement the v1 retrospective counterfactual generator over the verified S05
model cohort. The generator is a date-explicit, single-feature explanation of
past data. It varies `total_sleep_min` only, preserves the Oura-only v1 provider
policy, and does not expose any active 8 Sleep control.

Authority: this frozen contract; docs/gstack/health-data-hub-office-hours.md
sections "Counterfactual Algorithm (v1)", "Feature Mutability Taxonomy",
"Testing Strategy", and "UI Language Discipline"; and the tracked S05 model
surfaces `src/model/ridge.py`, `src/model/baseline_gate.py`, and the verified
feature-row loader in `scripts/retrain_model.py`. Where an older example render
sentence differs from this contract, the exact repository-required strings in
this contract win.

## Zero-Spend Dependency Boundary

Before any billed S06 generation, S11 and S12 must both be complete, their
recorded ship results must be durably integrated, and their distinct runtime
activation acceptances must have passed. Typed S11 tripwire evidence must still
evaluate green. S12 provider authority must still authorize the exact data
acquisition, retention, Ridge training/evaluation, mood-correlation, and
retrospective-explanation path; OAuth or token possession alone is never
authority. The fresh S06 lane decision must be created after that committed
state and bind every required S11/S12 product surface, both briefs/autoplans,
both readiness verifiers, the provider-authority schema and tracked decision,
and the landed-slice verification controls. Missing or stale inputs stop at
zero spend.

## Frozen Implementation Boundary

- `generate_retrospective_counterfactual` and `fit_scaled_ridge_once` are pure
  model-domain functions. They perform no database, pickle, filesystem,
  environment, API, or network I/O.
- The same module exposes the sole product context builder,
  `build_counterfactual_context`. It performs read-only I/O through
  `scripts.retrain_model.load_verified_feature_rows` and
  `src.model.eval_log.read_eval_records` only, then recomputes the S05 baseline
  gate on the attested cohort. It performs no writes and has no alternate row,
  JSON, pickle, environment, API, or network input path.
- S07 must call that builder; product code must not construct
  `CounterfactualContext` directly. Tests may construct frozen contexts and
  attestations from synthetic rows.
- After the collecting, as-of, and baseline gates pass, S06 calls
  `RidgePredictor(alpha=context.model_alpha, bootstrap_resamples=200,
  random_seed=0).fit(history_rows, history_targets)` exactly once. That one S05
  fit computes the point model and sign stability on the exact supplied cohort.
  A pre-fit suppression calls it zero times.
- The generator must not call `RidgePredictor.fit()` inside the 200-resample
  delta bootstrap. That method also computes sign stability, so nesting it
  would perform a second bootstrap inside every delta-bootstrap refit.
- S06 must expose and test `fit_scaled_ridge_once(rows, targets, *, alpha) ->
  ScaledRidgeFit`. It fits exactly one `StandardScaler` and one `Ridge`, in
  `MODEL_FEATURES` order, and performs no sign-stability bootstrap, residual
  bootstrap, interval calculation, persistence, or I/O. `ScaledRidgeFit`
  exposes `predict(rows)` only.
- An invocation that reaches the delta bootstrap calls
  `fit_scaled_ridge_once` exactly 200 times total, once for each resampled
  training cohort, and reuses that one fit to score the actual row and every
  surviving candidate. An invocation suppressed before bootstrap calls it zero
  times. The builder also uses this helper through a private fit/predict adapter
  when recomputing S05 walk-forward baseline predictions; those pre-generator
  fits are not delta-bootstrap fits. The helper is not an alternate data-
  loading path.
- `fit_scaled_ridge_once` must use `MODEL_FEATURES` and `context.model_alpha`.
  A parity test must prove that a one-fit helper prediction matches the point
  prediction from `RidgePredictor` fit on the same rows and targets.

## Canonical Feature Policy

`src/model/counterfactual.py` must export exactly one `FEATURE_POLICY` registry.
S06 and S07 import it rather than reproducing mutability, display, unit, or
safety values elsewhere. Its representation and values are frozen:

```python
@dataclass(frozen=True)
class FeaturePolicyEntry:
    role: Literal[
        "behavior",
        "physiological_proxy",
        "noisy_stage_metric",
        "outcome_lag",
    ]
    mutable: bool
    recommendation_policy: Literal["allowed_with_caveat", "disallowed"]
    show_as_contributor: bool
    display_name: str
    display_companion: str | None
    unit: Literal["minutes", "percent", "rating"] | None
    safe_floor: float | None

FEATURE_POLICY: Mapping[str, FeaturePolicyEntry] = MappingProxyType({
    "total_sleep_min": FeaturePolicyEntry(
        role="behavior",
        mutable=True,
        recommendation_policy="allowed_with_caveat",
        show_as_contributor=True,
        display_name="Total sleep",
        display_companion=None,
        unit="minutes",
        safe_floor=420.0,
    ),
    "hrv_z": FeaturePolicyEntry(
        role="physiological_proxy",
        mutable=False,
        recommendation_policy="disallowed",
        show_as_contributor=True,
        display_name="HRV (z-score vs baseline)",
        display_companion="hrv_avg_ms",
        unit=None,
        safe_floor=None,
    ),
    "deep_sleep_pct": FeaturePolicyEntry(
        role="noisy_stage_metric",
        mutable=False,
        recommendation_policy="disallowed",
        show_as_contributor=True,
        display_name="Deep sleep %",
        display_companion=None,
        unit="percent",
        safe_floor=None,
    ),
    "prior_day_feeling": FeaturePolicyEntry(
        role="outcome_lag",
        mutable=False,
        recommendation_policy="disallowed",
        show_as_contributor=True,
        display_name="Yesterday's feeling",
        display_companion=None,
        unit="rating",
        safe_floor=None,
    ),
})
```

`MappingProxyType` freezes the mapping and every value is a frozen dataclass, so
the registry is deeply immutable. Its keys equal `MODEL_FEATURES` exactly and
in the same insertion order. Only an entry with `mutable is True` and a policy
other than `disallowed` may be varied. Therefore v1 varies
`total_sleep_min` and nothing else.

## Public Input and As-of Contract

The public generator is:

```python
@dataclass(frozen=True)
class CounterfactualConfig:
    delta_bootstrap_seed: int = 20260611

@dataclass(frozen=True)
class VerifiedCohortAttestation:
    loader_contract: Literal[
        "scripts.retrain_model.load_verified_feature_rows:v1"
    ]
    provider_policy: Literal["oura_only_v1"]
    sleep_provider: Literal["oura"]
    training_cohort_sha256: str
    target_row_sha256: str | None

@dataclass(frozen=True)
class CounterfactualContext:
    target_date: date
    target_mood_logged: bool
    target_row: Mapping[str, Any] | None
    history_rows: tuple[Mapping[str, Any], ...]
    history_targets: tuple[float, ...]
    cohort_attestation: VerifiedCohortAttestation
    baseline_gate: BaselineGateResult | None
    model_alpha: float | None
    model_version: str | None
    model_trained_through_date: date | None
    feature_version: str | None
    eval_recorded_at_utc: datetime | None

build_counterfactual_context(
    *,
    target_date: date,
    target_mood_logged: bool,
    database_path: Path,
    eval_log_path: Path,
) -> CounterfactualContext

generate_retrospective_counterfactual(
    context: CounterfactualContext,
    *,
    config: CounterfactualConfig = DEFAULT_COUNTERFACTUAL_CONFIG,
) -> CounterfactualResult
```

All dataclasses in this contract are frozen. `CounterfactualConfig` exposes only
the seed so deterministic tests can select a different explicit seed; every
threshold, count, window, and policy value remains fixed for product behavior.

The builder contract is deterministic:

1. Call `load_verified_feature_rows(database_path)` once. Do not accept caller-
   supplied product rows. This S05 loader is the provider-policy authority.
2. Call `read_eval_records(eval_log_path)` once. A JSON parse error propagates.
   Ignore non-`trained` records. Every `status == "trained"` record must have a
   parseable `trained_through_date`, positive integer `n_model`, non-empty
   `model_version` and `feature_version`, and an offset-aware parseable
   `recorded_at_utc`; a partial or invalid trained record raises `ValueError`.
   Of the valid trained records, only dates below `target_date` are eligible.
   Stored gate booleans are not same-cohort evidence and must not be copied into
   the new context.
3. Select the record with the greatest `trained_through_date`; break a tie by
   greatest `recorded_at_utc` instant after normalizing offsets to UTC; break a
   remaining tie by the later JSONL line. Never select by filesystem mtime.
4. For `model_version == "ridge-v1.0"`, set `model_alpha = 1.0`. Any other
   version is unsupported until its alpha is persisted in a verified S05
   contract; fail closed with `ValueError` rather than guessing.
5. Filter verified rows to `feature_date <= trained_through_date`, require the
   count and every row's feature version to match the record, and split each
   row's `feeling` into `history_targets`. Require the last history row's date to
   equal `trained_through_date`; a record whose cutoff row is absent is
   incoherent even when its count happens to match. Do not include a newer
   pre-`D` row.
6. Select `target_row` only when a verified loaded row has
   `feature_date == target_date`. `target_mood_logged` comes from S07's
   mood-current lookup; therefore a logged mood with no verified target row is
   `target_features_unavailable`. A target row while
   `target_mood_logged is False` is a stale/contradictory caller view and raises
   `ValueError`; S07 may reread and retry rather than suppressing valid data.
7. Hash the canonical training cohort and target row with SHA-256 over UTF-8
   JSON. Canonicalize every row to a dictionary containing these fields only:
   `feature_date` as an ISO date, each of the four `MODEL_FEATURES` and
   `feeling` as a finite float, and `feature_version` as a non-empty string.
   Serialize the chronological training rows as one JSON list and the target as
   one JSON object with `sort_keys=True`, `separators=(",", ":")`,
   `ensure_ascii=True`, and `allow_nan=False`; hash those exact bytes with
   `sha256(...).hexdigest()`. Put the lowercase 64-character digests and the
   three exact authority literals in `VerifiedCohortAttestation`.
8. Recompute `BaselineGateResult` on exactly that hashed current cohort and its
   targets by calling S05 `evaluate_baseline_gate`. Supply a private S06
   fit/predict adapter whose `fit` delegates to `fit_scaled_ridge_once` with the
   selected alpha; baseline folds do not need a sign-stability bootstrap. Never
   reuse the stored eval-record pass flag. A same-count correction changes the
   digest and the recomputed gate together; a correction or backfill that
   changes `n_model` fails the record-count check instead of being paired with
   the old eval record.
9. If no trained record is eligible, return a context with every verified row
   before `D` for the collecting count, an all-`None` model/eval context, and a
   valid attestation over those rows. Do not convert an invalid or partial
   trained record into absence.

Date selection is fail-closed and point-in-time:

1. For target date `D`, select the newest coherent model/eval context whose
   `model_trained_through_date < D`. Never use an artifact trained on `D` or on
   a later date.
2. When a complete as-of model/eval context exists, `history_rows` and
   `history_targets` are exactly its verified Oura-only training cohort, with
   unique `feature_date` values in ascending order and every date
   `<= model_trained_through_date`. Do not append newer pre-`D` rows that the
   selected context did not use. Before a model exists, these fields contain
   all verified model-ready rows before `D` solely to provide the count for
   `collecting_model_ready_days`.
3. The target row is separate from history and has `feature_date == D`.
   Neither the target row nor any row after `D` may enter the envelope, recent
   median, standardization, similarity search, or bootstrap.
4. A model/eval context is coherent only when the recomputed `baseline_gate`, `model_alpha`,
   `model_version`, `model_trained_through_date`, `feature_version`, and
   `eval_recorded_at_utc` are all present. The baseline result must report the
   same `n_model` as `len(history_rows)`. S06 computes sign stability itself by
   fitting one S05 `RidgePredictor` on that exact cohort.
5. Missing target mood, missing target features, or no eligible as-of model are
   normal suppression states. Mismatched dates, cohort sizes, feature order,
   provider policy, or provenance are programming/data-contract errors and
   raise `ValueError`; they must not be hidden as a suppression.
6. After the collecting gate, an all-`None` model/eval context returns
   `as_of_model_unavailable`. A partially populated context raises `ValueError`.
7. Before any gate result, recompute both attestation hashes from the context
   and require the exact loader, provider-policy, and sleep-provider literals.
   A mismatch raises `ValueError`. This makes accidental raw/arbitrary row
   construction observable while keeping health values out of provenance.

This rule lets the evening mood unlock a retrospective result without training
on that mood label. It is deterministic and auditable for the current canonical
pre-`D` cohort. Current S05 eval records do not persist a cohort digest, so S06
must not claim that it reconstructed the exact warehouse bytes that existed at
the historical eval timestamp. It instead publishes the current cohort digest
and recomputes the S05 baseline gate on those same rows. Byte-for-byte replay of
a superseded historical cohort requires a future, versioned S05 artifact change
and is outside S06.

## Exact Public Result Schema

The top-level function never returns bare `None`. It returns these frozen
dataclasses:

```python
SuppressionReason = Literal[
    "target_mood_missing",
    "target_features_unavailable",
    "collecting_model_ready_days",
    "as_of_model_unavailable",
    "baseline_gate_not_passed",
    "mutable_feature_not_stable",
    "actual_at_or_above_recent_median",
    "empty_candidate_envelope",
    "no_plausible_candidate",
    "delta_interval_not_positive",
    "delta_below_materiality_floor",
]

@dataclass(frozen=True)
class CounterfactualProvenance:
    target_date: date
    history_cutoff_date: date
    model_version: str | None
    model_trained_through_date: date | None
    model_alpha: float | None
    feature_version: str | None
    eval_recorded_at_utc: datetime | None
    training_start_date: date | None
    training_end_date: date | None
    n_model: int
    loader_contract: Literal[
        "scripts.retrain_model.load_verified_feature_rows:v1"
    ]
    provider_policy: Literal["oura_only_v1"]
    sleep_provider: Literal["oura"]
    training_cohort_sha256: str
    target_row_sha256: str | None
    baseline_gate_source: Literal[
        "s05_gate_recomputed_from_attested_cohort"
    ] | None
    baseline_gate_eligible: bool | None
    baseline_gate_passed: bool | None
    point_model_fit_source: Literal[
        "s06_refit_from_attested_cohort"
    ] | None
    total_sleep_sign_stability: float | None
    sign_stability_seed: Literal[0]
    sign_stability_resamples_configured: Literal[200]
    sign_stability_resamples_executed: Literal[0, 200]
    delta_bootstrap_seed: int
    delta_bootstrap_resamples_configured: Literal[200]
    delta_bootstrap_resamples_executed: Literal[0, 200]

@dataclass(frozen=True)
class RetroCF:
    feature_name: Literal["total_sleep_min"]
    feature_display_name: Literal["Total sleep"]
    actual_value: float
    comparison_value: float
    model_delta_low: float
    model_delta_high: float
    median_delta: float
    direction: Literal["increase_only"]
    framing_label: Literal["model-estimated change in your past data"]
    caveat: Literal["correlation, not proven causation"]

@dataclass(frozen=True)
class CounterfactualResult:
    status: Literal["available", "suppressed"]
    suppression_reason: SuppressionReason | None
    counterfactual: RetroCF | None
    provenance: CounterfactualProvenance
```

`comparison_value` is the candidate value of `total_sleep_min` in minutes; it
is not a predicted mood value. For `available`, `suppression_reason` is `None`
and `counterfactual` is present. For `suppressed`, the reason is present and
`counterfactual` is `None`. `history_cutoff_date` is always `target_date - 1
day`, and `n_model` always equals `len(history_rows)`. The model version/date,
positive ridge alpha, feature version, eval timestamp, training start/end, and
baseline fields are all present when a complete model/eval context exists and
all `None` when it does not; when present,
`training_end_date == model_trained_through_date < target_date`. No raw training
rows, labels, provider payloads, model paths, coefficients, or secrets may
appear in the result. The loader, provider-policy, and sleep-provider fields
always equal the three validated attestation literals. The two SHA-256 fields
always equal the recomputed attestation digests; `target_row_sha256` is `None`
exactly when `target_row` is absent. `baseline_gate_source` is present exactly
for a complete model/eval context and declares that the stored eval gate flags
were not trusted.
`point_model_fit_source` and `total_sleep_sign_stability` are `None` before the
one full S05 fit and present for every later suppression or available result.
The configured counts are always 200. An executed count is 0 until that
bootstrap runs and 200 afterward; no other value is valid. Thus a result
distinguishes S05 sign-stability work from S06 delta-refit work without exposing
rows or coefficients.

## Deterministic Algorithm

Apply gates and suppression reasons in this exact order:

1. `target_mood_missing`
2. `target_features_unavailable`
3. `collecting_model_ready_days` when `n_model < 37`
4. `as_of_model_unavailable`
5. `baseline_gate_not_passed` unless the same-cohort gate is eligible and passed
6. `mutable_feature_not_stable` unless `total_sleep_min` sign stability is at
   least `0.90`
7. `actual_at_or_above_recent_median`
8. `empty_candidate_envelope`
9. `no_plausible_candidate`
10. `delta_interval_not_positive`
11. `delta_below_materiality_floor`

The computation is frozen as follows:

- **Recent median:** median `total_sleep_min` of the latest 28 chronological
  model-ready history rows before `D`. The gate guarantees at least 37 history
  rows whenever this statistic is reached. Use NumPy's linear median behavior
  and suppress when actual sleep is greater than or equal to this median.
- **Envelope quantiles:** compute the unconditional 5th and 95th percentiles
  over all as-of history sleep values with `numpy.quantile(...,
  method="linear")`. Set `lower = max(actual_value, 420, p5)` and `upper = p95`.
  Suppress when `upper <= lower`.
- **Candidates:** generate exactly ten float candidates with
  `numpy.linspace(lower, upper, num=11)[1:]`. Thus the lower endpoint and the
  unchanged actual value are excluded, the upper endpoint is included, and
  every candidate is strictly above `lower`. Do not round before filtering,
  prediction, or selection; S07 owns display rounding.
- **Standardization:** fit one `StandardScaler` to all four `MODEL_FEATURES` in
  the as-of history cohort. Scikit-learn's scale of `1.0` for a zero-variance
  feature is authoritative.
- **Full-vector plausibility:** substitute only candidate sleep into the target
  vector. Keep the other three target values unchanged. A candidate must have
  a nearest historical full-vector Euclidean distance `<= 2.0` in standardized
  units.
- **Similar-context witness:** the same candidate must have at least one
  historical row whose sleep is within `30.0` minutes inclusive and whose
  absolute standardized difference from the target is `<= 1.0` independently
  for each of `hrv_z`, `deep_sleep_pct`, and `prior_day_feeling`. The nearest
  full-vector row and this witness may be different rows.
- **Bootstrap:** sort the cohort by `feature_date`; use
  `numpy.random.default_rng(config.delta_bootstrap_seed)` (product default
  `20260611`); draw 200 row-index samples of size
  `n_model` with replacement; reuse the same 200 samples for every candidate;
  call `fit_scaled_ridge_once` once per sample; and compute raw, unclipped
  `prediction(candidate) - prediction(actual)` for each candidate.
- **Delta summary:** use `numpy.quantile(deltas, [0.05, 0.5, 0.95],
  method="linear")`. Reject a candidate when low `<= 0`; among the remaining
  candidates reject median `< 0.5`.
- **All-candidate reason:** if every plausible candidate has low `<= 0`, return
  `delta_interval_not_positive`. If at least one has low `> 0` but every such
  candidate has median `< 0.5`, return `delta_below_materiality_floor`.
- **Selection:** maximize `median_delta - 0.1 * abs(candidate - actual) /
  total_sleep_scale`. Break an exact score tie by choosing the smaller candidate
  value. Return the selected candidate's 5th, 95th, and median delta without
  clipping or point-estimate substitution.
- **Seed provenance:** the default delta seed is `20260611`; any test-only override
  must be explicit in `CounterfactualConfig` and is recorded in provenance.
  Product code must use the default. Provenance separately records the fixed
  S05 sign-stability seed `0` and its 200 resamples.

## Language and Provider Constraints

- S06 returns structured values, `model-estimated change in your past data`,
  and `correlation, not proven causation`. It does not compose a free-form
  product sentence; S07 owns presentation and must render both exact strings.
- The result is retrospective and explanatory. It contains no instruction,
  prediction, medical guidance, or present-day action language.
- 8 Sleep temperature, Autopilot, bed controls, room temperature, Pod controls,
  sleep score, sleep stages, and HRV are never mutable features or action
  targets. 8 Sleep rows never enter history or target feature construction.

## Allowed Write Roots

- `src/model/counterfactual.py`
- `tests/test_counterfactual.py`
- `docs/reviews/s06-autonomous-causal-framing-review.md`
- `docs/reviews/s06-autonomous-counterfactual-safety-review.md`

## Out of Scope

- Multi-feature counterfactuals, DiCE-style methods, or claims beyond
  retrospective association.
- Any UI rendering or API endpoint; S07 owns those surfaces.
- launchd, backups, or restore; S08 owns those surfaces.
- Changes to S05 model behavior, artifact formats, warehouse schema, ingestion,
  provider policy, or the verified row loader.
- Intervention experiments or Autopilot behavior.

## Deliverables

- `src/model/counterfactual.py`: frozen public dataclasses, feature policy,
  one-fit helper, mandatory verified context builder with same-cohort baseline
  recomputation, deterministic gated scan, and structured result.
- `tests/test_counterfactual.py`
- `docs/reviews/s06-autonomous-causal-framing-review.md`
- `docs/reviews/s06-autonomous-counterfactual-safety-review.md`

## Implementation Tasks

### Frozen domain contract and one-fit helper

- [ ] Implement the frozen dataclasses, immutable `FEATURE_POLICY`, context
  validation, provenance construction, and `fit_scaled_ridge_once` in
  `src/model/counterfactual.py`. Preserve the exact names, fields, nullability,
  deep-immutability mechanism, date boundary, and execution-count provenance in
  this plan.
  Files: `src/model/counterfactual.py`; `tests/test_counterfactual.py`
  Verify: `python -m pytest tests/test_counterfactual.py -q`

### Verified product context builder

- [ ] Implement and test `build_counterfactual_context` exactly as specified:
  only the two approved read-only readers, trained-record validation and
  three-part tie-break, fixed version-to-alpha mapping, cutoff/count/version
  matching, target separation, canonical hashes, and S05 baseline-gate
  recomputation through the lightweight adapter. Stored eval gate flags must
  not become the context gate. No eligible record is a valid all-`None` model
  context; a malformed or incoherent trained record fails closed.
  Files: `src/model/counterfactual.py`; `tests/test_counterfactual.py`
  Verify: `python -m pytest tests/test_counterfactual.py -q`

### Deterministic gated scan and behavior tests

- [ ] Implement the frozen gate precedence, history statistics, candidates,
  plausibility checks, bootstrap summaries, materiality rules, selection, and
  structured result. Add every test listed below; do not replace structured
  suppressions with bare `None` or presentation prose.
  Files: `src/model/counterfactual.py`; `tests/test_counterfactual.py`
  Verify: `python -m pytest tests/test_counterfactual.py -q`

### Autonomous safety reviews

- [ ] Produce both required `autonomous_gate_review` artifacts with sanitized
  command evidence whose commands all exit zero and whose findings explicitly
  cover the frozen as-of, provider, statistical, schema, and wording contracts.
  Files: `docs/reviews/s06-autonomous-causal-framing-review.md`;
  `docs/reviews/s06-autonomous-counterfactual-safety-review.md`
  Verify: `python scripts/check_autonomous_review_exists.py S06`

## Required Tests

`tests/test_counterfactual.py` must cover:

- feature-policy keys/order, every frozen field value, only-sleep mutability,
  mapping mutation failure, and frozen-entry mutation failure;
- builder calls each approved reader exactly once and performs no other I/O;
  record eligibility and the trained-through/timestamp/JSONL-order tie-break;
  unknown model version, invalid timestamp, malformed/partial trained record,
  count/version/cutoff mismatch, no eligible record, target separation, and
  exact canonical training/target hashes, including numeric normalization and
  contradictory mood/target state;
- the builder ignores stored gate booleans and recomputes `BaselineGateResult`
  on the hashed cohort; a same-count row correction changes the digest and gate
  together, while a count-changing correction/backfill fails closed;
- zero `RidgePredictor.fit` calls before the point-fit gate, exactly one
  `RidgePredictor.fit` call thereafter, one-fit parity with that point model,
  zero delta-helper calls before bootstrap, and exactly 200 delta-helper calls
  for every generator invocation that reaches bootstrap; separately account
  for the lightweight helper calls made by builder baseline folds;
- every suppression reason and the frozen precedence when multiple gates fail;
- rejection of future-trained artifacts, target/history overlap, duplicate or
  unordered dates, cohort/gate/provenance mismatch, and non-Oura input;
- 28-row recent median, linear p5/p95, exact ten-candidate endpoint behavior,
  safe floor, and increase-only behavior;
- full-vector distance, per-feature similar-context thresholds, and inclusive
  30-minute boundary;
- deterministic seed/sample reuse, linear delta quantiles, interval and
  materiality suppression, selection score, and smaller-change tie-break;
- result-schema available/suppressed invariants, loader/provider attestation,
  target/cohort digests, baseline/point-fit source markers,
  configured/executed bootstrap counts, and exact framing/caveat strings;
- absence of 8 Sleep fields or controls from policy, input variation, output,
  and provenance.

## Autonomous Safety Reviews

Produce both required `autonomous_gate_review` artifacts with sanitized command
evidence whose commands all exit zero. The reviews must explicitly verify the
frozen date/as-of boundary, one-fit bootstrap behavior, suppression schema,
feature policy, exact retrospective strings, and provider exclusion.

## Verification Expectations

- `python -m pytest tests/test_counterfactual.py -q` passes.
- `python scripts/check_autonomous_review_exists.py S06` passes.
- `python scripts/check_no_tracked_data.py` passes.

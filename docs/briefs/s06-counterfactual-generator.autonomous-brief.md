# S06 Counterfactual Generator Autonomous Brief

Autonomy profile: guarded zero-supervision for S06 only.

Manual gates are forbidden. High-risk counterfactual work must use autonomous_gate_review artifacts, deterministic tests, and recorded evidence instead of human signoff.

## Scope

S06 creates retrospective counterfactual explanations for v1. It must not introduce recommendations, predictions, autopilot language, or active 8 Sleep controls.

The frozen implementation contract is `docs/gstack/s06-counterfactual-generator-autoplan.md` revision 3. Generated playbooks and implementation work must follow that contract exactly rather than infer missing statistical, date-selection, schema, or wording rules.

## Pre-Launch Dependencies

S06 must not enter its usage-billed SWR lane until S11 and S12 are complete and
durably integrated. S11 completion includes the separate real mobile-form
activation acceptance and green typed tripwires. S12 completion includes a
currently valid provider-authority decision, hermetic production-sync code
acceptance, and separate read-only runtime sync acceptance. A functioning Oura
token or historical smoke result is not authority. The S06 lane decision must
be materialized only after those completed dependency trees are committed, and
must hash-bind their product/runtime surfaces plus both readiness contracts.

## Provider Policy Requirements

- The only mutable v1 counterfactual feature is `total_sleep_min`.
- `total_sleep_min` must come from Oura-derived eligible S04 rows under the Oura-only v1 policy.
- 8 Sleep temperature, Autopilot, bed controls, room temperature, Pod controls, sleep score, sleep stages, and HRV must not be counterfactual inputs or action targets.
- 8 Sleep / pyEight remains fallback-only unless a future explicit provider-reopening slice supersedes S03.

## Frozen Model and Date Contract

- S07 must use S06's `build_counterfactual_context`; product code must not construct a context from arbitrary rows. The builder reads only `load_verified_feature_rows` and `read_eval_records`, selects the greatest trained-through date below target `D`, then breaks ties by the eval timestamp instant normalized to UTC and JSONL order.
- The builder emits exact Oura-only loader/provider markers plus the autoplan's byte-exact canonical SHA-256 digests for the training cohort and target row. The generator recomputes them before any gate. A mismatched date, digest, cohort, baseline result, provider, feature order, provenance value, or contradictory mood/target state raises `ValueError`; it is not silently suppressed.
- The target row and every later row are excluded from model history, envelope statistics, similarity checks, and bootstrap samples. For the current `ridge-v1.0` context, alpha is exactly 1.0; an unknown version fails closed rather than guessing.
- Recompute the S05 `BaselineGateResult` on the newly hashed cohort through a private adapter backed by `fit_scaled_ridge_once`; never trust or copy the historical eval record's gate booleans. The record selects the cutoff, version, count, and timestamp, while the digest and recomputed gate attest the current canonical rows. A count mismatch fails closed.
- Require `n_model >= 37` and a passed eligible recomputed gate, then fit one S05 `RidgePredictor` on that exact history cohort with 200 sign-stability resamples and seed 0; require `total_sleep_min` sign stability `>= 0.90`.
- Do not call `RidgePredictor.fit()` inside the delta bootstrap. Implement the autoplan's public `fit_scaled_ridge_once` helper, which performs exactly one scaler fit and one ridge fit with no nested sign-stability or residual bootstrap. A generator invocation that reaches bootstrap performs one full S05 point/stability fit plus exactly 200 delta-helper fits reused across all candidates; a pre-fit suppression performs none. Baseline-fold helper calls belong to the builder and are counted separately from this zero-or-200 generator invariant.
- Current S05 eval records contain no cohort digest. S06 therefore makes no claim that current warehouse rows are byte-identical to rows at the historical eval timestamp; it publishes the current digests and recomputes the gate. Historical byte-for-byte cohort replay remains a future versioned S05-artifact concern.

## Frozen Feature and Result Contract

- `src/model/counterfactual.py` exports the sole `FEATURE_POLICY` registry as a `Mapping[str, FeaturePolicyEntry]` backed by `MappingProxyType`, with every entry a frozen dataclass. Its keys equal `MODEL_FEATURES` in order. All exact role, mutability, recommendation, contributor, display, companion, unit, and safe-floor fields come from the autoplan; only `total_sleep_min` is mutable and recommendable, with display name `Total sleep`, unit `minutes`, and safe floor 420.
- The generator returns `CounterfactualResult`, never bare `None`. Status is `available` with one `RetroCF`, or `suppressed` with exactly one reason from the autoplan's closed `SuppressionReason` list.
- Every result includes `CounterfactualProvenance`: target/history/model dates, eval timestamp, versions, ridge alpha, model-ready count, exact loader/provider/Oura attestation literals, training and optional target digests, recomputed-baseline and point-refit source markers, baseline flags, observed sleep sign stability, and separate sign-stability/delta-bootstrap seeds plus configured and executed counts. It contains no training rows, labels, paths, coefficients, provider payloads, or secrets.
- `RetroCF` contains the sleep feature name/display name, actual and candidate sleep values, 5th/95th delta bounds, median delta, `increase_only`, and the two exact strings below. `comparison_value` means candidate sleep minutes, not predicted mood.

## Frozen Determinism Rules

- Recent median is the latest 28 chronological model-ready history rows before `D`.
- Envelope p5/p95 and delta p5/median/p95 use NumPy linear quantiles.
- Candidate lower bound is `max(actual, 420, p5)`; upper bound is p95; candidates are `numpy.linspace(lower, upper, num=11)[1:]`, so ten values exclude the lower endpoint and include p95.
- Standardize all four features on the as-of cohort. Require nearest full-vector distance `<= 2.0`. Also require a historical witness within 30 minutes of candidate sleep and within 1.0 standardized unit independently for each immutable feature.
- Use `numpy.random.default_rng(config.delta_bootstrap_seed)`, whose product default is `20260611`; reuse the same 200 resample index sets for every candidate, compute raw unclipped prediction deltas and linear 5th/50th/95th percentiles, require low bound `> 0`, and require median delta `>= 0.5`.
- Select the largest `median_delta - 0.1 * standardized_sleep_jump`; exact ties choose the smaller candidate.

## Required Language

S06 returns structured values rather than free-form product prose. Every available result must carry the exact strings `model-estimated change in your past data` and `correlation, not proven causation`. The presentation layer must render both unchanged. Output remains a retrospective explanation of association and contains no instruction, prediction, medical guidance, or present-day action language.

## Mandatory Deliverables and Acceptance

- `src/model/counterfactual.py` must implement the frozen types, deeply immutable registry, lightweight refit helper, `build_counterfactual_context`, and pure generator. The builder is not optional and no alternate product row-construction path is allowed.
- `tests/test_counterfactual.py` must cover builder reader call counts and read-only boundary; record validation, eligibility, and all tie-breaks; version-to-alpha mapping; count/version/cutoff failure; byte-exact canonical hashes, numeric normalization, target separation, and contradictory mood/target state; stored-gate rejection and same-cohort baseline recomputation; registry deep immutability; all suppression precedence; zero-or-200 delta refits apart from baseline-fold refits; result/provenance state invariants; deterministic candidates, thresholds, quantiles, sample reuse, and tie-break; provider exclusion; and both exact strings.
- Both required S06 autonomous review artifacts must verify the as-of/provider/statistical/schema/language contract. The slice must also pass `python -m pytest tests/test_counterfactual.py -q`, `python scripts/check_autonomous_review_exists.py S06`, and `python scripts/check_no_tracked_data.py`.

# S04 Feature Engineering Autonomous Brief

Autonomy profile: guarded zero-supervision for S04 only.

Manual gates are forbidden for this autonomous run. Any former signoff must be represented as an `autonomous_gate_review` artifact, deterministic tests, and recorded evidence.

## Scope

S04 builds feature engineering for the Health Data Hub v1 Sleep + Mood Retrospective Explainer. It consumes completed S01 warehouse foundations, S02 mood ingestion, and the active S03 provider decision. It must not build ingestion-provider decisions, model training, UI, recommendations, predictions, or hosted infrastructure.

## Provider Decision Contract

- S04 must read the active S03 provider decision before implementing feature code.
- The active S03 provider decision resolves sleep ingestion to Oura-only v1 through direct Oura API v2 periodic pull.
- Oura-only v1 is the first-class S04 feature-engineering source.
- S04 must not require pyEight evidence.
- 8 Sleep must remain absent/fallback unless an explicit future slice supersedes the S03 fallback decision.
- S04 may include source-merge diagnostics only as Oura-only identity checks. It must not average, blend, or reconcile 8 Sleep values into v1 features.
- S04 must not write provider-evidence or ingestion-decision files except read-only consult references in docs or tests.

8 Sleep / pyEight is fallback-only for v1. S04 must treat the active S03 provider decision as Oura-only v1. Feature construction must ignore 8 Sleep rows for v1 model features even if 8 Sleep rows exist in the warehouse. Diagnostics may record that 8 Sleep rows were present and ignored under fallback. 8 Sleep must not be averaged, blended, reconciled, used as fallback HRV, used as fallback sleep stage source, or counted as an active sleep source unless a future explicit provider-reopening slice supersedes S03.

## Required Feature Invariants

- v1 target is same-day evening `feeling[D]`.
- Sleep features for `feeling[D]` come from sleep ending on morning `D`.
- `prior_day_feeling` is `feeling[D-1]`.
- Model features are exactly `total_sleep_min`, `hrv_z`, `deep_sleep_pct`, and `prior_day_feeling`.
- `hrv_avg_ms` is display metadata only.
- `hrv_z` must be prior-only and persisted.
- No sleep forward-fill for training.
- Mood labels are never imputed.

## Required Policy

- Never emit active `manual_gate` rows.
- Never call `keel-run mark-manual-gate`.
- Use narrow repo-relative write roots only.
- Keep raw health data, secrets, tokens, quarantine payloads, snapshots, and DuckDB files out of git and general logs.
- Preserve the retrospective-only v1 scope and statistical gates from the design document.
- S04 launch is blocked unless `python scripts/verify_s04_readiness.py --json` passes.

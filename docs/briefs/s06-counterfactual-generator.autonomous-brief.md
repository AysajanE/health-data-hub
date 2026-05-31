# S06 Counterfactual Generator Autonomous Brief

Autonomy profile: guarded zero-supervision for S06 only.

Manual gates are forbidden. High-risk counterfactual work must use autonomous_gate_review artifacts, deterministic tests, and recorded evidence instead of human signoff.

## Scope

S06 creates retrospective counterfactual explanations for v1. It must not introduce recommendations, predictions, autopilot language, or active 8 Sleep controls.

## Provider Policy Requirements

- The only mutable v1 counterfactual feature is `total_sleep_min`.
- `total_sleep_min` must come from Oura-derived eligible S04 rows under the Oura-only v1 policy.
- 8 Sleep temperature, Autopilot, bed controls, room temperature, Pod controls, sleep score, sleep stages, and HRV must not be counterfactual inputs or action targets.
- 8 Sleep / pyEight remains fallback-only unless a future explicit provider-reopening slice supersedes S03.

## Required Language

Counterfactuals must be retrospective and use `model-estimated change in your past data` plus `correlation, not proven causation`. Do not use prospective intervention language.

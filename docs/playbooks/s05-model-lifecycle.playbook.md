# S05 Model Lifecycle markdown_playbook_v1

This playbook plans the S05 high-risk autonomous model-lifecycle slice for Health Data Hub. It implements the v1 retrospective model lifecycle from S04 feature rows while preserving the active Oura-only v1 provider policy and keeping 8 Sleep as fallback-only. The scope, deliverables, write roots, and verification expectations come from `docs/gstack/s05-model-lifecycle-autoplan.md`; the guarded zero-supervision requirements come from `docs/briefs/s05-model-lifecycle.autonomous-brief.md`; the model semantics come from `docs/gstack/health-data-hub-office-hours.md`; and the markdown contract comes from `automation/task_packs/gstack_design_to_po_playbook/corpus/markdown_playbook_v1_contract.md`.

This playbook does not execute plan-orchestrator, does not claim plan-orchestrator verification has passed, does not create active manual gates, and uses `autonomous_gate_review` artifacts plus deterministic verification for the high-risk autonomous evidence path.

## 1. Phase Overview

S05 owns model lifecycle implementation only:

- Provider-policy preflight before model training.
- Exact four-feature Ridge model training for `total_sleep_min`, `hrv_z`, `deep_sleep_pct`, and `prior_day_feeling`.
- Bootstrap sign stability with 200 resamples and the 90 percent and 80 percent tiers.
- Conservative 90 percent prediction intervals with a 2.0 mood-point full-width floor until N is at least 60.
- Rolling 7-day mean and prior-day baselines.
- N_model-dependent walk-forward baseline gate.
- Nightly retrain entrypoint with graceful insufficient-data no-op behavior.
- `models/eval.jsonl` runtime logging and ignored model-artifact hygiene.
- Autonomous review artifacts documenting gate behavior and statistical validity.

S05 explicitly does not own:

- Counterfactual generation.
- Read API or Streamlit UI.
- launchd plist installation.
- Backups or restore.
- Warehouse schema changes.
- Ingestion changes.
- Provider-policy changes beyond local verification.
- Dependency manifest edits.
- New model features beyond the four fixed v1 features.
- Causal, medical, prospective, recommendation, or customer-facing claims.

Execution ordering:

1. Establish provider-policy preflight and tests.
2. Implement Ridge predictor behavior.
3. Implement baseline gate behavior.
4. Implement retrain orchestration and eval logging.
5. Produce autonomous review evidence after implementation commands exist.

## 2. Execution Items

| step_id | phase | action | why_now | owner_type | prerequisites | repo_surfaces | deliverable | exit_criteria | allowed_write_roots | requires_red_green | required_verification_commands |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 01 | Provider Policy Preflight | Implement the S05 provider-policy preflight and provider-policy tests, including fail-closed checks for Oura-only active sleep policy, 8 Sleep fallback-only status, no `sleep_source_count` above 1 for model rows, no training input with `source` equal to `8sleep`, no `hrv_merge_method` equal to `eight_fallback`, and the required `test_model_training_excludes_8sleep_under_oura_only_v1` synthetic-fixture test. | Provider policy must be established before any model training path exists or runs. | agent | none | docs/gstack/s05-model-lifecycle-autoplan.md; docs/briefs/s05-model-lifecycle.autonomous-brief.md; docs/gstack/health-data-hub-office-hours.md | scripts/verify_s05_provider_policy.py; tests/model/test_provider_policy.py | Preflight is deterministic and local-first, emits JSON status for allowed state, fails closed when provider-policy evidence is missing or invalid, uses no external provider calls, and provider tests prove inactive 8 Sleep fallback rows cannot affect model training eligibility. | scripts/verify_s05_provider_policy.py; tests/model/test_provider_policy.py | true | python scripts/verify_s05_provider_policy.py --json<br>python -m pytest tests/model/test_provider_policy.py -q |
| 02 | Ridge Model Core | Implement `RidgePredictor` with StandardScaler plus Ridge alpha 1.0, exact four-feature enforcement for `total_sleep_min`, `hrv_z`, `deep_sleep_pct`, and `prior_day_feeling`, fit and predict behavior, deterministic linear contribution calculations for the fixed scaled features, bootstrap sign stability with 200 resamples on `feature_date`, tiers at 90 percent and 80 percent, and bootstrap 90 percent prediction intervals with a 2.0 mood-point full-width floor until N is at least 60. | The core predictor must exist before baseline walk-forward evaluation and retrain orchestration can be built against it. | agent | 01 | docs/gstack/s05-model-lifecycle-autoplan.md; docs/briefs/s05-model-lifecycle.autonomous-brief.md; docs/gstack/health-data-hub-office-hours.md; scripts/verify_s05_provider_policy.py; tests/model/test_provider_policy.py | src/model/ridge.py; tests/model/test_ridge.py | Ridge tests pass on synthetic data, reject any feature set other than the four v1 features, keep `hrv_avg_ms` out of model features, compute stable sign tiers, compute contribution values for later consumers, and enforce conservative interval width behavior. | src/model/ridge.py; tests/model/test_ridge.py | true | python -m pytest tests/model/test_ridge.py -q |
| 03 | Baseline Gate | Implement rolling 7-day mean and prior-day baselines, walk-forward evaluation, N_model-dependent evaluation windows, minimum 30 prior model-ready days per fold, the 0.95 RMSE ratio gate against the better baseline, and the 65 percent better-day count gate using the ceil rule. | The model must earn the right to expose outputs, and retrain logging depends on a reusable gate decision. | agent | 02 | docs/gstack/s05-model-lifecycle-autoplan.md; docs/gstack/health-data-hub-office-hours.md; src/model/ridge.py | src/model/baseline_gate.py; tests/model/test_baseline_gate.py | Baseline-gate tests cover no gate below N_model 37, last 7 eval days for N_model 37 through 43, last 14 eval days at N_model 44 and above, fold training size constraints, RMSE ratio failure cases, and better-day count failure cases. | src/model/baseline_gate.py; tests/model/test_baseline_gate.py | true | python -m pytest tests/model/test_baseline_gate.py -q |
| 04 | Retrain Entrypoint And Eval Log | Implement the eval JSONL writer and nightly retrain entrypoint that runs provider preflight before training, loads verified S04 model-ready feature rows when available, no-ops gracefully below N_model 30 by appending a skipped eval record and exiting zero, trains when eligible, evaluates baseline gate and ablations, implements persistence for fitted model plus scaler under the ignored runtime artifact location when invoked, and appends schema-complete eval records with sign-stability tiers and contribution values. | Retrain orchestration must come after provider policy, predictor behavior, and baseline-gate logic because it composes all three into the S05 lifecycle entrypoint. | agent | 01-03 | docs/gstack/s05-model-lifecycle-autoplan.md; docs/briefs/s05-model-lifecycle.autonomous-brief.md; docs/gstack/health-data-hub-office-hours.md; scripts/verify_s05_provider_policy.py; src/model/ridge.py; src/model/baseline_gate.py; tests/model/test_provider_policy.py; tests/model/test_ridge.py; tests/model/test_baseline_gate.py | src/model/eval_log.py; scripts/retrain_model.py; tests/model/test_retrain_entrypoint.py | Entrypoint tests prove preflight runs before training, insufficient data writes a skipped record and exits zero without requiring an existing model file, eligible synthetic data trains and logs gate metrics, eval records include ablation fields plus sign-stability and contribution outputs, runtime model artifacts remain untracked, and the model-artifact ignore rule is asserted without editing ignore policy. | src/model/eval_log.py; scripts/retrain_model.py; tests/model/test_retrain_entrypoint.py | true | python -m pytest tests/model/test_retrain_entrypoint.py -q<br>python -m pytest tests/model -q<br>python scripts/verify_s05_provider_policy.py --json<br>python scripts/check_no_tracked_data.py<br>python -c "from pathlib import Path; lines={l.strip() for l in Path('.gitignore').read_text(encoding='utf-8').splitlines()}; assert 'models/' in lines, 'models/ ignore entry missing'" |
| 05 | Autonomous Review Evidence | Produce S05 autonomous model review artifacts covering model-gate behavior and statistical validity, with `autonomous_gate_review` evidence, deterministic command references, privacy handling for derived health artifacts, retrospective framing, correlational limitations, and no active manual gate. | High-risk model work needs autonomous evidence only after the implementation and verification commands exist, so the review artifacts can describe actual checks rather than speculative acceptance. | agent | 01-04 | docs/gstack/s05-model-lifecycle-autoplan.md; docs/briefs/s05-model-lifecycle.autonomous-brief.md; docs/gstack/health-data-hub-office-hours.md; scripts/verify_s05_provider_policy.py; scripts/retrain_model.py; src/model/ridge.py; src/model/baseline_gate.py; src/model/eval_log.py; tests/model/test_provider_policy.py; tests/model/test_ridge.py; tests/model/test_baseline_gate.py; tests/model/test_retrain_entrypoint.py | docs/reviews/s05-autonomous-model-gate-review.md; docs/reviews/s05-autonomous-statistical-validity-review.md | Both review docs include the literal `autonomous_gate_review`, record deterministic command evidence and limitations, describe baseline-gate and statistical-validity behavior with correlation-only wording, and the autonomous review checker plus full S05 verification commands pass. | docs/reviews/s05-autonomous-model-gate-review.md; docs/reviews/s05-autonomous-statistical-validity-review.md | false | python scripts/check_autonomous_review_exists.py S05<br>python -c "from pathlib import Path; a=Path('docs/reviews/s05-autonomous-model-gate-review.md').read_text(encoding='utf-8').lower(); b=Path('docs/reviews/s05-autonomous-statistical-validity-review.md').read_text(encoding='utf-8').lower(); assert 'autonomous_gate_review' in a and 'autonomous_gate_review' in b; assert 'baseline gate' in a; assert 'statistical' in b; assert 'deterministic' in a and 'deterministic' in b; assert 'retrospective' in a and 'retrospective' in b; assert 'correlational' in a and 'correlational' in b; assert 'derived health' in a and 'derived health' in b; assert 'limitations' in a and 'limitations' in b"<br>python scripts/verify_s05_provider_policy.py --json<br>python -m pytest tests/model -q<br>python scripts/check_no_tracked_data.py |

## 3. Phase Details

### 3.1 Row 01 Provider Policy Preflight

Purpose:

- Establish the S05-owned fail-closed preflight before any model training path is created.
- Preserve the active v1 Oura-only sleep-provider policy.
- Keep 8 Sleep and pyEight fallback-only for S05 model training.
- Add or preserve the required synthetic-fixture test named `test_model_training_excludes_8sleep_under_oura_only_v1`.

Implementation details:

- `scripts/verify_s05_provider_policy.py` must be deterministic and local-first.
- It must emit JSON for the allowed state.
- It must fail closed if required provider-policy evidence is missing or invalid.
- It must not call external provider services.
- It must not mutate warehouse, ingestion, provider, secret, or data state.
- `tests/model/test_provider_policy.py` must prove that inactive 8 Sleep fallback rows cannot influence model training eligibility.
- Training eligibility must reject rows with `sleep_source_count` above 1.
- Training eligibility must reject rows using `source` equal to `8sleep`.
- Training eligibility must reject rows using `hrv_merge_method` equal to `eight_fallback`.

Manual gate handling:

- No active manual gate is emitted.
- Provider-policy uncertainty is handled by fail-closed local checks and deterministic tests.
- If S03 or S04 prerequisite evidence is unavailable during execution, the preflight must fail closed rather than broadening S05 scope.

Verification commands:

- `python scripts/verify_s05_provider_policy.py --json`
- `python -m pytest tests/model/test_provider_policy.py -q`

### 3.2 Row 02 Ridge Model Core

Purpose:

- Implement the reusable predictor that later baseline and retrain rows compose.
- Enforce the fixed v1 feature set for S05.
- Persist contribution and sign-stability behavior for later consumers without implementing UI.

Implementation details:

- `src/model/ridge.py` must implement `RidgePredictor`.
- The predictor must use StandardScaler plus Ridge alpha 1.0.
- The model features must be exactly `total_sleep_min`, `hrv_z`, `deep_sleep_pct`, and `prior_day_feeling`.
- `hrv_avg_ms` must remain display metadata only and must not become a model feature.
- Bootstrap sign stability must use 200 resamples on `feature_date` with replacement.
- Sign-stability tiers must follow the 90 percent and 80 percent thresholds.
- Prediction intervals must use a 90 percent interval.
- Until N is at least 60, prediction intervals must enforce the 2.0 mood-point full-width floor.
- Tests should use synthetic model-ready rows and must not require real personal health data.

Manual gate handling:

- No active manual gate is emitted.
- Statistical risk is controlled through deterministic tests for feature enforcement, stability tiers, contribution calculations, and conservative interval behavior.
- The row must not weaken model constraints to make later UI output more interesting.

Verification command:

- `python -m pytest tests/model/test_ridge.py -q`

### 3.3 Row 03 Baseline Gate

Purpose:

- Implement the gate that decides whether the retrospective model has earned the right to expose outputs to later slices.
- Keep S05 responsible for gate computation and persistence only, not UI display.

Implementation details:

- `src/model/baseline_gate.py` must implement rolling 7-day mean and prior-day baselines.
- N_model below 37 must produce no baseline gate pass.
- N_model from 37 through 43 must evaluate the last 7 days.
- N_model 44 and above must evaluate the last 14 days.
- Every walk-forward fold must train on at least 30 prior model-ready days.
- The ridge walk-forward RMSE must be at most 0.95 times the better baseline RMSE.
- Ridge must beat the best baseline on at least 65 percent of eval days using the ceil rule.
- Tests must cover insufficient N_model, evaluation-window boundaries, fold training-size constraints, RMSE-ratio failures, and better-day-count failures.

Manual gate handling:

- No active manual gate is emitted.
- Baseline-gate acceptance is deterministic and non-UI-facing in S05.
- A failing baseline gate is a valid result and must not be bypassed.

Verification command:

- `python -m pytest tests/model/test_baseline_gate.py -q`

### 3.4 Row 04 Retrain Entrypoint And Eval Log

Purpose:

- Compose provider preflight, model training, baseline evaluation, ablation logging, model persistence, and eval JSONL writing into the S05 lifecycle entrypoint.
- Ship the script that later launchd work can invoke without S05 installing launchd.

Implementation details:

- `src/model/eval_log.py` must append schema-complete records to the runtime eval log.
- `scripts/retrain_model.py` must run `python scripts/verify_s05_provider_policy.py --json` before model training.
- When N_model is below 30, the retrain entrypoint must append a skipped eval record and exit zero.
- No model file is required before enough model-ready rows exist.
- When eligible, the retrain entrypoint must train the fitted model and scaler, evaluate the baseline gate, compute ablation metrics, persist runtime model artifacts under the ignored runtime artifact location, and append a schema-complete eval record.
- Eval records must include ablation fields, sign-stability outputs, contribution outputs, model metadata, and feature metadata where available.
- Runtime artifacts under `models/` contain derived health data and must not be tracked.
- This row must only assert the existing `models/` ignore rule by verification command. It must not edit ignore policy.

Manual gate handling:

- No active manual gate is emitted.
- Derived-health-data risk is handled by ignored runtime artifacts, no-tracked-data verification, synthetic tests, and the later `autonomous_gate_review` docs.
- If helper validators are missing, dependency libraries are unavailable, or S04 feature access is unavailable, execution must fail under current scope rather than widening write roots.

Verification commands:

- `python -m pytest tests/model/test_retrain_entrypoint.py -q`
- `python -m pytest tests/model -q`
- `python scripts/verify_s05_provider_policy.py --json`
- `python scripts/check_no_tracked_data.py`
- `python -c "from pathlib import Path; lines={l.strip() for l in Path('.gitignore').read_text(encoding='utf-8').splitlines()}; assert 'models/' in lines, 'models/ ignore entry missing'"`

### 3.5 Row 05 Autonomous Review Evidence

Purpose:

- Produce the required high-risk autonomous evidence artifacts after the implementation and verification commands exist.
- Document gate behavior and statistical validity without adding executable model behavior.

Implementation details:

- `docs/reviews/s05-autonomous-model-gate-review.md` must cover provider preflight behavior, N_model windows, baseline gate criteria, eval logging, artifact hygiene, and deterministic command evidence.
- `docs/reviews/s05-autonomous-statistical-validity-review.md` must cover the fixed feature set, no sleep forward-fill for training, no mood-label imputation, bootstrap sign stability, conservative prediction intervals, ablations, limitations, and non-causal framing.
- Both review docs must include the literal `autonomous_gate_review`.
- Both review docs must avoid raw health data, tokens, and derived health values.
- Both review docs must frame the S05 model as retrospective and correlational.
- The docs must not claim medical validity, causality, prospective prediction, or intervention recommendations.

Manual gate handling:

- No active manual gate is emitted.
- The `autonomous_gate_review` artifacts are the high-risk autonomous evidence path.
- The row must record deterministic evidence and limitations without treating the review docs as a substitute for running commands.

Verification commands:

- `python scripts/check_autonomous_review_exists.py S05`
- `python -c "from pathlib import Path; a=Path('docs/reviews/s05-autonomous-model-gate-review.md').read_text(encoding='utf-8').lower(); b=Path('docs/reviews/s05-autonomous-statistical-validity-review.md').read_text(encoding='utf-8').lower(); assert 'autonomous_gate_review' in a and 'autonomous_gate_review' in b; assert 'baseline gate' in a; assert 'statistical' in b; assert 'deterministic' in a and 'deterministic' in b; assert 'retrospective' in a and 'retrospective' in b; assert 'correlational' in a and 'correlational' in b; assert 'derived health' in a and 'derived health' in b; assert 'limitations' in a and 'limitations' in b"`
- `python scripts/verify_s05_provider_policy.py --json`
- `python -m pytest tests/model -q`
- `python scripts/check_no_tracked_data.py`

### 3.6 Autonomous Gate List

The high-risk autonomous evidence path is represented through deterministic checks and `autonomous_gate_review` artifacts:

1. Provider-policy gate: row 01 preflight plus row 04 pre-training invocation.
2. Feature-invariant gate: row 02 exact feature enforcement and metadata exclusion tests.
3. Baseline-evaluation gate: row 03 N_model windows, RMSE ratio, and better-day count tests.
4. Retrain-orchestration gate: row 04 no-op behavior, eval logging, ablations, artifact hygiene, and full model tests.
5. Autonomous evidence gate: row 05 review docs and autonomous review checker.

No interactive stop-point rows are emitted for S05. Uncertainty is handled by fail-closed checks, skipped retrain behavior where specified, deterministic tests, and autonomous evidence artifacts.

## 4. Shared Guidance

### 4.1 Verification List

The execution table is the row-level source of required verification. This list repeats the commands for operator clarity.

Provider-policy verification:

- `python scripts/verify_s05_provider_policy.py --json`
- `python -m pytest tests/model/test_provider_policy.py -q`

Ridge predictor verification:

- `python -m pytest tests/model/test_ridge.py -q`

Baseline-gate verification:

- `python -m pytest tests/model/test_baseline_gate.py -q`

Retrain entrypoint and eval-log verification:

- `python -m pytest tests/model/test_retrain_entrypoint.py -q`
- `python -m pytest tests/model -q`
- `python scripts/verify_s05_provider_policy.py --json`
- `python scripts/check_no_tracked_data.py`
- `python -c "from pathlib import Path; lines={l.strip() for l in Path('.gitignore').read_text(encoding='utf-8').splitlines()}; assert 'models/' in lines, 'models/ ignore entry missing'"`

Autonomous review evidence verification:

- `python scripts/check_autonomous_review_exists.py S05`
- `python -c "from pathlib import Path; a=Path('docs/reviews/s05-autonomous-model-gate-review.md').read_text(encoding='utf-8').lower(); b=Path('docs/reviews/s05-autonomous-statistical-validity-review.md').read_text(encoding='utf-8').lower(); assert 'autonomous_gate_review' in a and 'autonomous_gate_review' in b; assert 'baseline gate' in a; assert 'statistical' in b; assert 'deterministic' in a and 'deterministic' in b; assert 'retrospective' in a and 'retrospective' in b; assert 'correlational' in a and 'correlational' in b; assert 'derived health' in a and 'derived health' in b; assert 'limitations' in a and 'limitations' in b"`
- `python scripts/verify_s05_provider_policy.py --json`
- `python -m pytest tests/model -q`
- `python scripts/check_no_tracked_data.py`

### 4.2 PO Post-Output Checks

After plan-orchestrator produces a worktree diff, the operator must verify:

- The diff touches only the deliverable paths listed in the execution table.
- No source changes appear under unlisted roots.
- No dependency manifests, warehouse files, ingestion files, API files, UI files, launchd files, backup files, secret files, data files, or ops-state files were changed.
- No runtime model artifact is tracked as source.
- Every command listed in each row’s `required_verification_commands` cell is run in row order after its row completes.
- Row 04 confirms the model-artifact ignore rule by command only and does not edit ignore policy.
- Row 05 review docs contain `autonomous_gate_review`, deterministic command evidence, retrospective framing, correlational limitations, and derived health artifact handling.
- Helper commands required by `docs/gstack/s05-model-lifecycle-autoplan.md` either pass or the slice remains incomplete under current scope.
- If the repository Python environment lacks required libraries, dependency edits are not added by this playbook because dependency manifests are outside S05 write roots.

### 4.3 Parser And Formatting Guidance

- Keep the execution table columns exactly as shown in Section 2.
- Do not add plan-orchestrator derived columns.
- Do not add same-row or future deliverables to `repo_surfaces`.
- Keep `allowed_write_roots` semicolon-separated.
- Keep `prerequisites` to `none`, exact step ids, comma-separated exact step ids, or numeric ranges.
- Avoid broad roots, absolute paths, private-data paths, operational dot roots, and secret-bearing paths.
- Do not widen S05 scope for convenience if helper scripts, dependencies, schema access, or prerequisite evidence are missing.
- Do not claim plan-orchestrator doctor or execution verification has passed unless actual output is available to the operator.

## 5. Risks And Contingencies

### 5.1 S03 And S04 Prerequisite Evidence

Risk:

- The S05 sources require S03 and S04 completion before model training, but this playbook does not assert that completion.

Contingency:

- Row 01 and row 04 must fail closed or skip safely if required provider-policy or S04 feature evidence is unavailable.
- Tests should use synthetic fixtures so S05 model code can be verified without personal data.
- Do not modify warehouse, ingestion, or provider-policy implementation outside S05 roots to compensate for missing prerequisite evidence.

### 5.2 Unknown Warehouse Access Shape

Risk:

- The broad design defines expected feature rows, but actual warehouse implementation files were not attached for this stage.

Contingency:

- S05 must not modify warehouse schema or ingestion code.
- If the expected S04 feature contract is unavailable, row 04 must not work around it by writing outside S05 roots.
- Training must remain fail-closed rather than silently accepting unverified feature rows.

### 5.3 Helper Validator Availability

Risk:

- The S05 autoplan requires `python scripts/check_autonomous_review_exists.py S05` and `python scripts/check_no_tracked_data.py`, but their implementations were not attached in this stage.

Contingency:

- The commands remain required because the S05 autoplan requires them.
- If a helper command is absent during execution, treat that as a repo prerequisite failure under current scope.
- Do not create or edit helper validators outside the listed S05 write roots.

### 5.4 Dependency Availability

Risk:

- S05 requires Ridge and StandardScaler behavior plus pytest-based verification, but dependency manifests were not attached in this stage.

Contingency:

- Do not add dependency-manifest edits in S05.
- If required libraries are unavailable, execution should stop under current scope.
- Do not replace the specified model behavior with an unsupported implementation merely to avoid dependency issues.

### 5.5 Derived Health Artifact Hygiene

Risk:

- Model artifacts and eval logs contain derived health data and must not be tracked.

Contingency:

- Keep runtime artifacts under the ignored runtime artifact location.
- Use row 04 verification to assert the ignore rule.
- Run `python scripts/check_no_tracked_data.py`.
- Do not place raw health data or derived health values in review docs.
- Do not list `models/` as a tracked source deliverable.

### 5.6 Baseline Gate Outcome

Risk:

- This playbook requires implementation of the baseline gate and persistence of its decision. It does not assert that the gate will pass on real data.

Contingency:

- Preserve the gate exactly as specified.
- Do not weaken N_model windows, RMSE ratio, better-day count, bootstrap stability, or interval-floor behavior.
- A failed gate or suppressed downstream output is a valid trust-preserving outcome.

### 5.7 Review Doc Scope

Risk:

- Row 05 writes evidence docs only, and evidence docs can become misleading if they overstate model meaning.

Contingency:

- Review docs must record deterministic command evidence and limitations.
- They must keep the model framed as retrospective and correlational.
- They must not claim medical validity, causality, prospective predictions, or interventions.
- They must not include raw health data, secrets, provider payloads, or runtime model artifacts.

### 5.8 Ignore Policy Scope

Risk:

- S05 revision 3 states the `models/` exclusion already exists and removes ignore-policy editing from allowed write roots.

Contingency:

- No execution row may list ignore policy as a repo surface, deliverable, or allowed write root.
- Row 04 may only read and assert the model-artifact ignore rule through the verification command.
- If the assertion fails, the slice remains incomplete under current scope rather than editing ignore policy.

## 6. Immediate Next Actions

1. Save this artifact under `docs/playbooks/s05-model-lifecycle.playbook.md`.
2. Run PO `list-items` for `docs/playbooks/s05-model-lifecycle.playbook.md`.
3. Run PO `doctor --playbook docs/playbooks/s05-model-lifecycle.playbook.md`.
4. Review the PO `doctor` output and do not treat the playbook as validated unless the operator has actual passing output.
5. If `doctor` reports parser, column, write-root, prerequisite, or verification-command issues, revise only the playbook artifact and rerun `doctor`.
6. After PO accepts the playbook structure, execute rows in order from `01` through `05`.
7. After each row, run every command listed in that row’s `required_verification_commands` cell and keep the command output with the row evidence.
8. If any required command, repository prerequisite, or dependency is unavailable, stop under current scope rather than widening write roots or adding unsupported setup work.

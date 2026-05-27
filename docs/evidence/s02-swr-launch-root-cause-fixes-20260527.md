# S02 SWR Launch Root-Cause Fixes

Timestamp: 2026-05-27T15:25:00-04:00

## Context

Controlled S02 launch used:

```bash
python -m ops.autonomy.autokeel --once --slice S02
```

AutoKeel correctly routed S02 through `keel-swr` because S02 is high-risk `swr_preferred` with a `use_swr` lane decision.

## Issue 1: Missing OpenAI API Key

Root cause:

- `OPENAI_API_KEY` was not available to the AutoKeel process.
- No repo-local `.env` existed.
- `keel-swr` stopped before playbook materialization.

Fix:

- AutoKeel now classifies this as `provider_auth_failure`, not `compile_failure`.
- AutoKeel writes sanitized `blocked_external` evidence under `docs/evidence/`.
- The rerun sources the existing key from `/Users/aeziz-local/keel/tools/staged-workflow-runner/.env` without printing or committing the key.

Verification:

```bash
python -m pytest tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_swr_missing_openai_key_blocks_as_provider_auth_failure -q
```

Result: pass.

## Critical Drift: SWR Validation Failure Triggered Full Workflow Rerun

Root cause:

- The terminal S02 SWR run completed Stage 5, but the emitted
  `markdown_playbook_v1` playbook failed AutoKeel's autonomous validator.
- AutoKeel handled that validation failure through the generic playbook replan
  path, archived the playbook, marked S02 `replan_required`, and made the next
  tick eligible to call a fresh `keel-swr run --run-name ... --output-root ...`.
- That was fundamentally wrong for SWR. A failed terminal artifact must be
  traced back to the stage where the contract drift entered the handoff, then
  repaired from that stage only.
- Artifact review showed the drift entered at Stage 4: the Stage 4 gate and
  contract review accepted the old execution table without
  `required_verification_commands` or `autonomous_gate_review`. Stage 5 then
  faithfully emitted the bad Stage 4 handoff.

Fix:

- AutoKeel now handles SWR playbook validation failures with a dedicated repair
  path, not generic `replan_required`.
- The rejected playbook is archived, the source SWR `run_manifest` and
  validator errors are persisted in `swr_validation_repair`, and S02 is blocked
  as `blocked_compile_inputs`.
- The repair planner diagnoses whether Stage 4 or Stage 5 is the smallest safe
  rerun point. If Stage 4 is the source, only Stage 4 and downstream Stage 5 are
  reset to `prepared`; prior stages remain untouched.
- Future repair execution is blocked until explicitly authorized and, when
  authorized, can only use `keel-swr run --run-dir ... --stage ...` with the
  required approved review bundle. It must not use `--run-name` or
  `--output-root`.
- The Keel staged-workflow-runner task-pack source was also corrected so Stage
  3, Stage 4, Stage 5, shared instructions, and the contract corpus all require
  `required_verification_commands`.

Verification:

```bash
python -m pytest tests/autonomy/test_validate_playbook_autonomous.py -q
python -m pytest tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_s02_swr_output_must_validate_before_po_start tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_swr_validation_failure_does_not_fresh_rerun_full_workflow tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_reset_swr_manifest_for_stage_rerun_invalidates_target_and_downstream_only tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_swr_task_pack_materializes_under_manifest_root -q
```

Result: pass.

## Issue 11: New SWR Run Inherited Stale Monitor Timestamp

Root cause:

- After the stale-manifest fixes, AutoKeel correctly launched a fresh S02 SWR
  run at `2026-05-27T17:49:40-04:00`.
- The fresh run reached Stage 1 `in_progress` and was recorded as
  `active_swr_run`.
- `record_active_swr_run` preserved `last_remote_check_at` and
  `supervisor_session_id` from the previous stale run solely because the slice
  id matched.
- That would make the next monitor interval anchor to old state instead of the
  fresh run, and could trigger an early remote check despite the 300-second
  cadence.

Fix:

- AutoKeel now preserves monitor and supervisor fields only when the previous
  active SWR state matches the same run id or manifest path.
- When a new live SWR launch returns an in-progress wait timeout, AutoKeel
  records that command result as the current run's `last_remote_check_at`.
- The already-written S02 runtime state was corrected so the active run's
  monitor timestamp is `2026-05-27T17:49:52-04:00`, matching the actual launch
  observation, and its stale supervisor session id is cleared.

Verification:

```bash
python -m pytest tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_swr_wait_timeout_in_progress_records_active_run_not_compile_failure -q
python -m py_compile ops/autonomy/autokeel.py scripts/verify_s02_readiness.py
git diff --check
```

Result: pass.

## Issue 7: SWR Continuation Dropped Original Primary Job Inputs

Root cause:

- Stage 1 review completed and AutoKeel created an approved review bundle.
- AutoKeel then continued the same SWR run with `--review-bundle` but did not pass the original `operator_overrides.primary_job_inputs`.
- The staged-workflow-runner requires the workflow's minimum primary job inputs on continuation, so it rejected the Stage 2 launch with `workflow requires at least 1 primary job input(s), got 0`.

Fix:

- AutoKeel now carries forward the original primary job inputs and reference context from the run manifest when continuing an SWR run after review-bundle approval.
- The review-lane regression test now fails if AutoKeel attempts SWR continuation without both `--review-bundle` and `--primary-job-input`.

Verification:

```bash
python -m pytest tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_swr_waiting_for_review_runs_supervisor_review_lane_before_next_stage -q
```

Result: pass.

## Issue 8: Approved Stage Review Bundle Was Not Idempotently Reused

Root cause:

- A previous AutoKeel tick created an approved Stage 1 review bundle, then the
  SWR continuation failed before Stage 2 launch because primary job inputs were
  missing.
- On retry, AutoKeel saw the manifest still at `waiting_for_review` and reran
  the Stage 1 supervisor review lane instead of reusing the already-approved
  hash-bound bundle for the same run, stage, and artifacts.
- This did not skip a required review, but it duplicated reviewer work and
  created avoidable audit noise.

Fix:

- AutoKeel now checks for an existing approved SWR review bundle before running
  the supervisor lane.
- The bundle is reused only when it is approved, matches the current run id and
  stage id, points to the current stage markdown/JSON artifacts, and its
  recorded hashes still match the local artifacts.
- If the bundle is missing, rejected, stale, mismatched, or hash-invalid,
  AutoKeel falls back to the full supervisor review lane.

Verification:

```bash
python -m pytest tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_swr_waiting_for_review_reuses_existing_approved_bundle -q
```

Result: pass.

## Issue 9: SWR Task Pack Contract Drifted From AutoKeel Validator

Root cause:

- The SWR gstack-to-playbook task pack still told Stage 5 to emit the older
  `markdown_playbook_v1` table with no `required_verification_commands`
  column.
- AutoKeel's autonomous validator is stricter: high-risk slices must include
  the literal `autonomous_gate_review` term and every execution row must have
  non-empty `required_verification_commands`.
- Stage 5 therefore produced a structurally coherent SWR playbook that
  predictably failed AutoKeel validation before PO.

Fix:

- AutoKeel now appends an explicit autonomous validation overlay when it
  materializes the SWR task pack into the product workspace.
- The overlay is injected into the task-pack contract and Stage 3 through Stage
  5 prompts so the SWR run sees the stricter AutoKeel validator contract before
  drafting, hardening, and finalizing execution rows.
- The overlay requires `required_verification_commands` and the
  `autonomous_gate_review` term, while preserving the no-manual-gate and
  no-human-approval rules.

Verification:

```bash
python -m pytest tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_swr_task_pack_materializes_under_manifest_root -q
```

Result: pass.

## Issue 10: Prepared SWR Manifest Misclassified As Active

Root cause:

- After the Stage 5 validation failure, the next AutoKeel tick scanned local
  SWR manifests and adopted an older pre-submit manifest with
  `status: created`.
- That manifest had only prepared stages and no submitted response id.
- Treating `created` as active would block a legitimate relaunch and record a
  misleading `swr_run_active` event for a run that was never actually in
  progress.
- S02 readiness also treated historical `waiting_for_review` stage rows inside
  a top-level `completed` manifest as active, even though those rows were the
  audited review checkpoints from an already finished SWR run.

Fix:

- AutoKeel now treats only manifest status `running` or
  `waiting_for_review`, plus submitted/in-progress stages, as active.
- S02 readiness uses the same rule and ignores stale `created` manifests, even
  if old `active_swr_run` state points at them.
- S02 readiness no longer treats historical stage-level
  `waiting_for_review` rows as active unless the top-level manifest is itself
  `waiting_for_review`.
- The 300-second monitor cadence remains reserved for genuinely active SWR
  responses rather than stale prepared run directories.

Verification:

```bash
python -m pytest tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_created_swr_manifest_without_response_does_not_block_relaunch tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_completed_swr_manifest_review_history_does_not_block_relaunch tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_active_swr_manifest_blocks_relaunch -q
python -m py_compile ops/autonomy/autokeel.py scripts/verify_s02_readiness.py
git diff --check
```

Result: pass.

## Issue 6: SWR Supervisor Internal Task Pack Was Not Materialized

Root cause:

- After AutoKeel resumed the active response, Stage 1 reached `waiting_for_review` and classified as `completed_complete_artifact`.
- The SWR supervisor lane then attempted to load `automation/task_packs/responses_runner_v2_supervisor_internal/...` from the product workspace.
- AutoKeel had materialized only the gstack-to-playbook task pack, not the supervisor internal task pack required by `keel-swr supervisor invoke-operator` and reviewer commands.

Fix:

- AutoKeel now materializes `responses_runner_v2_supervisor_internal` from the Keel staged-workflow-runner checkout into the product workspace before any SWR supervisor command.
- The copied supervisor pack remains ignored under `automation/task_packs/`.
- The review-lane regression test uses a fake supervisor pack and verifies that AutoKeel reaches bundle-gated continuation only after supervisor review commands.

Verification:

```bash
python -m pytest tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_swr_waiting_for_review_runs_supervisor_review_lane_before_next_stage -q
```

Result: pass.

## Issue 5: Waiting-For-Review SWR Stage Needed Explicit AutoKeel Routing

Root cause:

- SWR stages 1 through 4 are `review_required`.
- A completed non-terminal stage must stop at `waiting_for_review` until the SWR supervisor lane creates an approved review bundle.
- AutoKeel previously only knew how to handle a final materialized playbook or an active in-progress run; it did not have a dedicated `waiting_for_review` route.

Fix:

- AutoKeel now recognizes active SWR manifests whose current stage is `waiting_for_review`.
- AutoKeel invokes the SWR supervisor lane to classify the stage, run operator/reviewer/consolidation/acceptance steps, create the approved review bundle, and continue the same SWR run with `--review-bundle`.
- AutoKeel records the continued SWR stage as active if the next stage remains in progress after the local wait window.

Verification:

```bash
python -m pytest tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_swr_waiting_for_review_runs_supervisor_review_lane_before_next_stage -q
```

Result: pass.

## Issue 4: SWR Background Wait Timeout Misclassified As Compile Failure

Root cause:

- `keel-swr run --wait --max-wait-seconds 5` successfully submitted Stage 1 as a background Responses API run.
- The local wait window expired while the remote response was still non-terminal (`last_status=in_progress`).
- AutoKeel treated that short local wait timeout as a compile failure, even though the run manifest and stage checkpoint contained a durable response id and resumable in-progress state.
- SWR stages can legitimately take minutes to hours. A short local wait timeout is only a monitoring boundary, not evidence of failure.

Fix:

- AutoKeel now detects the in-progress wait-timeout signature.
- AutoKeel records `active_swr_run` in `ops/autonomy/autonomy_state.json` with the run manifest, run id, current stage, current stage status, and response id.
- AutoKeel marks S02 `waiting_for_playbook` and returns a nonterminal success for that supervisor tick instead of recording `compile_failure`.
- AutoKeel refuses to relaunch SWR while the active run manifest remains `running` or `waiting_for_review`.
- S02 readiness now reports an error when an active S02 SWR run or active local S02 SWR manifest already exists, so operators do not accidentally start a second SWR run before state adoption.

Verification:

```bash
python -m pytest tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_active_swr_manifest_blocks_relaunch tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_swr_wait_timeout_in_progress_records_active_run_not_compile_failure -q
```

Result: pass.

## Issue 3: SWR Token Preflight Sends Unsupported Background Parameter

Root cause:

- After provider auth and task-pack materialization were fixed, `keel-swr` reached request construction.
- The staged-workflow-runner token preflight path called `/responses/input_tokens` with a request payload that included `background`.
- The service rejected token preflight with `400 Bad Request: Unknown parameter: 'background'`.
- The staged-workflow-runner runbook states to use `--skip-token-count` for live runs unless the service-side token-preflight issue is known to be resolved.

Fix:

- AutoKeel SWR policy now sets `skip_token_count: true`.
- AutoKeel includes `--skip-token-count` in the `keel-swr run` command.
- This does not skip playbook validation, SWR evidence recording, PO supervision, review validation, slice verification, or tracked-data checks.

Verification:

```bash
python -m pytest tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_swr_preferred_playbook_generation_routes_through_keel_swr -q
```

Result: pass.
## Issue 2: SWR Task-Pack Manifest Root Mismatch

Root cause:

- AutoKeel materialized the SWR task pack under `.local/autokeel/swr/task_packs/gstack_design_to_po_playbook`.
- The staged-workflow-runner task pack input manifests reference `automation/task_packs/gstack_design_to_po_playbook/...`.
- `keel-swr` therefore could not resolve `automation/task_packs/gstack_design_to_po_playbook/corpus/markdown_playbook_v1_contract.md`.

Fix:

- AutoKeel policy now materializes the task pack at `automation/task_packs/gstack_design_to_po_playbook`.
- AutoKeel invokes the workflow at `automation/task_packs/gstack_design_to_po_playbook/workflows/gstack_design_to_po_playbook.workflow.json`.
- `.gitignore` now ignores `automation/task_packs/` because this is local generated tool runtime material.

Verification:

```bash
python -m pytest tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_swr_task_pack_materializes_under_manifest_root tests/autonomy/test_autokeel_v1_feedback.py::AutoKeelV1FeedbackTests::test_swr_preferred_playbook_generation_routes_through_keel_swr -q
python -m py_compile ops/autonomy/autokeel.py
git diff --check
```

Result: pass.

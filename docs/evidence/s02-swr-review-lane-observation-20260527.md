# S02 SWR Review-Lane Observation

Timestamp: 2026-05-27T15:50:00-04:00

## Scope

This record documents local observation and static workflow inspection only.
No `keel-swr run`, `keel-swr resume`, stage launch, or direct stage advancement
was performed for this observation.

## Current Local State

- Active SWR run manifest:
  `.local/autokeel/swr/runs/2026-05-27_193037_autokeel-s02-20260527t153037-0400_gstack_design_to_po_playbook/run_manifest.json`
- Run id: `run_20260527_193037_b4691e05`
- Response id: `resp_09e4f2aa582ad18e006a17466097f8819187cdcc63a159f6da`
- Current stage: `source_authority_map`
- Local stage status: `in_progress`
- Gate: `review_required`
- Token preflight: `skipped_by_operator`

No local AutoKeel, `keel-swr`, or staged-workflow-runner process is currently
running. The local manifest will not update by itself; future live status
inspection must be performed by AutoKeel on an operator-approved low-cadence
interval and must use resume/refresh semantics rather than duplicate submit.

## Expected SWR Review-Lane Contract

The gstack-to-PO task pack defines five stages:

1. `source_authority_map`
2. `repo_grounding`
3. `execution_row_draft`
4. `gate_and_contract_review`
5. `final_markdown_playbook`

Stages 1 through 4 are `review_required`; Stage 5 is terminal.

The staged-workflow-runner engine contract says a `review_required` stage with a
next stage must stop as `waiting_for_review`. The next stage must not start
until an approved review bundle is supplied.

The SWR supervisor review sequence for every non-terminal stage is:

1. operator Codex provisional review
2. independent read-only Codex review
3. independent read-only Claude review
4. deterministic consolidation
5. operator selective acceptance with evidence
6. approved review bundle creation

## Audit Checks Required After Stage 1 Finalizes

After AutoKeel observes Stage 1 as terminal, future audit must verify:

- the run manifest records Stage 1 as `waiting_for_review`;
- no Stage 2 response id exists before an approved Stage 1 review bundle;
- supervisor review artifacts exist for the Stage 1 review cycle;
- the approved review bundle points to the exact Stage 1 markdown and response
  JSON artifacts and hash-validates them;
- Stage 2, if later started by AutoKeel, consumes the Stage 1 review bundle via
  `--review-bundle`;
- no direct `keel-swr run` duplicate-submit occurred.

## Current Root-Cause Finding

AutoKeel previously treated the short local wait timeout as a compile failure.
That was incorrect because the SWR response was still in progress. AutoKeel now
tracks the active SWR manifest and blocks duplicate launch attempts. The next
behavior to observe is whether AutoKeel performs the required supervisor review
lane when Stage 1 reaches `waiting_for_review`.

## AutoKeel Adoption Observation

Command:

```bash
python -m ops.autonomy.autokeel --once --slice S02
```

Observed behavior:

- AutoKeel recorded `active_swr_run` in `ops/autonomy/autonomy_state.json`.
- AutoKeel marked S02 `waiting_for_playbook`.
- AutoKeel emitted `swr_run_active`.
- AutoKeel did not relaunch `keel-swr run`.

## AutoKeel Review-Lane Hardening

AutoKeel now has an explicit `waiting_for_review` path:

- classify the SWR stage through `keel-swr supervisor classify`;
- invoke the SWR operator and independent reviewer lanes;
- consolidate review output;
- record operator acceptance;
- create the approved review bundle;
- continue the same SWR run with `--review-bundle`.

This path is covered by an autonomy regression test that fails if AutoKeel
continues a waiting-review stage without first creating and supplying the
review bundle.

## Stage 1 Completion Observation

Command:

```bash
set -a && . /Users/aeziz-local/keel/tools/staged-workflow-runner/.env && set +a && \
  python -m ops.autonomy.autokeel --once --slice S02
```

Observed behavior:

- AutoKeel resumed the existing response id, not a duplicate run.
- Stage 1 reached `waiting_for_review`.
- `keel-swr supervisor classify` classified Stage 1 as
  `completed_complete_artifact`.
- AutoKeel attempted to enter the supervisor review lane.

Observed issue:

- The first supervisor review command could not load
  `automation/task_packs/responses_runner_v2_supervisor_internal/commands/operator_codex.command.json`.
- Root cause: AutoKeel had materialized the gstack playbook task pack but not
  the SWR supervisor internal task pack required by `keel-swr supervisor`.
- Fix: AutoKeel now materializes the supervisor internal task pack before any
  supervisor command.

## Stage 1 Review-Lane Completion Observation

After the supervisor internal pack fix, AutoKeel completed the Stage 1 review
lane:

- operator Codex review artifact was created;
- independent Codex review artifact was created;
- independent Claude review artifact was created;
- consolidated review artifact was created;
- operator acceptance artifact was created;
- approved Stage 1 review bundle was created at
  `.local/autokeel/swr/review_lane/S02-run_20260527_193037_b4691e05-source_authority_map/source_authority_map.review_bundle.json`.

Observed continuation issue:

- AutoKeel supplied the review bundle but omitted the original primary job
  inputs when continuing the same SWR run.
- The staged-workflow-runner rejected continuation with
  `workflow requires at least 1 primary job input(s), got 0`.
- Fix: AutoKeel now carries forward original primary job inputs and reference
  context from the run manifest during review-bundle continuation.

## Stage 1 Retry Observation

After the primary-input continuation fix, the next AutoKeel tick was allowed to
operate at the AutoKeel boundary:

```bash
set -a && . /Users/aeziz-local/keel/tools/staged-workflow-runner/.env && set +a && \
  python -m ops.autonomy.autokeel --once --slice S02
```

Observed behavior:

- AutoKeel did not manually run `keel-swr` outside its supervisor loop.
- AutoKeel continued the same SWR run and Stage 2 started as
  `repo_grounding`.
- Stage 2 response id:
  `resp_00084a94ba16f7c1006a1752707200819ebeb2d5b3e93e50f2`.
- Stage 2 remained in progress after the local wait window, and AutoKeel
  recorded it as `active_swr_run` instead of a compile failure.

Observed idempotency issue:

- The retry reran the Stage 1 supervisor review lane even though the approved
  Stage 1 review bundle already existed.
- This did not skip review or launch Stage 2 unreviewed, but it duplicated the
  Stage 1 reviewer artifacts and added audit noise.

Fix:

- AutoKeel now reuses an existing approved review bundle only when the bundle
  matches the same run id, stage id, stage markdown artifact, response JSON
  artifact, reviewer notes, and recorded hashes.
- If that validation fails, AutoKeel runs the full supervisor review lane.

## Stage 2 Review-Lane Observation

After the Stage 3 monitor interval elapsed, AutoKeel was invoked again through
the same operator boundary:

```bash
set -a && . /Users/aeziz-local/keel/tools/staged-workflow-runner/.env && set +a && \
  python -m ops.autonomy.autokeel --once --slice S02
```

Observed behavior:

- AutoKeel resumed Stage 2 `repo_grounding` from response id
  `resp_00084a94ba16f7c1006a1752707200819ebeb2d5b3e93e50f2`.
- Stage 2 reached `completed_complete_artifact`.
- AutoKeel ran the required Stage 2 supervisor review lane before any Stage 3
  launch.
- AutoKeel created an approved Stage 2 review bundle at
  `.local/autokeel/swr/review_lane/S02-run_20260527_193037_b4691e05-repo_grounding/repo_grounding.review_bundle.json`.
- AutoKeel continued the same SWR run with that review bundle.
- Stage 3 `execution_row_draft` started with response id
  `resp_00122f2987f8058c006a17587023d081959f36e75339321cad`.
- Stage 3 remained in progress after the local wait window, and AutoKeel
  recorded it as `active_swr_run` instead of recording a compile failure.

Conclusion:

- AutoKeel is enforcing the SWR review/supervisor lane between Stage 2 and
  Stage 3, not just after Stage 1.

## Monitor Cadence Adjustment

Operator instruction changed the SWR monitor cadence from the previous implicit
900-second default to 300 seconds.

Implementation:

- `ops/autonomy/policy.yaml` now sets `swr.monitor_min_interval_seconds: 300`.
- AutoKeel's fallback default is also 300 seconds if policy omits the key.
- `ops/autonomy/README.md` documents the configured 300-second cadence.

## Stage 3 Review-Lane Observation

After applying the 300-second monitor cadence, AutoKeel was invoked again:

```bash
set -a && . /Users/aeziz-local/keel/tools/staged-workflow-runner/.env && set +a && \
  python -m ops.autonomy.autokeel --once --slice S02
```

Observed behavior:

- AutoKeel resumed Stage 3 `execution_row_draft` from response id
  `resp_00122f2987f8058c006a17587023d081959f36e75339321cad`.
- Stage 3 reached `completed_complete_artifact`.
- AutoKeel ran the required Stage 3 supervisor review lane before any Stage 4
  launch.
- AutoKeel created an approved Stage 3 review bundle at
  `.local/autokeel/swr/review_lane/S02-run_20260527_193037_b4691e05-execution_row_draft/execution_row_draft.review_bundle.json`.
- AutoKeel continued the same SWR run with that review bundle.
- Stage 4 `gate_and_contract_review` started with response id
  `resp_08f1230920c3d9f0006a175e5d117c8195899e924176910dee`.
- Stage 4 remained in progress after the local wait window, and AutoKeel
  recorded it as `active_swr_run` instead of recording a compile failure.

Conclusion:

- AutoKeel is enforcing the SWR review/supervisor lane between Stage 3 and
  Stage 4.

## Stage 4 Review-Lane Observation

After the next 300-second monitor interval, AutoKeel was invoked again:

```bash
set -a && . /Users/aeziz-local/keel/tools/staged-workflow-runner/.env && set +a && \
  python -m ops.autonomy.autokeel --once --slice S02
```

Observed behavior:

- AutoKeel resumed Stage 4 `gate_and_contract_review` from response id
  `resp_08f1230920c3d9f0006a175e5d117c8195899e924176910dee`.
- On the first 300-second check, Stage 4 was still in progress and AutoKeel
  kept it as `active_swr_run` without relaunching.
- On the next 300-second check, Stage 4 reached
  `completed_complete_artifact`.
- AutoKeel ran the required Stage 4 supervisor review lane before any terminal
  Stage 5 launch.
- AutoKeel created an approved Stage 4 review bundle at
  `.local/autokeel/swr/review_lane/S02-run_20260527_193037_b4691e05-gate_and_contract_review/gate_and_contract_review.review_bundle.json`.
- AutoKeel continued the same SWR run with that review bundle.
- Terminal Stage 5 `final_markdown_playbook` started with response id
  `resp_0445b4e98035b0f8006a1763a1db08819d8685093d8fd41979`.
- Stage 5 remained in progress after the local wait window, and AutoKeel
  recorded it as `active_swr_run` instead of recording a compile failure.

Conclusion:

- AutoKeel enforced the SWR review/supervisor lane before the terminal
  markdown playbook stage.

## Stage 5 Completion And Validation Observation

After the next 300-second monitor interval, AutoKeel was invoked again:

```bash
set -a && . /Users/aeziz-local/keel/tools/staged-workflow-runner/.env && set +a && \
  python -m ops.autonomy.autokeel --once --slice S02
```

Observed behavior:

- AutoKeel resumed terminal Stage 5 `final_markdown_playbook` from response id
  `resp_0445b4e98035b0f8006a1763a1db08819d8685093d8fd41979`.
- Stage 5 completed.
- AutoKeel materialized the Stage 5 markdown to
  `docs/playbooks/s02-mood-api.playbook.md`.
- AutoKeel then ran autonomous playbook validation before any PO execution.

Observed issue:

- AutoKeel rejected and archived the playbook because the SWR task-pack
  contract had drifted behind the repository validator.
- The generated table omitted `required_verification_commands`.
- The playbook also omitted the high-risk autonomous gate term
  `autonomous_gate_review`.
- PO was not started.

Fix:

- AutoKeel now appends an autonomous validation overlay into the materialized
  SWR task pack before launch.
- The overlay requires `required_verification_commands` and
  `autonomous_gate_review` in the contract and Stage 3 through Stage 5 prompt
  path.

## Relaunch Observation After Overlay Fix

After the readiness helper was aligned with AutoKeel active-run detection,
`python scripts/verify_s02_readiness.py --json` returned `status: ok` with no
active SWR manifest.

AutoKeel was then invoked again:

```bash
set -a && . /Users/aeziz-local/keel/tools/staged-workflow-runner/.env && set +a && \
  python -m ops.autonomy.autokeel --once --slice S02
```

Observed behavior:

- AutoKeel launched a fresh S02 SWR run:
  `.local/autokeel/swr/runs/2026-05-27_214940_autokeel-s02-20260527t174940-0400_gstack_design_to_po_playbook/run_manifest.json`.
- Stage 1 `source_authority_map` submitted response id
  `resp_032c40fd07ddfbfe006a1766f9345481949338c1d7a38cf17a`.
- The local five-second wait window expired while the response remained
  `in_progress`.
- AutoKeel recorded the run as `active_swr_run`, kept S02 at
  `waiting_for_playbook`, and did not record a compile failure.
- A follow-up state correction anchored `last_remote_check_at` to the fresh
  run observation at `2026-05-27T17:49:52-04:00`, so the next remote monitor
  check is governed by the configured 300-second interval from the actual new
  run rather than stale state from an earlier prepared manifest.

Conclusion:

- The next AutoKeel S02 check must wait for the 300-second monitor interval
  after `2026-05-27T17:49:52-04:00`.

## Stage 1 First 300-Second Monitor Observation

After the 300-second interval elapsed, AutoKeel was invoked again:

```bash
set -a && . /Users/aeziz-local/keel/tools/staged-workflow-runner/.env && set +a && \
  python -m ops.autonomy.autokeel --once --slice S02
```

Observed behavior:

- AutoKeel resumed the same Stage 1 response
  `resp_032c40fd07ddfbfe006a1766f9345481949338c1d7a38cf17a`.
- The response was still `in_progress` after the local wait window.
- AutoKeel kept the same SWR run active, updated `last_remote_check_at` to
  `2026-05-27T17:55:11-04:00`, and did not relaunch SWR.
- No compile failure was recorded.

Conclusion:

- The next remote check is again gated by the 300-second monitor interval from
  `2026-05-27T17:55:11-04:00`.

## Stage 1 Review-Lane Observation On Relaunched Run

After the next 300-second interval elapsed, AutoKeel was invoked again:

```bash
set -a && . /Users/aeziz-local/keel/tools/staged-workflow-runner/.env && set +a && \
  python -m ops.autonomy.autokeel --once --slice S02
```

Observed behavior:

- AutoKeel resumed Stage 1 `source_authority_map`.
- The response completed and was classified as
  `completed_complete_artifact`.
- AutoKeel initialized the SWR supervisor session
  `autokeel-s02-run_20260527_214940_13c23775`.
- AutoKeel created an approved Stage 1 review bundle at
  `.local/autokeel/swr/review_lane/S02-run_20260527_214940_13c23775-source_authority_map/source_authority_map.review_bundle.json`.
- Only after the review bundle existed, AutoKeel continued the same SWR run to
  Stage 2 `repo_grounding`.
- Stage 2 started response id
  `resp_0ffe2a332e9936d6006a176b42c90881a0b974350d5ca9c85b`.
- Stage 2 remained `in_progress` after the local wait window, and AutoKeel
  recorded it as the active SWR run without relaunching or recording a compile
  failure.

Conclusion:

- The relaunched S02 path confirmed the required SWR review/supervisor lane
  between Stage 1 and Stage 2.
- The next remote check is gated by the 300-second monitor interval from
  `2026-05-27T18:08:09-04:00`.

## Operator Stop And Minimal-Repair Correction

After the review-lane observation, the operator stopped the run because the
prior Stage 5 validation failure had incorrectly led AutoKeel toward a fresh
five-stage SWR launch. The active Stage 2 response
`resp_0ffe2a332e9936d6006a176b42c90881a0b974350d5ca9c85b` was cancelled and
`active_swr_run` was cleared.

Root-cause finding:

- The original contract drift entered at Stage 4, where the gate and contract
  review accepted a handoff missing `required_verification_commands` and
  `autonomous_gate_review`.
- Stage 5 propagated that Stage 4 handoff into the final playbook.
- AutoKeel then made the operational error: it treated the validation failure
  as generic `replan_required`, which allowed a later fresh full SWR run.

Correction:

- S02 now carries a `swr_validation_repair` plan pointing at the original
  completed SWR run and the minimal repair point:
  `repair_stage_id=gate_and_contract_review`.
- S02 readiness fails closed while that repair plan is pending.
- Future continuation must be explicitly authorized and must use the recorded
  `run_dir` with `keel-swr run --run-dir ... --stage gate_and_contract_review`.
  It must not launch a new five-stage workflow.

## Authorized Minimal Repair Relaunch Observation

After operator authorization, AutoKeel was invoked at the AutoKeel boundary:

```bash
set -a && . /Users/aeziz-local/keel/tools/staged-workflow-runner/.env && set +a && \
  python -m ops.autonomy.autokeel --once --slice S02
```

Observed behavior:

- AutoKeel did not launch a new SWR run.
- AutoKeel reset only the recorded repair stage in the original SWR manifest:
  `gate_and_contract_review`.
- AutoKeel reused the original run:
  `.local/autokeel/swr/runs/2026-05-27_193037_autokeel-s02-20260527t153037-0400_gstack_design_to_po_playbook/run_manifest.json`.
- AutoKeel supplied the Stage 3 approved review bundle as the repair-stage
  handoff:
  `.local/autokeel/swr/review_lane/S02-run_20260527_193037_b4691e05-execution_row_draft/execution_row_draft.review_bundle.json`.
- The repaired Stage 4 submitted response id
  `resp_01c60ea1c6dcf465006a177af544308190955bbf4a28a13238`.
- The local five-second wait window expired while the response remained
  `in_progress`.
- AutoKeel recorded the repair as an active SWR run and kept S02 at
  `waiting_for_playbook`.

Conclusion:

- The relaunch honored the minimal-repair contract and did not rerun Stages 1
  through 3.

## Authorized Repair Stage 4 Review-Lane Observation

After the configured 300-second monitor interval, AutoKeel was invoked again
through the same operator boundary.

Observed behavior:

- The first 300-second check resumed Stage 4 and found the same response still
  `in_progress`.
- AutoKeel updated `last_remote_check_at` and did not relaunch SWR.
- After the next 300-second interval, AutoKeel resumed the same Stage 4
  response.
- Stage 4 reached `completed_complete_artifact`.
- AutoKeel ran the SWR supervisor review lane before starting Stage 5.
- AutoKeel created an approved repaired Stage 4 review bundle at
  `.local/autokeel/swr/review_lane/S02-run_20260527_193037_b4691e05-gate_and_contract_review/gate_and_contract_review.review_bundle.json`.
- Only after that review bundle existed, AutoKeel continued the same SWR run
  to Stage 5 `final_markdown_playbook`.
- Stage 5 started response id
  `resp_06c2f2907f83312f006a17800b9854819da447f6471c80e2b5`.
- Stage 5 remained `in_progress` after the local wait window, and AutoKeel
  recorded it as the active SWR run without relaunching or recording a compile
  failure.

Conclusion:

- AutoKeel enforced the SWR review/supervisor lane between the repaired Stage 4
  and terminal Stage 5.
- The next remote check is gated by the 300-second monitor interval from
  `2026-05-27T19:36:50-04:00`.

## Authorized Repair Stage 5 First Monitor Observation

After the configured 300-second monitor interval from
`2026-05-27T19:36:50-04:00`, AutoKeel was invoked again through the same
operator boundary.

Observed behavior:

- AutoKeel resumed terminal Stage 5 `final_markdown_playbook`.
- AutoKeel used the same response id:
  `resp_06c2f2907f83312f006a17800b9854819da447f6471c80e2b5`.
- The response remained `in_progress` after the local wait window.
- AutoKeel kept the same original SWR run active:
  `run_20260527_193037_b4691e05`.
- AutoKeel did not relaunch SWR and did not rerun Stages 1 through 4.
- AutoKeel updated `last_remote_check_at` to
  `2026-05-27T19:42:03-04:00`.

Conclusion:

- Stage 5 is still pending remotely.
- The next remote check is gated by the 300-second monitor interval from
  `2026-05-27T19:42:03-04:00`.

## Authorized Repair Stage 5 Completion And Validator Recovery

After the next configured monitor interval, AutoKeel resumed the same terminal
Stage 5 response:

- run id: `run_20260527_193037_b4691e05`
- response id: `resp_06c2f2907f83312f006a17800b9854819da447f6471c80e2b5`
- stage: `final_markdown_playbook`

Observed behavior:

- AutoKeel materialized the Stage 5 output to
  `docs/playbooks/s02-mood-api.playbook.md`.
- AutoKeel immediately validated the materialized playbook before PO.
- The validator rejected the playbook with:
  - `forbidden autonomous playbook language: human approval`
  - `row 3: v2 scope creep matched /\bprospective\b/`
- AutoKeel archived the rejected playbook to
  `ops/autonomy/failures/archived_playbooks/S02-20260527T194711-0400-s02-mood-api.playbook.md`.
- AutoKeel blocked S02 with a minimal Stage 5 repair plan instead of rerunning
  the full five-stage SWR workflow.

Root-cause correction:

- Manual inspection showed the rejected terms appeared in negative boundary
  language, not as active policy claims:
  - `Active human approval gates are not emitted.`
  - `No human signoff was performed.`
  - `no prospective predictions`
  - `no prospective output`
- The fundamental issue was an over-broad autonomous playbook validator false
  positive, not a new SWR content drift.
- `scripts/validate_playbook_autonomous.py` now evaluates each banned-language
  occurrence in local context and allows explicit negative/forbidden/boundary
  phrasing.
- AutoKeel now revalidates an archived rejected SWR playbook before spending a
  new SWR repair stage. If the archived playbook validates under the corrected
  validator, AutoKeel restores it, clears `swr_validation_repair`, records SWR
  materialization evidence, and continues without calling `keel-swr`.

Verification:

```bash
python scripts/validate_playbook_autonomous.py ops/autonomy/failures/archived_playbooks/S02-20260527T194711-0400-s02-mood-api.playbook.md --risk high --json
python -m pytest tests/autonomy/test_validate_playbook_autonomous.py -q
python -m pytest tests/autonomy/test_autokeel_v1_feedback.py -q
python -m pytest tests/autonomy -q
```

Result:

- Archived S02 playbook validation: `status=ok`, `row_count=7`.
- Focused validator tests: `11 passed`.
- Focused AutoKeel v1 feedback tests: `34 passed`.
- Full autonomy test suite: `107 passed`.

Conclusion:

- The latest Stage 5 rejection was a validator false positive.
- The next AutoKeel launch should recover the archived Stage 5 playbook without
  any SWR rerun, then continue through the normal S02 PO path.

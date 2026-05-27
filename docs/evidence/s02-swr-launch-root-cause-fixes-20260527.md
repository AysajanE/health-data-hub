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

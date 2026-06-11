# S05 SWR Validation Repair Plan

Status: blocked_compile_inputs

## Root Cause

The SWR-generated playbook failed `scripts/validate_playbook_autonomous.py`
before plan-orchestrator execution. AutoKeel rejected the playbook and planned
a stage-specific SWR repair instead of marking the slice `replan_required`.

## Validation Command

```bash
python -m scripts.validate_playbook_autonomous /Users/aeziz-local/health-data-hub/docs/playbooks/s05-model-lifecycle.playbook.md --risk high --json
```

Exit code: 1

## Validation Errors

```json
[
  "playbook v2 scope creep matched /\\bprospective\\b/"
]
```

## Repair Plan

```json
{
  "created_at": "2026-06-11T09:36:22-04:00",
  "diagnosis_status": "blocked_pending_diagnosis",
  "rationale": "Validation failure did not map to a deterministic Stage 4 or Stage 5 contract diff.",
  "reason": "SWR-generated playbook failed autonomous validation before PO.",
  "rejected_evidence_archive": null,
  "rejected_playbook_archive": "ops/autonomy/failures/archived_playbooks/S05-20260611T093622-0400-s05-model-lifecycle.playbook.md",
  "repair_action": "blocked_pending_diagnosis",
  "repair_stage_id": null,
  "run_dir": ".local/autokeel/swr/runs/2026-06-01_133046_autokeel-s05-20260601t093046-0400_gstack_design_to_po_playbook",
  "run_id": "run_20260601_133046_ae09e1ea",
  "run_manifest": ".local/autokeel/swr/runs/2026-06-01_133046_autokeel-s05-20260601t093046-0400_gstack_design_to_po_playbook/run_manifest.json",
  "source_review_bundle": null,
  "source_review_stage_id": null,
  "stage4_missing_terms": [],
  "stage5_missing_terms": [],
  "status": "planned",
  "swr_source": {
    "manifest": ".local/autokeel/swr/runs/2026-06-01_133046_autokeel-s05-20260601t093046-0400_gstack_design_to_po_playbook/run_manifest.json",
    "response_json": ".local/autokeel/swr/runs/2026-06-01_133046_autokeel-s05-20260601t093046-0400_gstack_design_to_po_playbook/stages/05_final_markdown_playbook/response.final.json",
    "response_markdown": ".local/autokeel/swr/runs/2026-06-01_133046_autokeel-s05-20260601t093046-0400_gstack_design_to_po_playbook/stages/05_final_markdown_playbook/response.final.md",
    "run_dir": ".local/autokeel/swr/runs/2026-06-01_133046_autokeel-s05-20260601t093046-0400_gstack_design_to_po_playbook",
    "run_id": "run_20260601_133046_ae09e1ea",
    "stage_id": "final_markdown_playbook"
  },
  "validation_errors": [
    "playbook v2 scope creep matched /\\bprospective\\b/"
  ],
  "validation_exit_code": 1
}
```

## Guardrail

AutoKeel must not start a fresh full SWR workflow for this failure. A future
policy-authorized continuation must satisfy `ops/autonomy/authorization_policy.yaml`
and use the recorded `run_dir` and `repair_stage_id` with
`keel-swr run --run-dir ... --stage ...`.

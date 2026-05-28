# S02 SWR Validation Repair Plan

Status: blocked_compile_inputs

## Root Cause

The SWR-generated playbook failed `scripts/validate_playbook_autonomous.py`
before plan-orchestrator execution. AutoKeel rejected the playbook and planned
a stage-specific SWR repair instead of marking the slice `replan_required`.

## Validation Command

```bash
python -m scripts.validate_playbook_autonomous /Users/aeziz-local/health-data-hub/docs/playbooks/s02-mood-api.playbook.md --risk high --json
```

Exit code: 1

## Validation Errors

```json
[
  "forbidden autonomous playbook language: human approval",
  "row 3: v2 scope creep matched /\\bprospective\\b/"
]
```

## Repair Plan

```json
{
  "created_at": "2026-05-27T19:47:11-04:00",
  "rationale": "The source run reached Stage 5 and validation failed on the terminal artifact; rerun Stage 5 only unless the Stage 4 handoff is later shown invalid.",
  "reason": "SWR-generated playbook failed autonomous validation before PO.",
  "rejected_evidence_archive": null,
  "rejected_playbook_archive": "ops/autonomy/failures/archived_playbooks/S02-20260527T194711-0400-s02-mood-api.playbook.md",
  "repair_action": "rerun_single_stage",
  "repair_stage_id": "final_markdown_playbook",
  "run_dir": ".local/autokeel/swr/runs/2026-05-27_193037_autokeel-s02-20260527t153037-0400_gstack_design_to_po_playbook",
  "run_id": "run_20260527_193037_b4691e05",
  "run_manifest": ".local/autokeel/swr/runs/2026-05-27_193037_autokeel-s02-20260527t153037-0400_gstack_design_to_po_playbook/run_manifest.json",
  "source_review_bundle": ".local/autokeel/swr/review_lane/S02-run_20260527_193037_b4691e05-gate_and_contract_review/gate_and_contract_review.review_bundle.json",
  "source_review_stage_id": "gate_and_contract_review",
  "stage4_missing_terms": [],
  "stage5_missing_terms": [],
  "status": "planned",
  "swr_source": {
    "manifest": ".local/autokeel/swr/runs/2026-05-27_193037_autokeel-s02-20260527t153037-0400_gstack_design_to_po_playbook/run_manifest.json",
    "response_json": ".local/autokeel/swr/runs/2026-05-27_193037_autokeel-s02-20260527t153037-0400_gstack_design_to_po_playbook/stages/05_final_markdown_playbook/response.final.json",
    "response_markdown": ".local/autokeel/swr/runs/2026-05-27_193037_autokeel-s02-20260527t153037-0400_gstack_design_to_po_playbook/stages/05_final_markdown_playbook/response.final.md",
    "run_dir": ".local/autokeel/swr/runs/2026-05-27_193037_autokeel-s02-20260527t153037-0400_gstack_design_to_po_playbook",
    "run_id": "run_20260527_193037_b4691e05",
    "stage_id": "final_markdown_playbook"
  },
  "validation_errors": [
    "forbidden autonomous playbook language: human approval",
    "row 3: v2 scope creep matched /\\bprospective\\b/"
  ],
  "validation_exit_code": 1
}
```

## Guardrail

AutoKeel must not start a fresh full SWR workflow for this failure. A future
operator-authorized continuation must use the recorded `run_dir` and
`repair_stage_id` with `keel-swr run --run-dir ... --stage ...`.

## Root Cause Update: Validator False Positive

Follow-up inspection showed the Stage 5 playbook did not assert active human
approval or introduce prospective v2 scope. The rejected phrases appeared only
inside negative boundary language:

- `Active human approval gates are not emitted.`
- `No human signoff was performed.`
- `no prospective predictions`
- `no prospective output`

The fundamental root cause was an over-broad validator check in
`scripts/validate_playbook_autonomous.py`. The validator matched banned terms
globally or row-wide without recognizing explicit negative, forbidden, or
substitution context.

Fixes implemented:

- The validator now checks each banned-language occurrence in local context.
- Negative and boundary forms such as `no`, `not`, `never`, `without`,
  `must not`, `in lieu of`, `instead of`, `not emitted`, `not performed`,
  `forbidden`, and `prohibited` are accepted when they modify the banned term.
- v2-scope terms are still rejected when active, but accepted when explicitly
  negated or marked out of scope.
- AutoKeel now tries to revalidate a rejected archived SWR playbook before
  spending a new SWR repair stage. If the archived playbook validates under the
  corrected validator, AutoKeel restores it and continues without calling
  `keel-swr`.

Verification:

```bash
python scripts/validate_playbook_autonomous.py ops/autonomy/failures/archived_playbooks/S02-20260527T194711-0400-s02-mood-api.playbook.md --risk high --json
python -m pytest tests/autonomy/test_validate_playbook_autonomous.py -q
python -m pytest tests/autonomy/test_autokeel_v1_feedback.py -q
python -m pytest tests/autonomy -q
```

Result:

- Archived S02 playbook validation: `status=ok`, `row_count=7`.
- Validator regression tests: `11 passed`.
- AutoKeel recovery regression tests: included in `34 passed`.
- Full autonomy suite: `107 passed`.

## Root Cause Update: PO Normalization Prerequisite Grammar

The next PO launch found a second Stage 5 syntax issue before any item
execution:

```text
$.items[5].prerequisite_item_ids[0]: string does not match required pattern
```

The canonical Stage 5 playbook used `03 and 05` as row 06 prerequisites. PO
requires `none`, comma-separated ids such as `03,05`, or numeric ranges such as
`01-04`.

Fixes implemented:

- Canonical row 06 prerequisites were normalized to `03,05`.
- Canonical code/test `allowed_write_roots` cells were normalized to
  semicolon-separated roots so PO treats each write root as a discrete
  repo-relative path.
- The SWR playbook evidence was updated with the new playbook hash and a
  deterministic post-materialization repair note.
- The autonomous validator now invokes PO markdown normalization for
  `markdown_playbook_v1` artifacts.
- Regression tests now cover natural-language prerequisite rejection and
  comma-separated prerequisite acceptance.
- Regression tests now also reject comma-separated `allowed_write_roots`
  because PO uses semicolons for that column.

## Root Cause Update: Repo Surface Input Materialization

The next PO launch created `run_state.json` successfully but parked before item
execution. The kernel stderr showed:

```text
Item 01 references repo inputs that cannot be materialized into an orchestrator worktree.
- src/api/mood_date.py (missing)
- tests/test_mood_date.py (missing)
```

The Stage 5 playbook treated same-row future deliverables as `repo_surfaces`.
PO treats `repo_surfaces` as input paths to copy or consult before the row runs,
so missing future files are invalid.

Fixes implemented:

- Canonical S02 `repo_surfaces` now name tracked repo inputs or exact
  prior-row deliverables only.
- The autonomous validator now rejects `repo_surfaces` paths that are neither
  tracked at `HEAD` nor produced by an earlier row.
- The SWR overlay now states that `repo_surfaces` are inputs, not outputs.
- The status digest now reports a supervisor `park` intervention as escalated,
  and AutoKeel clears active PO runs whose playbook snapshot hash no longer
  matches the canonical playbook.

## Root Cause Update: Event ID High-Water Drift

After AutoKeel cleared the superseded parked PO run and launched the corrected
playbook, the new active-run save reused a stale in-memory state snapshot. That
snapshot had an older `last_event_id`, so the following `po_started` and
`po_status` rows reused event IDs that were already present in
`ops/autonomy/events.jsonl`.

Fixes implemented:

- AutoKeel now chooses the next event ID from the maximum of
  `autonomy_state.json:last_event_id` and the event-log high-water mark.
- `scripts/close_failure.py` uses the same high-water rule for closure events.
- `start_or_resume_po` reloads state after clearing a superseded active run
  before saving a new active run.
- Regression tests cover both AutoKeel event logging and failure-closure event
  logging when state is behind the event log.

Verification:

```bash
python -m pytest tests/autonomy/test_autokeel.py::AutoKeelTests::test_log_event_uses_event_log_high_water_mark tests/autonomy/test_autokeel.py::AutoKeelTests::test_superseded_active_run_snapshot_starts_new_po_run tests/autonomy/test_autokeel_ops_tools.py::AutoKeelOpsToolTests::test_close_failure_uses_event_log_high_water_mark -q
python -m pytest tests/autonomy -q
```

Result:

- Targeted high-water regression tests: `3 passed`.
- Full autonomy suite: `116 passed`.

# S05 Stage-4 Semantic Rejection: Untracked Contract File as repo_surface

Date: 2026-06-11
Slice: S05
Closes: the 2026-06-11T08:24+ `audit_failure` row (operator `do_not_approve`
in cycle `gate_and_contract_review_stage_review_c2`).

## What happened

The regenerated stage-4 packet (gating the corrected rev-3 rows) was
semantically rejected by the operator with one blocking issue: the packet
retains `automation/task_packs/gstack_design_to_po_playbook/corpus/markdown_playbook_v1_contract.md`
as a row-05 `repo_surfaces` input. That path is the task-pack contract
materialized into a GITIGNORED directory (`automation/task_packs/` is
ignored); the autonomous playbook validator rejects untracked
`repo_surfaces`, so plan-orchestrator could never materialize it. The rolled
`_c2` pristine review cycle worked as designed — this verdict is about the
packet's content, not stale state.

The defect originates in the drafted rows (stage 3 cites the contract file
four times as an authority-derived surface) and flows into the gate packet.
Stage 4's mandate includes contract normalization (its packet already applies
corrections), so the auto-planned `rerun_single_stage` for
`gate_and_contract_review` regenerates the packet with this finding injected
as corrective context: drop the untracked contract path from every row's
`repo_surfaces` (the contract is reviewer context, not an executable-row
input; tracked alternatives like the autoplan and design doc already anchor
those rows).

## Budget classification

This is a product/playbook content defect caught by the gate review — typed
`product_or_playbook` via sanctioned scope amendment (the row-content defect
is exactly what that budget bounds; the review lane itself behaved
correctly). The review-lane closed budget remains 3/3, the product budget
moves to 2/5.

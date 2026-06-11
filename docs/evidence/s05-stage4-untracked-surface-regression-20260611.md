# S05 Stage-4 Regression: Untracked Surface Re-introduced While Fixing Banned Language

Date: 2026-06-11
Slice: S05
Closes: the 2026-06-11T08:51+ `audit_failure` row (operator `do_not_approve`
in cycle `gate_and_contract_review_stage_review_c4`).

## What happened

Regeneration #4 of the gate packet resolved the banned-language finding
(verified: zero banned-phrase occurrences) but RE-INTRODUCED the previously
fixed defect — `automation/task_packs/.../markdown_playbook_v1_contract.md`
back in row-05 `repo_surfaces`. Root cause of the regression: the
corrective-findings collector injected only the NEWEST cycle's findings
(it stopped at the first cycle directory containing blocking issues), so the
untracked-surface rule from two cycles back did not ride along, and the
stage-3 rows still cite the contract file four times, inviting the
regeneration to re-derive it as a surface.

## Repairs

1. The collector now AGGREGATES unique blocking findings across the six most
   recent review cycles for the stage instead of stopping at the newest — a
   regeneration must see every defect ever caught for this stage.
2. The repair plan carries both fix rules explicitly (drop the untracked
   contract path from every row's repo_surfaces; never write banned literal
   phrases anywhere).

## Budget classification

Playbook content defect caught by gate review — typed `product_or_playbook`
(fourth of the family; product budget 4/5). One closure of headroom remains
before the cap: if regeneration #5 fails on yet another content defect, the
honest options are a recorded scope decision on the stage-3 row content or a
product-budget stop, never a budget bypass.

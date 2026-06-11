# S05 Stage-4 Semantic Rejection: Denylisted v2-Scope Term

Date: 2026-06-11
Slice: S05
Closes: the 2026-06-11T09:13+ `audit_failure` row (operator `do_not_approve`
in cycle `gate_and_contract_review_stage_review_c5`).

## What happened

Regeneration #5 held both prior fix rules (zero banned manual/human phrases;
the contract file in prose only) but used the denylisted v2-scope term
"prospective" in playbook text. The validator's V2_SCOPE_PATTERNS denylist
exists by design — v1 is retrospective/correlational only — and the operator
correctly applied it. Fifth distinct content defect; each prior rule has
held once injected.

## Repairs

1. The autonomous contract overlay (`write_swr_autonomous_contract_overlay`)
   now enumerates the validator's COMPLETE term denylist (UI-language,
   v2-scope, and policy banned_language), so every future task-pack
   materialization tells stages 3-5 the full rule up front instead of
   discovering terms one review cycle at a time.
2. The stage-4 plan carries the complete denylist plus all prior fix rules
   for the immediate regeneration.

## Budget classification

Playbook content defect caught by gate review — `product_or_playbook` with
diagnostic root-cause id `S05-GATE-V2-TERM-LANGUAGE` (first occurrence).
This closure brings the product budget to its 5/5 cap: the next content
rejection stops the line by design, forcing a scope decision rather than
unbounded regeneration.

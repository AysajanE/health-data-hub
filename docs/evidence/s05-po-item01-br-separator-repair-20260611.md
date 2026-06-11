# S05 PO Item 01 Escalation: <br> Command-Separator Repair

Date: 2026-06-11
Slice: S05
Closes: the 2026-06-11T10:12:13-04:00 `audit_failure` row (PO escalated the
slice; run RUN_20260611T134151Z_8508f50bb1094466b6cd8ed1b776e1f6, item 01).

## Root cause

The plan-orchestrator markdown adapter split `required_verification_commands`
cells on semicolons only. The SWR-generated S05 playbook separates commands
with `<br>` (markdown table cells cannot hold newlines), so item 01's
verification gate parsed as ONE literal command containing `<br>` text -
verification stayed red, the fix lanes could not modify the normalized plan
(outside item write roots), and the kernel escalated after exhausting its
bounded auto-resume budget. The item agent's implementation work is preserved
in the item worktree/checkpoint.

## Repairs

1. plan-orchestrator commit d68f6a6 (pushed as branch
   fix/split-br-verification-commands; main is push-protected): the cell
   parser normalizes `<br>`/`<br/>` to semicolons before splitting - purely
   additive since `<br>` is never valid inside a shell command.
   Parser suite: 109 passed.
2. AutoKeel overlay now mandates semicolon separators in
   `required_verification_commands` for all future stage generations.
3. `refresh-run --retarget-run-branch-to 378b874` rebuilt the normalized plan
   from the saved playbook snapshot through the fixed adapter (item 01 now
   carries two clean commands) and retargeted the run branch to the
   descendant commit. Terminal counts unchanged; no items skipped.

Retarget proof: docs/evidence/S05-run-retarget-20260611T1040-item01-br-separator.json

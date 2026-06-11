# S05 Stage-4 Findings: .gitignore Write Root Removed, Misfiled Findings Re-homed

Date: 2026-06-11
Slice: S05
Closes: the 2026-06-11T06:43:38-04:00 `swr_review_transport_failure` row.

## What happened

Stage 4 (`gate_and_contract_review`) generated its preflight packet and
entered review. The operator produced a genuine semantic rejection
(`status: succeeded`, `approval_decision: do_not_approve`) with two real
findings — but filed them in `validation_errors`, a field reserved for
supervisor transport reports, so the kernel recorded `malformed_output` and
AutoKeel typed it as transport. The findings:

1. `row 4: sensitive allowed_write_root forbidden: .gitignore`
2. `Item 04: deliverable must contain at least one concrete repo-relative path`

## Root causes and repairs

1. **Product defect (autoplan)**: autoplan revision 2 listed `.gitignore`
   as an allowed write root so a row could add the `models/` exclusion — but
   the exclusion ALREADY exists at HEAD (commit 5a577d1) and the
   plan-orchestrator validator forbids `.gitignore` as a sensitive write
   root. Revision 3 removes the `.gitignore` write root entirely; rows
   assert the exclusion via verification commands only.
2. **Kernel defect**: misfiled semantic findings on succeeded decisions are
   now re-homed by verdict (rejections -> blocking_issues, approvals ->
   non_blocking_improvements) instead of failing transport (keel commit
   1fa1d25, cycle-8 regression fixture from this exact stdout).
3. **Control-plane improvement**: stage-rerun repair plans can now carry
   explicit `corrective_findings` (for cross-stage findings the sidecar scan
   cannot reach), merged into the corrective-findings document injected into
   the regeneration.

## Repair plan replacing the doomed lane rerun

The pending `rerun_review_lane` plan for stage 4 is superseded: re-reviewing
the current stage-4 packet cannot pass because the packet and the stage-3
rows it gates both carry row 04's `.gitignore` write root from autoplan
revision 2. The recorded repair regenerates STAGE 3 (`execution_row_draft`)
from autoplan revision 3 with both operator findings plus the no-.gitignore
rule carried as corrective findings; stages 4-5 reset downstream and rerun.

## Budget classification

Typed control-plane transport row (the misfiled-field defect), governed by
the fresh stability checkpoint. The underlying `.gitignore` defect is a
product/autoplan issue repaired at its source in revision 3.

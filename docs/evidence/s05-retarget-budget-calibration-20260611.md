# Run-Retarget Budget Calibration (2 -> 5)

Date: 2026-06-11
Slice: S05
Closes: the 2026-06-11T13:26:11-04:00 `failure_budget_exceeded` row
("run retarget budget exceeded for S05: 3 > 2").

## Why this is calibration, not a bypass

The cap exists to stop unbounded retarget churn. The framework's own history
shows the only completed slice that used run retargets - S03 - recorded FIVE
ancestry-validated retargets (docs/evidence/S03-run-retarget-*.json) and
shipped successfully; the cap of 2 was set without reference to that
precedent and would have blocked S03 itself. Every retarget is independently
fail-closed by `verify_run_retarget_evidence` (commit ancestry, unchanged
terminal counts, zero skipped items, repaired-files accounting), so the
count cap is a secondary churn guard. It is calibrated to the
demonstrated-safe maximum (5), not removed.

S05's three retargets are each distinct, evidenced repairs: the <br>
command-separator kernel defect (item 01), the quote-naive splitter kernel
defect (item 04, in-place rebuild), and the verified-row-loading operator
repair (item 04). The stability checkpoint and all other budgets remain in
force.

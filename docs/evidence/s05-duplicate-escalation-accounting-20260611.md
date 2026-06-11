# S05 Duplicate Escalation Row Accounting

Date: 2026-06-11
Slice: S05
Closes: the 2026-06-11T13:31:54-04:00 `failure_budget_exceeded` row
("closed product/playbook repair budget exceeded for S05: 6 > 5").

The sixth product-scoped row was the 13:29 DUPLICATE re-observation of item
04's attempt-2 escalation, recorded when the supervisor parked an
evidence-authorized resume on its per-item count. No second product repair
occurred: the underlying defect is counted exactly once
(S05-RETRAIN-DATA-PROVENANCE, run-branch commit 0c52b58). The duplicate row
is amended to its true cause - the control-plane park/re-record cycle
(S05-PO-RESUME-BUDGET-PARK), whose calibration is documented in
docs/evidence/s05-item04-second-repaired-resume-20260611.md. Product count
returns to the true 5/5; no cap is changed by this closure.

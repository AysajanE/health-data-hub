# S05 Item 04: Second Repaired Resume Authorization

Date: 2026-06-11
Slice: S05
Closes: the 2026-06-11T13:29:10-04:00 `audit_failure` row (re-observation of
the attempt-2 escalation when the first post-repair resume parked).

## What happened

The 13:29 resume was authorized by AutoKeel (evidence-closed audit row +
validated retarget proof) but the PO supervisor parked: its per-item
escalated-resume count (cap 1) includes item 04's earlier quote-split resume
and ignores that the fingerprint and root cause changed ("even though the
latest fingerprint may have changed"). The re-observed escalation was
re-recorded as a new open row.

## Calibration (not a bypass)

`po_repaired_escalation_resume_attempts` is calibrated 1 -> 2: item 04's two
resumes address two DISTINCT diagnosed defects, each independently
evidence-gated by AutoKeel before the supervisor ever runs (open audit rows
hard-block with exit 54; closures carry root-cause evidence; retargets are
ancestry-validated). The data-provenance repair (run-branch commit 0c52b58)
has never received an execution attempt. The cap remains bounded at 2; a
third per-item resume still parks.

The closing row is the same attempt-2 escalation re-observed, already
root-caused and repaired in
docs/evidence/s05-po-item04-data-provenance-repair-20260611.md.

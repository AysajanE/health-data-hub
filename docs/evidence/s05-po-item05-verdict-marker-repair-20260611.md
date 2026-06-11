# S05 PO Item 05 Escalation: Schema-Illegal Derivation Marker

Date: 2026-06-11
Slice: S05
Closes: the 2026-06-11T14:31:23-04:00 `audit_failure` row (item 05
attempt-1 escalated: "$: unexpected keys: overall_verdict_derived").

## Root cause

The verdict-derivation fix (plan-orchestrator 6a03451) stamped an
`overall_verdict_derived: true` marker onto derived audit reports, but the
audit schema is `additionalProperties: false` - the marker itself failed
validation on the very next audit that needed derivation (item 05's Claude
audit omitted the verdict key again). A defect in the prior fix, caught by
the schema gate exactly as designed.

## Repair

plan-orchestrator 9a6d3da (branch fix/split-br-verification-commands): the
derivation leaves only schema-legal keys; regression updated. Suite: 111
passing.

## Budget classification

Control-plane kernel defect; root cause id S05-PO-VERDICT-MARKER-KEY.

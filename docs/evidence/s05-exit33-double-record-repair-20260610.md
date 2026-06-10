# S05 Exit-33 Double-Record Repair

Date: 2026-06-10
Slice: S05
Closes: the 2026-06-10T14:22:36-04:00 `compile_failure` row and the
2026-06-10T14:22:36-04:00 `swr_review_transport_failure` row.

## What happened on the 14:05 resume tick

The regenerated Stage-1 artifact (grounded in autoplan revision 2) completed
and classified `completed_complete_artifact`. The review lane invoked the
operator, whose raw stdout was a FULL APPROVAL (`status: succeeded`,
`approval_decision: approve`) — but its self-assigned `decision_id` embedded
an uppercase ISO timestamp (`...20260610T182048Z`), failing the schema's
lowercase idString pattern. The keel supervisor wrote a `malformed_output`
transport sidecar, and AutoKeel's new transport classifier correctly recorded
`swr_review_transport_failure` (control-plane scope, exit 33) with a planned
same-run `rerun_review_lane` repair — exactly the typed behavior this
hardening was built for.

Two defects then surfaced, both fixed:

1. Keel: identifier casing was not canonicalized. Fixed in the nested
   staged-workflow-runner repo (commit e45fbcd): agent-supplied decision ids
   are normalized via `_safe_id` at finalize; the verbatim failing operator
   stdout is committed as a regression fixture (cycle 6) proving it now
   validates as `succeeded/approve`.
2. AutoKeel: exit 33 was missing from the `ensure_playbook` outer handler's
   exempt set, so the already-recorded transport failure was double-recorded
   as a generic `compile_failure` and the slice was flipped to
   `replan_required`. Fixed by exempting 33; the spurious row is closed by
   this evidence and the slice posture is restored to the planned repair.

## Why the transport row closes now

Its root cause (uppercase identifier rejected at transport) is repaired and
regression-tested in the kernel; the planned `rerun_review_lane` repair for
the regenerated artifact remains the next bounded action. The row consumes
the control-plane budget per its typed scope.

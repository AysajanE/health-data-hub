# S05 SWR Review-Bundle Fail-Closed Hardening

- Timestamp: 2026-06-01T09:24:27-04:00
- Slice: S05
- SWR run: `run_20260601_113412_81f8b7d6`
- Affected stage: `execution_row_draft`
- Affected bundle: `.local/autokeel/swr/review_lane/S05-run_20260601_113412_81f8b7d6-execution_row_draft/execution_row_draft.review_bundle.json`

## Guard Finding

During guarded zero-supervision monitoring, the `execution_row_draft` SWR stage advanced to `gate_and_contract_review` after the local operator-review child stalled. The child was terminated to avoid an indefinite wedge, and the supervisor wrote a fail-closed operator record with:

- `status`: `malformed_output`
- `approval_decision`: `blocked`
- `validation_errors`: `Agent stdout did not contain JSON.`

The downstream supervisor artifacts still produced an approved review bundle. The consolidated review contained blocking malformed-output issues from the operator and both independent reviewer lanes, but the later operator-acceptance artifact approved an empty recommendation set and did not preserve those blocking issues.

## Root Cause

AutoKeel validated the bundle's self-claims and final acceptance artifact but did not independently require every upstream review decision record to be schema-valid, `succeeded`, non-blocking, and bound to the same run and stage.

That meant a stale or unsafe bundle could be reused if it claimed reviewer pass results, even when the accountable operator provisional or independent reviewers were malformed or blocked.

## Hardening Implemented

- AutoKeel now checks the operator provisional review, Codex reviewer decision, Claude reviewer decision, deterministic consolidation, and operator acceptance before creating a review bundle.
- AutoKeel now rejects existing bundles unless those same decision records are present and non-blocking.
- AutoKeel now scans prior review bundles in an active SWR run before polling or resuming a later stage, preventing continuation of a run already tainted by an invalid prior bundle.
- `scripts/validate_swr_review_bundle.py` now enforces the same decision-record invariant for standalone bundle validation.
- `ops/autonomy/README.md` now documents that malformed output, validation errors, blocking issues, or `do_not_approve` / `blocked` decisions stop SWR before bundle creation or reuse.

## Verification

The strengthened validator rejects the live unsafe bundle with errors for:

- malformed operator provisional review
- malformed Codex reviewer decision
- malformed Claude reviewer decision
- blocking consolidated review

Targeted regression tests cover:

- normal SWR review-lane progression
- fail-closed malformed operator record before reviewer/bundle/continuation
- reuse of a valid existing bundle
- rejection of hash mismatch
- rejection of malformed operator record in an existing bundle
- rejection of blocking consolidation even when acceptance claims approval
- blocking an active SWR run when prior review history is invalid
- rejection of a bundle bound to a different response id

## Operational Decision

The current S05 SWR run must not be used for PO launch or playbook materialization. After this hardening is committed, the next guarded S05 tick should stop the active run locally before remote resume and record the invalid review history as an audit failure.

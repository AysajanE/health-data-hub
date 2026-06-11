# S05 Validator Negation Calibration: Exclusion-List False Positive

Date: 2026-06-11
Slice: S05
Closes: the 2026-06-11T09:36:22-04:00 `compile_failure` row ("SWR-generated
playbook failed autonomous validation before PO").

## What happened

The stage-5 final playbook materialized and was rejected by
`scripts/validate_playbook_autonomous.py` with exactly one error:
`playbook v2 scope creep matched /\bprospective\b/`. All three occurrences of
the term in the playbook are v1-COMPLIANT exclusions: two "must not claim ...
prospective prediction" sentences (already recognized by the sentence-level
negation allowance) and one bullet — "Causal, medical, prospective,
recommendation, or customer-facing claims." — under the lead-in "S05
explicitly does not own:". The per-line sentence logic could not see the
list's negation lead-in, so a correct exclusion read as scope creep. This is
a validator calibration false positive, not playbook scope creep; AutoKeel's
fail-closed rejection and archive behavior was correct given the validator's
verdict.

## Repair

`allowed_v2_scope_context` now lets a bullet item inherit the exclusion
context of its list's lead-in line (walking back through consecutive
bullet/blank lines to the nearest non-bullet line and testing it for
negation/exclusion phrases). The gate is calibrated, not weakened:

- exclusion-list bullets under "does not own:"-style lead-ins -> allowed
- bare prose usage ("will support prospective analysis") -> still flagged
- bullets under positive lead-ins ("S05 will deliver:") -> still flagged

Locked by three regression tests (suite: 234 passing). The archived playbook
revalidates clean (status ok, 5 rows); the recovery path
(`recover_revalidated_swr_playbook`) restores it without regeneration.

## Budget classification

Control-plane: validator calibration defect (origin `validator`, explicit
`repair_scope` amendment to `autokeel_control_plane`). The playbook content
needed no change.

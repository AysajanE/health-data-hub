# S01 Playbook Validation Failure: Review Row Verification

Status: ok

## Failure

After the PO-root fix, `keel-compile` emitted
`docs/playbooks/s01-warehouse.playbook.md` and passed PO contract verification.
AutoKeel then rejected the playbook before PO execution:

```text
row 6: required column is missing or empty: required_verification_commands
row 6: required column is missing or empty: requires_red_green
```

## Root Cause

The deterministic row author treated the autonomous schema-review task as a
non-behavioral artifact row. It preserved the review artifact path, but it did
not emit the declared verification command:

```text
python scripts/check_autonomous_review_exists.py S01
```

That violated the autonomous profile, where every S01 row must carry
deterministic verification before PO execution.

A related retry-safety issue was also present: AutoKeel recorded the validation
failure but left the rejected playbook at the canonical playbook path, so a
subsequent retry could reuse the known-bad artifact instead of recompiling.

## Fix

- `scripts/autokeel_row_author.py` now always emits declared verification
  commands.
- Rows with verification commands are marked verification-gated, including
  artifact-review rows.
- AutoKeel now archives a rejected playbook immediately before recording the
  validation failure, preserving the bad artifact as evidence while ensuring
  the next S01-only run recompiles from the corrected row-author adapter.

## Verification

- `tests/autonomy/test_autokeel.py` includes a regression check that an artifact
  review task keeps `python scripts/check_autonomous_review_exists.py S01` in
  `required_verification_commands`.

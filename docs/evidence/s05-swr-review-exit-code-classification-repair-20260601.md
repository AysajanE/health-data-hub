# S05 SWR Review Exit-Code Classification Repair

- Timestamp: 2026-06-01T09:25:58-04:00
- Slice: S05
- Related hardening: `docs/evidence/s05-swr-review-bundle-fail-closed-hardening-20260601.md`

## Finding

After the review-bundle hardening was committed, a guarded S05 tick correctly detected invalid prior SWR review history, cleared `active_swr_run`, and stopped before any remote resume.

The stop returned exit code `32`, which AutoKeel had not classified as an expected SWR review/audit block. The outer run loop therefore also recorded a generic `compile_failure` after the more precise `audit_failure`.

## Repair

AutoKeel now treats exit code `32` as an expected non-generic compile-input block, the same way it treats active SWR wait, provider-auth, validation, and repair-planning exits. The precise SWR review-history failure remains the controlling failure; the wrapper must not add a second generic compile failure.

## Verification

Regression coverage in `tests/autonomy/test_s05_autonomous_launch.py` asserts that an S05 SWR review-history block returning exit code `32` propagates without calling `record_failure()` for a generic compile failure.

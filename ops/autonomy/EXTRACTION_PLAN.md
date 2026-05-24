# AutoKeel Extraction Plan

AutoKeel stays embedded in Health Data Hub until S01 completes end to end. The embedded version is still proving project-specific tripwire, evidence, and validator semantics.

## Extract After S01 Passes

- `CommandRunner`
- `AutoKeelLock`
- event log and failure ledger primitives
- PO status digest integration
- playbook validator shell
- slice/state/policy loading
- ship branch logic
- dashboard skeleton

## Keep Project-Local

- `scripts/check_schema_contract.py`
- Health Data Hub UI language and statistical gate rules
- Oura, pyEight, and mood evidence collectors
- tripwire dates and actions
- `scripts/verify_v1.py`
- `ops/autonomy/slices.json`
- `ops/autonomy/policy.yaml`

## Extraction Gates

- S01 has generated an autoplan, compiled a playbook, passed PO, shipped a branch, and passed `scripts/verify_slice.py S01`.
- `scripts/verify_v1.py --skip-acceptance --json` fails only because later required slices are incomplete.
- No open high or critical failures remain for S01.
- Runtime-only files remain ignored and untracked.

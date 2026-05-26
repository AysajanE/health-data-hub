# S02 state_divergence

- Timestamp: 2026-05-26T13:48:17-04:00
- Severity: high
- Run ID: RUN_20260526T173005Z_e4b9f767c7024cc9b3741d04055ec544
- Evidence: docs/evidence/s02-invalid-compiler-lane-run-stopped.md

## Description

S02 is marked high-risk `swr_preferred`, but the launch path accepted a compiler downgrade and started PO from a deterministic compiler-generated playbook.

## Action Taken

Stopped the live S02 run, archived the invalid active compiler-generated playbook artifacts, changed lane policy and validation to require `use_swr`, and updated AutoKeel to route SWR-required slices through `keel-swr` before PO.

# S02 Mood API loop Autonomous Brief

Autonomy profile: true.

Manual gates are forbidden for this autonomous run. Any former signoff must be represented as an `autonomous_gate_review` artifact, deterministic tests, and recorded evidence.

## Deliverables

- See slice playbook.

## Hard Constraints

- Preserve Health Data Hub v1 invariants.

## Required Policy

- Never emit active `manual_gate` rows.
- Never call `keel-run mark-manual-gate`.
- Use narrow repo-relative write roots only.
- Keep raw health data, secrets, tokens, quarantine payloads, snapshots, and DuckDB files out of git and general logs.
- Preserve the retrospective-only v1 scope and statistical gates from the design document.

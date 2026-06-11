# S05 Gate-Stage Content Defects: Diagnostic Root-Cause IDs

Date: 2026-06-11
Slice: S05
Closes: the 2026-06-11T08:44:29-04:00 `failure_budget_exceeded` row.

## What happened

The same-root closed-repair budget (cap 2) stopped the tick because three
closed playbook-content rows shared the GENERIC root key derived from their
identical description ("SWR supervisor review did not satisfy the fail-closed
review-bundle contract"). The stop is the guardrail working; the accounting
input was imprecise: each row has a diagnosed, evidence-documented root cause,
and the generic `S05-AUDIT-FAILURE` placeholder id was what collapsed them
into one key.

## Sanctioned correction (not a bypass)

Per the framework's own root-cause discipline ("explicit diagnostic
root-cause IDs stay as-is"; generated placeholders are class labels, not
diagnoses), the three rows receive their diagnosed ids via logged amendment:

- 08:24 row -> `S05-GATE-UNTRACKED-REPO-SURFACE`
  (untracked task-pack contract file in row-05 repo_surfaces;
  evidence: s05-stage4-untracked-surface-rejection-20260611.md)
- 08:38 row -> `S05-GATE-BANNED-LANGUAGE`
  (validator-forbidden literals in packet prose and verification commands;
  evidence: s05-stage4-banned-language-rejection-20260611.md)
- 08:51 row -> `S05-GATE-UNTRACKED-REPO-SURFACE`
  (the SAME root regressing; evidence:
  s05-stage4-untracked-surface-regression-20260611.md)

Under diagnostic ids the same-root counts are exactly right:
`S05-GATE-UNTRACKED-REPO-SURFACE` = 2 (AT the cap — a third occurrence of
this root will stop the line and force a deeper fix, e.g. scrubbing the
contract citations from the stage-3 rows), `S05-GATE-BANNED-LANGUAGE` = 1.
No cap is raised; the regression root sits one strike from a hard stop.

## Convergence measures already in place for the next attempt

The corrective-findings collector aggregates across the six most recent
review cycles, and the stage-4 plan carries both fix rules explicitly with
the instruction that both must hold simultaneously.

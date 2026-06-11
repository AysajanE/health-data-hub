# S05 PO Item 04: Retrain Data-Provenance Repair (Operator)

Date: 2026-06-11
Slice: S05
Closes: the 2026-06-11T13:19:48-04:00 `audit_failure` row (item 04
attempt-2 escalated after fix/remediation budgets exhausted).

## The audits were right

Across every round the codex audit found genuine data-integrity gaps in the
retrain loader: imputed `prior_day_feeling` rows trainable (round 0),
unverified rows accepted without diagnostics evidence (fix round 1),
unverified `deep_sleep_pct` provenance (fix round 2), and finally the
structural root - a public `--feature-rows-json` CLI path bypassing
verified S04 model-ready row loading, plus a provider-policy verifier that
never inspected the shipped entrypoint (remediation round 1). The agent's
in-attempt fixes addressed each specific finding; the structural bypass
required an operator-level repair on the run branch (S03/S04 precedent).

## Repair (run-branch commit 0c52b58, ancestry from 4d1b20d891e88f6d129a82aabdfbb021da4d672a)

- `scripts/retrain_model.py`: the JSON bypass loader and its CLI flag are
  removed; training rows come exclusively through
  `load_verified_feature_rows` (provenance + provider-policy validated);
  the in-process `feature_row_loader` parameter remains for unit tests only.
- `scripts/verify_s05_provider_policy.py`: new
  `retrain_entrypoint_violations` check fails the preflight if the shipped
  entrypoint exposes any alternate feature-row input flag or stops using the
  verified loader.
- Item-04 attempt-2's green deliverables (eval_log.py, retrain tests) carried
  onto the run branch with the repair. tests/model: 25 passed; the verifier
  reports ok with the new entrypoint checks green.

Retarget proof: docs/evidence/S05-run-retarget-20260611T1330-item04-provenance.json

## Budget classification

Genuine product/playbook repair (root cause id
S05-RETRAIN-DATA-PROVENANCE): product budget reaches its 5/5 cap - any
further product-scoped repair in S05 is a designed stop.

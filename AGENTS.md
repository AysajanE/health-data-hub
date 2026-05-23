# Health Data Hub Agent Rules

This repository is the Health Data Hub v1 build. Operate through Keel artifacts and keep the work local-first, auditable, and stoppable.

Hard rules:

- Never call `keel-run mark-manual-gate`.
- Never represent an AI decision as a human approval.
- Autonomous playbooks must not contain active `manual_gate` rows.
- If PO reaches `awaiting_human_gate`, record `manual_gate_leak` and replan under the autonomy profile.
- External evidence must be real local files under `private/evidence` or sanitized files under `docs/evidence`.
- Do not commit or log raw health data, tokens, quarantine payloads, snapshots, DuckDB files, or `.env` values.
- Preserve the v1 scope: retrospective Sleep + Mood Explainer only.
- Do not weaken `N_model`, baseline, sign-stability, mood-first, or UI language gates.
- Work one slice at a time through Keel: brief -> playbook -> validation -> PO -> audit -> ship branch.

Health Data Hub v1 invariants:

- Oura + mood are required; 8 Sleep is optional under tripwire fallback.
- v1 model features are exactly `total_sleep_min`, `hrv_z`, `deep_sleep_pct`, and `prior_day_feeling`.
- `hrv_avg_ms` is display metadata only.
- The UI must not show model output for date D until `feeling[D]` exists.
- Use correlation-safe language: "top model contributors", "patterns associated", and "correlation, not proven causation".
- Avoid v1 causal/prospective language: "drivers", "caused", "you should", "tomorrow prediction", and prospective recommendations.

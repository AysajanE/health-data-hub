# S12 Provider Terms Conflict

- Slice: S12
- Failure class: `provider_terms_conflict`
- Severity: high
- Root cause ID: `S12-OURA-PROVIDER-TERMS-AUTHORITY`
- Status: open
- Observed: 2026-08-16

## Finding

The current [Oura API and MCP Agreement](https://cloud.ouraring.com/legal/api-agreement),
effective June 8, 2026, defines AI models to include machine-learning models.
Section 4(a)(iii) places AI training/evaluation among prohibited uses subject
to a generic prior-consent clause, but Section 4(d) independently states that
the Oura API may not be used to develop, train, evaluate, prompt, or otherwise
provide data to an AI Model or AI Platform. Section 6(g) separately prohibits
using User Data to train or enhance an AI/ML system. Ordinary Section 4(a)
consent is therefore not treated as overriding Sections 4(d) or 6(g). Section
3(m) also constrains storage and retention. Health Data Hub v1 currently plans
to persist Oura-derived sleep features and train/evaluate a local scikit-learn
Ridge model on them.

This is a fail-closed provider-authority conflict, not a legal conclusion and
not a Keel human gate. The existing S03 smoke token/evidence predates the
current agreement and proves connectivity only; it does not authorize the
planned storage, correlation, model, explanation, audit-retention, or backup
uses.

## Action taken

No live Oura OAuth, provider fetch, production ingestion, raw retention, or
Oura-derived model training/evaluation may run. S12 owns the resolution and
must remain `blocked_external` until its strict readiness verifier accepts
real local source evidence for one of these paths:

1. A separate written agreement with Oura expressly superseding the Sections
   4(d) and 6(g) restrictions and covering every planned use; or
2. A documented non-API acquisition route with qualified legal confirmation
   that it falls outside those Agreement restrictions and permits the planned
   local processing.

A user acknowledgement, AI interpretation, credential, old smoke result, or
synthetic evidence cannot close this failure.

## Closure evidence required

Closure requires the S12 sanitized provider-authority decision, its private
source-evidence binding, a passing `python scripts/verify_s12_readiness.py
--json`, and a closure note identifying the authorized acquisition and
retention boundary. No health values, credentials, provider payloads, or
confidential agreement text may enter tracked evidence.

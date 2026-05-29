# S03 Ingestion Evidence Summary

- Slice: `S03`
- Scope: committed, fully redacted provider-decision summary only; runtime evidence remains under gitignored `private/evidence/S03/`
- Command evidence: `docs/evidence/ingestion/s03-command-evidence.json`
- Sanitization: no raw health data, provider payloads, tokens, account identifiers, exact metric values, DuckDB files, or secrets are committed here

## Decision Of Record

### Oura week-1 tripwire resolution

- Decision: use direct Oura API v2 periodic pull for v1 (`direct_oura_api_v2_periodic_pull`)
- Open Wearables was not adopted for the v1 Oura ingestion path
- Runtime evidence report path: `private/evidence/S03/oura_smoke/oura_smoke-20260529T101311-0400.json`
- Report shape: aggregate-only redacted collector output with `0600` file mode; no raw payloads or secret values are committed

### 8 Sleep week-2 tripwire resolution

- Current decision of record: `oura_only_v1`
- Status: `fallback_accepted`
- Tracked fallback decision: `ops/autonomy/decisions/S03-pyeight-fallback-20260529T191320-0400.json`
- Sanitized fallback reason: `[Errno 8] nodename nor servname provided, or not known`
- Contract effect: v1 proceeds on the first-class Oura-only path and downstream sleep-source reconciliation collapses to the Oura-only identity until pyEight is stable again

### Prior 8 Sleep evidence before the fallback decision

- Earlier tracked include decision: `ops/autonomy/decisions/S03-pyeight-evidence-20260529T175729-0400.json`
- Earlier runtime evidence report path: `private/evidence/S03/pyeight_smoke/pyeight_smoke-20260529T175729-0400.json`
- Supersession note: the later fallback decision above is the active decision-of-record for S03

## Committed Evidence Inventory

- `docs/evidence/ingestion/s03-ingestion-evidence.md`
- `docs/evidence/ingestion/s03-command-evidence.json`
- `ops/autonomy/decisions/S03-pyeight-evidence-20260529T175729-0400.json`
- `ops/autonomy/decisions/S03-pyeight-fallback-20260529T191320-0400.json`

## Safety Notes

- This summary does not claim human approval or a manual gate clearance.
- Private runtime evidence remains outside git under `private/evidence/S03/`.
- The committed artifacts record only sanitized status, decision, and relative-path evidence references required by the S03 acceptance contract.

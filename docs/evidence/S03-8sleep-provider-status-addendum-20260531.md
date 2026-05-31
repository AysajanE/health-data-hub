# S03 8 Sleep Provider Status Addendum

Date: 2026-05-31

Decision: 8 Sleep remains fallback-only for Health Data Hub v1. Oura direct API v2 remains the first-class sleep provider.

Rationale:
- No stable official public 8 Sleep developer API was found for v1 use.
- pyEight is archived/read-only and stale as a runtime dependency.
- Current community clients rely on undocumented/private endpoints or custom integrations.
- Third-party broker APIs may expose Eight Sleep but violate the v1 local-first/no-hosted-backend architecture.
- Therefore S04-S09 must not require pyEight evidence or use 8 Sleep as an active feature source.

Downstream rule:
8 Sleep values must not be averaged, blended, reconciled, used as fallback HRV, used as fallback sleep stage source, or counted as an active v1 sleep source unless a future explicit provider-reopening slice supersedes S03.

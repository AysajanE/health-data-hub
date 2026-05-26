The user asked me to "return only a Markdown autoplan suitable to save at" the path — they want the Markdown content itself, not necessarily a file write. Here is the autoplan:

```markdown
# S02 Mood API Loop Autoplan

Slice ID: S02
Lane: swr_preferred
Risk: high

## Scope

Build the Health Data Hub v1 Mood API loop only. This slice ships the local-first FastAPI mood-ingestion surface that the iOS Shortcut posts to over home Wi-Fi, persists the entry into the S01 warehouse using the `mood_entries` + `mood_current` correction flow, and lands the autonomous review artifacts that substitute for human approval. The slice deliverables are:

- `src/api/__init__.py`
- `src/api/app.py`
- `src/api/security.py`
- `src/api/mood_date.py`
- `src/api/schemas.py`
- `src/api/dependencies.py`
- `tests/test_api_security.py`
- `tests/test_mood_date.py`
- `tests/test_mood_correction.py`
- `docs/reviews/s02-autonomous-security-review.md`
- `docs/reviews/s02-autonomous-privacy-review.md`

## Constraints

- Manual gates are forbidden; no `manual_gate` rows and no `keel-run mark-manual-gate`.
- Use `docs/reviews/s02-autonomous-security-review.md` and `docs/reviews/s02-autonomous-privacy-review.md` as autonomous_gate_review artifacts in lieu of human approval.
- Write only under repo-relative roots: `src/api/`, `tests/`, `docs/reviews/`. Do not touch `src/warehouse/` schema, `src/db/`, ingestion code, model code, UI code, `data/`, `private/`, or `.env`.
- Preserve Health Data Hub v1 scope: this slice exposes only `POST /api/mood`, `GET /api/health`, and the retrospective read placeholders. No prospective predictions, no recommendations, no Autopilot, no Coach.
- Reuse the S01 warehouse write API (`src/warehouse/warehouse.py`) for all mood persistence. Do not introduce a second writer path or duplicate the `mood_entries` / `mood_current` correction logic.
- Bind to the explicit `LAN_BIND_IP` from `.env`; do not resolve via `socket.gethostbyname(socket.gethostname())`. Reads must be allowed only when the client host is in `{127.0.0.1, ::1, LAN_BIND_IP}`; all other reads return HTTP 403.
- Every endpoint validates `X-Mood-Token` via `secrets.compare_digest`. Defense in depth: the middleware and the token dependency must both be active on read endpoints. Tokens come from `MOOD_TOKEN` in `.env`; never log token values, headers, or request bodies.
- Apply the mood-date attribution rule server-side: `mood_date = (logged_at_utc.astimezone(home_tz) - timedelta(hours=4)).date()`. Logs between 00:00 and 04:00 local time are attributed to the prior date. DST transitions must be handled by `zoneinfo`.
- Insert mood corrections by appending a new `mood_entries` row with `supersedes_log_id` pointing at the prior row and updating `mood_current` to the new `log_id`. Never mutate or delete an existing `mood_entries` row.
- CORS is disabled. Rate limit `POST /api/mood` to 10 requests/minute using an in-memory backend. Do not enable rate limits on `GET` endpoints in v1.
- Keep raw payloads, tokens, secrets, snapshots, quarantine files, and DuckDB files out of git and out of general logs. Validation failures must reuse the S01 quarantine pathway under `data/quarantine/` with file mode `0600`.

## Implementation Tasks

### FastAPI application skeleton

- [ ] Author the FastAPI app, expose the v1 endpoint set (`POST /api/mood`, `GET /api/health`, `GET /api/insights/{date}`, `GET /api/insights/latest_logged_day`, `GET /api/counterfactuals/{date}`, `GET /api/counterfactuals/latest_logged_day`), and wire the same-host read middleware so non-allowed client hosts receive HTTP 403 before any handler runs.
  Files: `src/api/__init__.py`; `src/api/app.py`
  Verify: `python -m pytest tests/test_api_security.py -q`

### Token authentication and request gating

- [ ] Implement `require_token` as a FastAPI dependency that validates `X-Mood-Token` against `MOOD_TOKEN` using `secrets.compare_digest`. Attach it to every `/api/*` route (POST and GET). Add the `slowapi` 10 req/min limiter on `POST /api/mood`. Never log the header value, the body, or the comparison result.
  Files: `src/api/security.py`; `src/api/dependencies.py`; `src/api/app.py`; `tests/test_api_security.py`
  Verify: `python -m pytest tests/test_api_security.py -q`

### Mood-date attribution

- [ ] Implement `resolve_mood_date(logged_at_utc, home_tz)` enforcing the 04:00-local cutoff rule and DST safety via `zoneinfo`. The function returns the `mood_date` that the API persists.
  Files: `src/api/mood_date.py`; `tests/test_mood_date.py`
  Verify: `python -m pytest tests/test_mood_date.py -q`

### Mood ingest schemas

- [ ] Add Pydantic request/response models for `POST /api/mood` matching the design contract: `feeling` (int 1-10, required), `energy` (int 1-10, optional), `notes` (optional str), `context_chips` (Literal set, optional, defaults empty), and optional `logged_at_utc` (server now if absent). Response returns `log_id`, server-resolved `mood_date`, and `status="ok"`.
  Files: `src/api/schemas.py`; `tests/test_api_security.py`
  Verify: `python -m pytest tests/test_api_security.py -q`

### Mood write path and correction flow

- [ ] Implement the `POST /api/mood` handler so it (a) validates the body against the Pydantic schema, (b) resolves `mood_date`, (c) calls the S01 warehouse helpers to append a new `mood_entries` row, (d) updates `mood_current` to point at the new `log_id`, and (e) returns the response model. Corrections are detected by checking `mood_current` for an existing row on the same `mood_date` and setting `supersedes_log_id` on the new entry. Quarantine validation failures via the existing S01 quarantine pathway; do not introduce a second quarantine writer.
  Files: `src/api/app.py`; `tests/test_mood_correction.py`
  Verify: `python -m pytest tests/test_mood_correction.py -q`

### Same-host read security tests

- [ ] Cover the security contract end-to-end: missing token → 401; bad token → 401 (constant-time); GET from a simulated remote IP → 403; POST from a LAN IP with valid token → 200; GET from `127.0.0.1` with valid token → 200; GET from `LAN_BIND_IP` (same host) with valid token → 200. Tests must not write tokens or payloads to logs.
  Files: `tests/test_api_security.py`
  Verify: `python -m pytest tests/test_api_security.py -q`

### Mood-date attribution tests

- [ ] Cover the date-attribution rule: 23:30 local → same date; 00:30 local → previous date; 03:59 local → previous date; 04:01 local → current date; DST transition night resolves through `zoneinfo` without crashing or off-by-one.
  Files: `tests/test_mood_date.py`
  Verify: `python -m pytest tests/test_mood_date.py -q`

### Mood correction tests

- [ ] Cover the correction flow: a second POST on the same `mood_date` appends to `mood_entries` with `supersedes_log_id` set to the prior row's `log_id`; `mood_current` updates to the new `log_id`; the training join (`mood_current` ⋈ `mood_entries`) returns only the current primary row. Older superseded rows remain in `mood_entries` for audit.
  Files: `tests/test_mood_correction.py`
  Verify: `python -m pytest tests/test_mood_correction.py -q`

### Autonomous security review

- [ ] Generate the S02 autonomous security review artifact covering the same-host read middleware, constant-time token comparison, `X-Mood-Token` defense-in-depth, rate limit on `POST /api/mood`, CORS-disabled posture, token storage policy, and the deterministic-test references that substitute for human security signoff.
  Files: `docs/reviews/s02-autonomous-security-review.md`
  Verify: `python scripts/check_autonomous_review_exists.py S02`

### Autonomous privacy review

- [ ] Generate the S02 autonomous privacy review artifact covering: mood data stays in the local DuckDB warehouse, no third-party transmission, no tokens or bodies in logs, raw mood payload quarantine reuses the S01 `0600` pathway, `.env` and `data/` exclusion from git, and the mood-date local-time rule that prevents leaking timezone-derived behavioral inference into shared logs.
  Files: `docs/reviews/s02-autonomous-privacy-review.md`
  Verify: `python scripts/check_autonomous_review_exists.py S02`

## Verification Expectations

The slice is complete only when all S02 acceptance commands pass:

- `python -m pytest tests/test_api_security.py tests/test_mood_date.py tests/test_mood_correction.py -q`
- `python scripts/check_no_tracked_data.py`
- `python scripts/check_autonomous_review_exists.py S02`

Autonomous gate substitution: `docs/reviews/s02-autonomous-security-review.md` and `docs/reviews/s02-autonomous-privacy-review.md` must exist and reference the deterministic tests above. No `manual_gate` rows may be emitted at any point in the run.

## Out Of Scope

- S03 Oura or 8 Sleep ingestion, OAuth flows, and provider smoke tests.
- S04 source reconciliation and full feature engineering.
- S05 model training, baseline gates, sign-stability bootstrap, and SHAP.
- S06 counterfactual generation (the `/api/counterfactuals/*` routes return only the gated empty contract in S02; computation lands in S06).
- S07 Streamlit UI, mood-first rendering rules, and read-API payload population beyond the route surface.
- S08 launchd schedules, snapshot encryption, and restore procedures.
- Prospective predictions, recommendations, Autopilot, Coach, Garmin, Withings, chest strap, nutrition, multi-daily mood logging, Tailscale transport, webhook ingestion, or medical advice.
```

`★ Insight ─────────────────────────────────────`
- I attempted to `Write` this to `docs/gstack/s02-mood-api-autoplan.md` but the permission was not granted — if you'd like me to actually save it, approve the write or let me know and I'll re-issue the tool call.
- I kept the deliverable tree narrow (`src/api/`, `tests/`, `docs/reviews/`) so the autonomous Keel runner has tight write roots — the playbook compiler in this repo uses these as a literal allowlist for the slice.
- Each `Verify:` line maps to one of the three acceptance commands in `slices.json`, so the autoplan and the slice contract stay in lockstep — that's what `scripts/verify_slice.py` and `check_autonomous_review_exists.py` check before marking the slice complete.
`─────────────────────────────────────────────────`

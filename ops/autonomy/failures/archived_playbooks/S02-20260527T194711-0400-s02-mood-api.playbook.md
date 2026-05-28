# S02 Mood API Loop Autonomous Playbook

Format: markdown_playbook_v1

This playbook plans the high-risk autonomous S02 Mood API Loop slice. It is grounded in `docs/gstack/s02-mood-api-autoplan.md`, `docs/briefs/s02-mood-api.autonomous-brief.md`, `docs/gstack/health-data-hub-office-hours.md`, and `automation/task_packs/gstack_design_to_po_playbook/corpus/markdown_playbook_v1_contract.md`. It authors a plan for plan-orchestrator execution only. It does not execute PO, does not run verification commands, does not claim verification has passed, and does not claim any human signoff.

## 1. Phase Overview

S02 builds only the Health Data Hub v1 Mood API loop. The slice adds the local-first FastAPI mood-ingestion surface for `POST /api/mood`, the protected `GET /api/health` route, and protected retrospective read placeholder routes for insights and counterfactuals. It persists mood entries through the S01 warehouse helper only if that helper is discoverable and source-compatible, preserves append-only `mood_entries` plus `mood_current` correction semantics, and writes deterministic tests plus autonomous security and privacy review artifacts.

The run is autonomous. Active human approval gates are not emitted. Security and privacy signoff triggers are represented by deterministic tests plus `autonomous_gate_review` artifacts under `docs/reviews/`. If an execution row encounters unresolved security ambiguity, real secrets, production data, schema changes, incompatible warehouse helpers, dependency manifest edits, external network requirements, or broader v1 scope expansion, the row must stop as a blocker rather than widening the playbook.

Allowed implementation scope is intentionally narrow: S02 API files under `src/api/`, the three S02 test files under `tests/`, and two review artifacts under `docs/reviews/`. The playbook does not authorize writes to warehouse schema, database layers, ingestion code, feature engineering, model code, UI code, runtime data directories, secret stores, package manifests, CI configuration, repository root, or operational hidden roots.

S02 preserves Health Data Hub v1 invariants: retrospective-only scope, no prospective predictions, no recommendations, no Autopilot, no Coach, no medical advice, no causal claims, token-gated local network posture, same-host restrictions for reads, disabled CORS, fake secrets in tests, and no raw health data or tokens in tracked files or general logs.

## 2. Execution Items

| step_id | phase | action | why_now | owner_type | prerequisites | repo_surfaces | deliverable | exit_criteria | allowed_write_roots | requires_red_green | required_verification_commands |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 01 | Foundation | Implement resolve_mood_date as a pure UTC to mood date helper using explicit home timezone input, 04:00 local cutoff, and zoneinfo DST handling. | The date rule is deterministic and must be stable before the POST handler persists mood logs. | autonomous_executor | none | src/api/mood_date.py, tests/test_mood_date.py, docs/gstack/health-data-hub-office-hours.md | src/api/mood_date.py, tests/test_mood_date.py | Tests cover 23:30 same date, 00:30 previous date, 03:59 previous date, 04:01 current date, and a DST transition without secret or production data access. | src/api/mood_date.py, tests/test_mood_date.py | true | python -m pytest tests/test_mood_date.py -q |
| 02 | API contract foundation | Add Pydantic mood ingest and response schemas plus dependency seams for fake token, bind IP, and timezone values in tests without reading real local configuration. | Schemas and dependency injection let later app and security work stay deterministic. | autonomous_executor | 01 | src/api/schemas.py, src/api/dependencies.py, tests/test_api_security.py, docs/gstack/s02-mood-api-autoplan.md | src/api/schemas.py, src/api/dependencies.py, tests/test_api_security.py | Tests validate feeling range 1 to 10, optional energy range 1 to 10, optional notes, allowed context chips, optional logged_at_utc, response status ok, and injectable fake settings. | src/api/schemas.py, src/api/dependencies.py, tests/test_api_security.py | true | python -m pytest tests/test_api_security.py -q |
| 03 | Security and route surface | Implement or extend FastAPI app composition, all S02 routes, same-host read middleware, X-Mood-Token dependency using secrets.compare_digest, CORS-disabled posture, protected retrospective placeholders, and POST mood rate limiting only through an already available in-memory limiter dependency. | Security controls must wrap routes before persistence is connected, and placeholders preserve retrospective-only S02 scope. | autonomous_executor | 02 | src/api/__init__.py, src/api/app.py, src/api/security.py, src/api/dependencies.py, src/api/schemas.py, tests/test_api_security.py, docs/gstack/health-data-hub-office-hours.md | src/api/__init__.py, src/api/app.py, src/api/security.py, tests/test_api_security.py | Tests pass for missing token 401, bad token 401, remote GET 403 before handler logic, loopback GET accepted with fake valid token, same-host LAN_BIND_IP GET accepted with fake valid token, LAN POST accepted with fake valid token through a test persistence override, no CORS middleware, no prospective output, and rate-limit behavior when the limiter dependency is available. If the limiter dependency is unavailable and package-file edits would be needed, the row blocks instead of widening scope. | src/api/__init__.py, src/api/app.py, src/api/security.py, src/api/dependencies.py, src/api/schemas.py, tests/test_api_security.py | true | python -m pytest tests/test_api_security.py -q |
| 04 | Mood persistence | Connect POST /api/mood to the discovered source-compatible S01 warehouse mood helper through the dependency seam, preserving append-only mood_entries plus mood_current correction semantics and not duplicating writer logic. | Persistence comes after security and date handling so writes are token-gated and correctly attributed. | autonomous_executor | 01-03 | src/api/app.py, src/api/dependencies.py, tests/test_api_security.py, tests/test_mood_correction.py, src/warehouse/warehouse.py as read-only discovery surface if present | src/api/app.py, tests/test_api_security.py, tests/test_mood_correction.py | Tests pass for valid fake LAN POST, server-side mood_date resolution, second POST appending a new row, supersedes_log_id pointing to prior log_id, mood_current updating to the new log_id, current-mood join returning one row, and superseded rows remaining. If no compatible S01 helper exists, stop as a blocker and do not create a second writer. | src/api/app.py, src/api/dependencies.py, tests/test_api_security.py, tests/test_mood_correction.py | true | python -m pytest tests/test_api_security.py tests/test_mood_correction.py -q |
| 05 | Deterministic test closure | Harden the three S02 test files so all security, mood-date, and correction coverage uses fake tokens, simulated client hosts, dependency overrides, and temporary or mocked storage only. | Autonomous review artifacts need deterministic evidence before they can cite code behavior. | autonomous_executor | 01-04 | tests/test_api_security.py, tests/test_mood_date.py, tests/test_mood_correction.py, src/api/app.py, src/api/security.py, src/api/mood_date.py, src/api/schemas.py, src/api/dependencies.py | tests/test_api_security.py, tests/test_mood_date.py, tests/test_mood_correction.py | Full S02 pytest command passes without a real phone, real Shortcut, real LAN, real token, production data, provider APIs, or external network. | tests/test_api_security.py, tests/test_mood_date.py, tests/test_mood_correction.py | true | python -m pytest tests/test_api_security.py tests/test_mood_date.py tests/test_mood_correction.py -q |
| 06 | autonomous_gate_review security | Write the S02 autonomous_gate_review security artifact linking implemented controls to deterministic tests without claiming human signoff. | Security boundary work needs the authorized autonomous review substitution after tests exist. | autonomous_executor | 03 and 05 | docs/reviews/s02-autonomous-security-review.md, src/api/security.py, src/api/app.py, tests/test_api_security.py, docs/briefs/s02-mood-api.autonomous-brief.md | docs/reviews/s02-autonomous-security-review.md | Artifact exists, cites deterministic tests, documents same-host reads, X-Mood-Token, secrets.compare_digest, POST mood rate limit, CORS-disabled posture, no token or body logging, and states No human signoff was performed. | docs/reviews/s02-autonomous-security-review.md | false | test -s docs/reviews/s02-autonomous-security-review.md; grep -F "autonomous_gate_review" docs/reviews/s02-autonomous-security-review.md; grep -F "tests/test_api_security.py" docs/reviews/s02-autonomous-security-review.md; grep -F "X-Mood-Token" docs/reviews/s02-autonomous-security-review.md; grep -F "same-host" docs/reviews/s02-autonomous-security-review.md; grep -F "secrets.compare_digest" docs/reviews/s02-autonomous-security-review.md; grep -F "CORS" docs/reviews/s02-autonomous-security-review.md; grep -F "rate limit" docs/reviews/s02-autonomous-security-review.md; grep -F "No human signoff" docs/reviews/s02-autonomous-security-review.md |
| 07 | autonomous_gate_review privacy and closure | Write the S02 autonomous_gate_review privacy artifact and run S02 acceptance checks named by the slice plan. | Privacy evidence and hygiene checks close the autonomous substitution after code, tests, and security review artifact exist. | autonomous_executor | 04-06 | docs/reviews/s02-autonomous-privacy-review.md, docs/reviews/s02-autonomous-security-review.md, tests/test_api_security.py, tests/test_mood_date.py, tests/test_mood_correction.py, scripts/check_no_tracked_data.py, scripts/check_autonomous_review_exists.py, scripts/verify_slice.py | docs/reviews/s02-autonomous-privacy-review.md | Artifact exists, cites deterministic tests, documents local-only mood data, no third-party transmission, no secrets or request bodies in general logs, no tracked raw health data, source-compatible quarantine handling only, and states No human signoff was performed. Final S02 checks complete with zero exit status. | docs/reviews/s02-autonomous-privacy-review.md | false | test -s docs/reviews/s02-autonomous-privacy-review.md; grep -F "autonomous_gate_review" docs/reviews/s02-autonomous-privacy-review.md; grep -F "tests/test_api_security.py" docs/reviews/s02-autonomous-privacy-review.md; grep -F "tests/test_mood_correction.py" docs/reviews/s02-autonomous-privacy-review.md; grep -F "local-only" docs/reviews/s02-autonomous-privacy-review.md; grep -F "no third-party" docs/reviews/s02-autonomous-privacy-review.md; grep -F "raw health data" docs/reviews/s02-autonomous-privacy-review.md; grep -F "No human signoff" docs/reviews/s02-autonomous-privacy-review.md; python -m pytest tests/test_api_security.py tests/test_mood_date.py tests/test_mood_correction.py -q; python scripts/check_no_tracked_data.py; python scripts/check_autonomous_review_exists.py S02; python scripts/verify_slice.py S02 --json |

## 3. Phase Details

### Row ordering and execution intent

Rows 01 through 02 establish deterministic primitives before route behavior exists. Row 01 isolates mood-date attribution so late-night logs are resolved consistently without touching secrets, network state, or persistence. Row 02 creates schemas and dependency seams so tests can use fake values rather than real configuration.

Row 03 implements the security boundary and route surface before any persistence path is connected. This ordering ensures every S02 endpoint is token-gated and every read route is same-host restricted before a handler can write or reveal mood data. The retrospective read routes are protected placeholders in S02. They must not implement model output, counterfactual computation, predictions, recommendations, causal claims, medical language, Autopilot behavior, or Coach behavior.

Row 04 connects mood POST persistence only after date handling and security controls exist. This row is conditional on discovering a source-compatible S01 warehouse helper. If the helper does not clearly support append-only mood entries, mood_current updates, corrections, and temporary or mocked test execution, the row must stop as a blocker. The playbook does not authorize schema edits, direct writes to runtime data files, or a duplicate writer path in the API layer.

Row 05 hardens deterministic tests across the three S02 test files. It is not a product-scope expansion row. It should close coverage gaps using fake tokens, simulated client hosts, test dependency overrides, and temporary or mocked persistence only.

Rows 06 and 07 create the autonomous security and privacy review artifacts after tests and code exist. These are docs-only rows with concrete content checks and do not claim human signoff. They complete the authorized `autonomous_gate_review` substitution for high-risk security and privacy concerns.

### Manual-gate disposition for this autonomous run

Active human approval gate rows are not part of this playbook. The autonomous brief forbids active gate completion flows for this run, so former signoff triggers are handled as follows.

Security boundary trigger: row 03 implements authentication, same-host reads, disabled CORS posture, and POST mood rate limiting. Disposition is deterministic security tests in `tests/test_api_security.py` plus the row 06 `autonomous_gate_review` artifact. If a security behavior is not supported by `docs/gstack/s02-mood-api-autoplan.md` or `docs/gstack/health-data-hub-office-hours.md`, execution must stop as a blocker.

Privacy and raw-health-data trigger: rows 04 and 07 touch mood persistence behavior and privacy evidence. Disposition is fake or temporary storage in tests, the no-tracked-data check named by the slice plan, and the row 07 `autonomous_gate_review` artifact. Real mood data, real health payloads, tokens, snapshots, quarantine payloads, and DuckDB files must not be introduced into tracked files or general logs.

Warehouse integration trigger: row 04 relies on a S01 helper whose exact interface was not attached as implementation evidence in this stage. Disposition is read-only discovery followed by reuse if source-compatible. If no compatible helper exists, execution blocks. Do not modify warehouse schema, create a parallel writer, or change runtime data paths to force progress.

Dependency trigger: rows 02 and 03 require FastAPI, Pydantic, pytest, and the planned in-memory limiter dependency. Dependency availability was not verified by attached package manifests. If a dependency is missing and passing the slice would require package-manifest edits, execution blocks because package-file writes are outside S02 authority.

Acceptance-script trigger: row 07 uses scripts named by the S02 slice plan. Their implementations were not attached in this stage. If a required script path is absent or cannot run, execution blocks rather than inventing substitute evidence.

### Verification command handling

Every execution row has a non-empty `required_verification_commands` cell. Rows 01 through 05 are red-green behavioral rows and must not be treated as complete until their pytest commands pass with zero exit status in the execution environment. Rows 06 and 07 are docs-only rows and still require concrete content checks through `test`, `grep`, pytest, and slice hygiene commands.

Row-level commands are intentionally local and deterministic. They must not require a real iOS Shortcut, real phone, real home Wi-Fi, real token, real provider API, real production DuckDB database, real raw health payload, or external network call.

The command set represented by the rows is:

- `python -m pytest tests/test_mood_date.py -q`
- `python -m pytest tests/test_api_security.py -q`
- `python -m pytest tests/test_api_security.py tests/test_mood_correction.py -q`
- `python -m pytest tests/test_api_security.py tests/test_mood_date.py tests/test_mood_correction.py -q`
- `test -s docs/reviews/s02-autonomous-security-review.md`
- `grep -F "autonomous_gate_review" docs/reviews/s02-autonomous-security-review.md`
- `grep -F "tests/test_api_security.py" docs/reviews/s02-autonomous-security-review.md`
- `grep -F "X-Mood-Token" docs/reviews/s02-autonomous-security-review.md`
- `grep -F "same-host" docs/reviews/s02-autonomous-security-review.md`
- `grep -F "secrets.compare_digest" docs/reviews/s02-autonomous-security-review.md`
- `grep -F "CORS" docs/reviews/s02-autonomous-security-review.md`
- `grep -F "rate limit" docs/reviews/s02-autonomous-security-review.md`
- `grep -F "No human signoff" docs/reviews/s02-autonomous-security-review.md`
- `test -s docs/reviews/s02-autonomous-privacy-review.md`
- `grep -F "autonomous_gate_review" docs/reviews/s02-autonomous-privacy-review.md`
- `grep -F "tests/test_api_security.py" docs/reviews/s02-autonomous-privacy-review.md`
- `grep -F "tests/test_mood_correction.py" docs/reviews/s02-autonomous-privacy-review.md`
- `grep -F "local-only" docs/reviews/s02-autonomous-privacy-review.md`
- `grep -F "no third-party" docs/reviews/s02-autonomous-privacy-review.md`
- `grep -F "raw health data" docs/reviews/s02-autonomous-privacy-review.md`
- `grep -F "No human signoff" docs/reviews/s02-autonomous-privacy-review.md`
- `python scripts/check_no_tracked_data.py`
- `python scripts/check_autonomous_review_exists.py S02`
- `python scripts/verify_slice.py S02 --json`

The playbook authoring step has not run these commands and does not claim any command output.

### Endpoint and behavior boundaries

S02 route surface is limited to:

- `POST /api/mood`
- `GET /api/health`
- `GET /api/insights/{date}`
- `GET /api/insights/latest_logged_day`
- `GET /api/counterfactuals/{date}`
- `GET /api/counterfactuals/latest_logged_day`

The read endpoints must be same-host restricted and token-gated. In S02 they are placeholders or gated empty surfaces only. They must not return model contributors, predictions, recommendations, counterfactual computations, confidence claims, medical guidance, or causal explanations.

The mood POST request schema must preserve the design contract: required `feeling` from 1 to 10, optional `energy` from 1 to 10, optional `notes`, optional `context_chips` from the documented literal set, and optional `logged_at_utc` defaulting server-side when absent. The response must include `log_id`, server-resolved `mood_date`, and `status` equal to `ok`.

The mood-date function must apply the 04:00 local cutoff through explicit timezone input. Runtime timezone acquisition remains an implementation uncertainty unless a source-compatible configuration surface is discovered during execution.

## 4. Shared Guidance

Stay within the table’s allowed write roots. Do not widen the slice for convenience.

Do not write to warehouse schema, database layers, ingestion code, feature engineering, model code, UI code, CI configuration, package manifests, repository root, runtime data directories, hidden operational roots, or secret stores.

Do not write to `src/warehouse/` or `src/db/`. Those areas are read-only discovery surfaces for S02 if present. If the required S01 helper is missing or incompatible, stop as a blocker.

Do not write to `data/`, `private/`, environment secret files, raw payload locations, quarantine locations, snapshot locations, or DuckDB files as part of repository changes. Tests must use fake values, monkeypatched environment, dependency overrides, temporary directories, mocks, or in-memory stores.

Do not access real `MOOD_TOKEN`, real `LAN_BIND_IP`, real iOS Shortcut secrets, real Oura credentials, real 8 Sleep credentials, real mood data, real provider data, or real production warehouse files.

Do not log token values, request headers, request bodies, raw payloads, comparison results, secrets, or private local paths. Error handling should summarize safely and avoid general-log disclosure.

Do not add CORS middleware. Streamlit and UI integration are outside S02. Browser cross-origin behavior is not part of this slice.

Do not implement Oura ingestion, 8 Sleep ingestion, provider OAuth, Open Wearables, pyEight, feature engineering, HRV z-score computation, model training, baseline gates, sign-stability bootstrap, SHAP, counterfactual computation, Streamlit UI, launchd schedules, backups, restore, Tailscale, public HTTPS, webhooks, Autopilot, Coach, Garmin, Withings, chest strap, nutrition, or multi-daily mood logging.

Do not introduce causal, medical, coaching, recommendation, or tomorrow-facing language. Use retrospective and non-claiming placeholders only.

If tests cannot simulate client hosts through the available test tooling, factor same-host logic into a pure helper and test that helper deterministically. Do not require real LAN behavior.

If the in-memory limiter dependency is absent and dependency-manifest writes are required, stop as a blocker. Do not silently implement an unapproved custom limiter or broaden allowed write roots.

If an autonomous review artifact cannot truthfully cite deterministic tests and code surfaces, stop as a blocker. Do not use the artifact to paper over unresolved security or privacy ambiguity.

## 5. Risks And Contingencies

Implementation files under `src/api/`, `src/warehouse/`, `tests/`, `scripts/`, package manifests, and CI were not attached as source code in this final stage. The playbook therefore treats specific function names, fixtures, dependency declarations, script behavior, and CI behavior as execution-time uncertainties.

S01 warehouse helper uncertainty is the largest implementation risk. Row 04 may proceed only if a compatible helper is discovered and can be used without schema edits, production data, or a duplicate writer path. If not, record a blocker and stop S02.

S01 quarantine pathway uncertainty remains unresolved. The playbook does not authorize a new raw request quarantine writer. Reuse an existing pathway only if its interface is source-compatible and testable without writing raw payloads to tracked or runtime data paths.

Dependency availability is unverified. If FastAPI, Pydantic, pytest, or the planned in-memory limiter dependency are unavailable and passing the slice requires package-file edits, S02 blocks pending new authority.

Acceptance script implementations are unreviewed in this stage. Row 07 may run the named commands, but if a script is absent or fails because its expected format differs from the generated artifacts, the run must surface that failure. Do not replace absent scripts with invented evidence.

Real iOS Shortcut behavior, iOS Local Network permission, real home-Wi-Fi reachability, real LAN source IP routing, Uvicorn binding on the target Mac, and token rotation are outside deterministic S02 repository verification. They must not be claimed as passed by this playbook.

The read endpoint placeholder shape is intentionally constrained. If execution reveals ambiguity about status codes or response bodies, keep responses protected, retrospective, and non-claiming rather than implementing model or counterfactual semantics.

The review artifacts are not compliance certifications and are not production-readiness claims. They should describe implemented controls, deterministic tests, known limits, and the absence of human signoff.

Any discovered tracked secret, raw health data file, quarantine payload, snapshot, DuckDB file, or private runtime artifact is a blocker. Do not silently edit unrelated files outside S02 write roots to hide or repair the issue.

## 6. Immediate Next Actions

1. Save this artifact under `docs/playbooks/s02-mood-api-loop.playbook.md`.

2. Run PO `list-items --playbook docs/playbooks/s02-mood-api-loop.playbook.md` to inspect the parsed execution items.

3. Run PO `doctor --playbook docs/playbooks/s02-mood-api-loop.playbook.md` to check playbook structure before execution.

4. Let the repository autonomous validation workflow run before plan-orchestrator execution. If validation reports issues, revise the playbook instead of bypassing the validator.

5. Confirm the execution table has exactly the required columns, step ids `01` through `07`, no extra derived columns, non-empty verification commands for every row, and no active human approval gate rows.

6. Confirm every allowed write root remains a narrow repo-relative S02 path and that no row authorizes writes to warehouse schema, runtime data, secret stores, package manifests, CI, repository root, or broader product areas.

7. Do not treat this document as evidence that PO verification passed. No PO `list-items`, PO `doctor`, autonomous validator, pytest, script, or slice-verification output was provided as an input to this authoring stage.

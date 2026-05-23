**Scope:** Three generalizable zero-human operating frameworks for running Keel end-to-end, applied to Health Data Hub v1.
**Confidence:** 0.82. The plan-orchestrator/Keel parts are strongly grounded in the public repos; the “Ralph Loop” and agent-manager parts are current but still moving fast.
**Perspective:** The central issue is not “can an agent run commands?” It can. The central issue is whether the experiment preserves a truthful audit trail when Keel was explicitly designed around human-held gates.

<research_summary>

## 1. Ralph Loop findings

The Ralph Loop pattern is a useful fit for this project, but only as an **outer persistence loop**, not as the execution kernel. The core idea is simple: keep invoking a fresh coding agent on the next unfinished item, preserve state in files and git rather than in chat context, run objective checks, commit progress, and repeat until the whole task list passes. The original Ralph repo describes an autonomous loop that runs Amp or Claude Code repeatedly until `prd.json` items are complete, with durable memory in git history, `progress.txt`, and `prd.json`; each loop starts a fresh agent instance with clean context. ([GitHub][1])

That maps well to Health Data Hub because the build already decomposes into slices: S01 warehouse, S02 mood API, S03 ingestion, S04 features, S05 model, S06 counterfactual, S07 UI, S08 backup/restore, S09 evaluation. The Ralph-style outer loop should not implement those slices directly. It should pick the next slice, invoke Keel, inspect Keel’s state, update durable progress, and continue.

The important best-practice from Ralph-style systems is: **state lives outside the agent**. Addy Osmani’s write-up frames this cleanly: use `prd.json`, `progress.txt`, and project instructions so the agent can be amnesiac while the filesystem is not. ([Addy Osmani][2]) Matt Pocock’s Ralph guidance adds two directly relevant points: make items small enough that a single loop can finish one logical change, and run feedback loops such as tests, type checks, linters, browser checks, or pre-commit after each change. ([AI Hero][3])

For this project, the Ralph Loop should be adapted as:

```text
while verify_v1_not_done:
    read autonomy_state.json + slices.json + PO state
    choose next incomplete slice
    run Keel plan/compile/PO/ship cycle
    inspect status and evidence
    make autonomous decision under explicit policy
    log event
    repeat
```

The key weakness of raw Ralph Loop is that it is too prompt-driven. It may work for codebase refactors, but the Health Data Hub experiment needs stronger state inspection, gate semantics, and failure classification than a generic “run Claude again until done” shell loop.

## 2. Plan Orchestrator / Keel findings

Keel’s architecture is already close to what this experiment needs: gstack for planning, compiler or SWR for producing `markdown_playbook_v1`, plan-orchestrator as the execution kernel, dual audits, isolated worktrees, explicit verification commands, and shippable branches. Keel’s README describes the pipeline as idea → gstack → compiler → `markdown_playbook_v1` → plan-orchestrator → gstack ship cycle, with local-first execution, isolated worktrees, dual independent audits, and explicit safety boundaries. ([GitHub][4])

The compiler is also useful here because it constrains model-generated implementation plans. It translates approved design/autoplan/brief artifacts into a normalized markdown playbook, writes validation sidecars, rejects dangerous or broad scopes, and leaves execution to plan-orchestrator. ([GitHub][5]) The plan-orchestrator playbook contract requires concrete columns such as `action`, `deliverable`, `exit_criteria`, `allowed_write_roots`, and `requires_red_green`, which is exactly the structure an autonomous operator needs to parse and supervise. ([GitHub][6])

The hard problem is gates. Plan-orchestrator is intentionally not zero-human. Its README says terminal states include `passed`, `awaiting_human_gate`, `blocked_external`, and `escalated`; it also says manual gates remain human stops and agents must not auto-approve them. ([GitHub][7]) The operations book is even more explicit: a worker agent must not call `mark-manual-gate` or treat “complete the run” as permission to approve human gates. ([GitHub][8])

So the truthful conclusion is:

**A zero-human Keel experiment must not let an AI agent impersonate a human gate reviewer.** That would corrupt the very artifact trail the experiment is supposed to study.

The clean solution is to introduce an **autonomy profile**:

```text
manual_gate rows are not allowed in autonomous playbooks
former manual gates become autonomous_gate_review artifacts
the operator records "approved_by_autonomous_operator", not "human approved"
blocked_external is allowed, but evidence must be gathered by the outer operator and stored locally
if PO reaches awaiting_human_gate, the autonomous framework logs "manual_gate_leak" and replans/recompiles
```

This preserves the experiment’s honesty. It does not pretend the stock Keel human gate was cleared. It tests a modified operating mode: **Keel with autonomous gate substitution**.

Plan-orchestrator’s supervision layer is highly relevant. It monitors liveness, diagnoses stop states, uses `run_state.json` as the authority, handles `blocked_external` through local evidence directories, and keeps supervision artifacts locally. ([GitHub][9]) The operator guide reinforces the same invariants: one worktree per attempt, verification before audit, dual audit, bounded fix loops, human/blocked/escalated terminal handling, and clean preflight expectations. ([GitHub][10])

## 3. Agent manager comparison

### VS Code

VS Code is useful as an **interactive cockpit**, not as the primary zero-human operator. Current VS Code agent docs show support for third-party agents such as Claude and OpenAI Codex, with editor, terminal, debug, test, and source-control integration. Claude sessions can run locally or in the cloud, and Codex can run interactively or in background. ([Visual Studio Code][11])

For this experiment, VS Code’s weaknesses are decisive:

* It is optimized for a human watching and steering.
* Permission modes and UI affordances create friction for a true 24/7 loop.
* It does not naturally own Keel’s state machine.
* It is harder to make a clean JSONL audit ledger from GUI operations.

Verdict: **good for debugging and post-mortem inspection; not the primary autonomous manager.**

### Conductor Mac app

The Conductor Mac app is designed for running multiple Claude Code agents in parallel, each in an isolated copy of the codebase, with tools for reviewing and merging their changes. Vercel’s docs describe it as a Mac app for managing multiple Claude Code agents side by side. ([Vercel][12]) YC’s company profile similarly describes Conductor as a way to start multiple coding agents in isolated workspaces, monitor them, create PRs, and merge changes. ([Y Combinator][13])

For this project, Conductor Mac is attractive but probably not ideal:

* Keel already creates isolated worktrees.
* Parallelism is dangerous because the Health Data Hub slices are sequentially dependent.
* The experiment is about Keel operating reliability, not agent-parallel throughput.
* A GUI manager makes long-running headless observability harder.

Verdict: **useful as a sidecar for parallel review experiments; not the main operator.**

### Microsoft Conductor CLI

Microsoft’s Conductor CLI is more relevant than the Mac app if the goal is a deterministic workflow manager. Its repo describes YAML-based multi-agent workflows with deterministic routing, script steps, parallel execution, validation, dashboarding, conditional routing, and human-in-the-loop support. ([GitHub][14])

This could supervise Keel, but it adds another orchestrator above plan-orchestrator. That creates a “double-orchestrator” problem:

```text
Conductor owns DAG state
PO owns run_state.json
Ralph/progress files may own task state
git owns branch state
```

That can work, but the extra complexity is negative for this specific experiment.

Verdict: **best off-the-shelf workflow manager if you need one, but still more complex than a Keel-native Python supervisor.**

### Better option

The better option is a **Keel-native autonomous supervisor**: a small Python daemon or shell/Python harness that wraps Keel’s existing commands, parses Keel/PO status JSON, records events, and invokes Claude/Codex only where judgment or coding is needed.

This is simpler than VS Code, less GUI-dependent than Conductor Mac, and less duplicative than Microsoft Conductor CLI. It also keeps the experiment focused: the object of study remains Keel plus autonomous gate substitution.

## 4. Best practices for long-running autonomous agent tasks

The strongest recurring practices across current agent-loop guidance are:

1. **Use durable state outside chat context.** Claude Code sessions begin fresh unless project memory files carry context; Anthropic recommends `CLAUDE.md` and concise project memory for persistent instructions. ([Claude API Docs][15])

2. **Give objective verification.** Anthropic’s long-running harness guidance emphasizes feature lists, progress files, git history, app-running scripts, browser validation, and tests as a way to keep the agent grounded. ([Anthropic][16]) OpenAI’s Codex best-practices guide similarly recommends clear “done when” conditions, AGENTS.md, repo commands, constraints, and verification steps. ([OpenAI Developers][17])

3. **Prefer one slice per loop.** Ralph guidance emphasizes one logical change per iteration, followed by tests and commit. ([AI Hero][3])

4. **Use fresh-context agents but stable files.** The Ralph pattern works because the fresh model reads durable state files and git history each time. ([Addy Osmani][2])

5. **Make browser/computer use a last resort.** For this project, deterministic CLI/API checks should be preferred. Browser/computer use is appropriate for OAuth setup, Oura developer pages, iOS Shortcut verification, screenshots, and local UI smoke checks, but not as the main execution loop.

6. **Treat hooks and automation as powerful but risky.** Claude hooks can run shell commands or HTTP calls at lifecycle points, but Anthropic’s docs warn that hooks require careful validation, path restrictions, and avoidance of sensitive file leakage. ([Claude API Docs][18])

7. **Use specialized subagents for review, not uncontrolled parallel mutation.** Claude subagents are designed as specialized assistants with separate context and tool permissions. ([Claude API Docs][19]) For Health Data Hub, subagents are best used for schema review, privacy review, model-gate review, UI-language review, and restore-review artifacts.

## 5. Key implication

The zero-human requirement is feasible only if the experiment explicitly changes gate semantics.

There are two honest options:

```text
Option A — Autonomous profile:
    Do not emit manual_gate rows.
    Replace them with autonomous_gate_review artifacts and verification commands.

Option B — Fork PO:
    Add an autonomous_gate terminal and mark-autonomous-gate command.
    Preserve manual_gate as human-only.
```

Option A is simpler and sufficient for the first experiment. Option B is cleaner if this becomes a reusable Keel mode.

</research_summary>

<approach_1>

**Name:** Keel-Native Autonomous Supervisor (“AutoKeel”)

## General Idea

AutoKeel is a thin autonomous operator wrapped around the existing Keel toolchain. It does not replace Keel, plan-orchestrator, compiler, SWR, or gstack. It treats those as the project’s operating system and adds one missing layer: a durable, always-on, zero-human project-owner loop.

The loop is Ralph-inspired but Keel-native. Ralph provides the outer pattern: persistent progress files, fresh agents, objective checks, commits, and repeated attempts. Keel provides the real execution kernel: playbooks, worktrees, verification, dual audit, status inspection, and ship branches.

The critical design choice is **autonomous gate substitution**. AutoKeel never asks an agent to call `mark-manual-gate`. Instead, playbooks for autonomous mode either avoid `manual_gate` rows or replace them with explicit `autonomous_gate_review` deliverables. A schema signoff becomes a machine-readable review artifact plus tests plus an independent review subagent, not a fake “human approval.”

## High-Level Workflow

1. **Initialize autonomy state**

   * Create `ops/autonomy/slices.json`.
   * Create `ops/autonomy/autonomy_state.json`.
   * Create `ops/autonomy/events.jsonl`.
   * Create `ops/autonomy/failure_ledger.jsonl`.
   * Create `AGENTS.md` and `CLAUDE.md` with Health Data Hub invariants and Keel operating rules.

2. **Run preflight**

   * Verify `/Users/aeziz-local/keel`.
   * Verify `/Users/aeziz-local/health-data-hub`.
   * Verify `keel-smoke`.
   * Verify `claude`, `codex`, `gh`, `jq`, Python, git identity, clean repo.
   * Verify no secrets or data paths are tracked by git.

3. **Load the next incomplete slice**

   * Start with S01 warehouse foundation.
   * Do not start S02 until S01 passes objective checks.
   * Do not start modeling slices if mood compliance tripwire fails.

4. **Generate or update slice brief**

   * Use gstack/autoplan or a prewritten slice brief.
   * Force the brief to include autonomous-gate semantics.
   * Record the brief under `docs/briefs/`.

5. **Compile playbook**

   * Run `keel-compile compile`.
   * Run `keel-run list-items`.
   * Run `keel-doctor`.
   * Reject any playbook containing `manual_gate` in autonomous mode.
   * Reject broad write roots, missing verification commands, unsafe commands, or v2 scope creep.

6. **Execute with plan-orchestrator**

   * Run `keel-run supervise run --next`.
   * Poll `keel-run supervise status`.
   * Parse `keel-run status` and `keel-run doctor`.

7. **Handle terminal states**

   * `passed`: ship slice, update progress, continue.
   * `blocked_external`: gather evidence externally, store local evidence, resume.
   * `awaiting_human_gate`: log `manual_gate_leak`, regenerate playbook under autonomy profile.
   * `escalated`: run bounded diagnostic/replan loop.
   * nonresponsive: restart from last known run state.

8. **Ship**

   * Create `ship/<slice>` branch from the run branch.
   * Run review/ship/land/document/retro commands or their CLI equivalents.
   * Record PR/commit/release notes.

9. **Run global v1 verification**

   * `python scripts/verify_v1.py`
   * If pass: stop.
   * If fail: continue loop with next incomplete slice or remediation slice.

10. **Continue until v1 exists**

* The loop exits only when `verify_v1.py` passes and `ops/autonomy/slices.json` marks all required v1 slices complete.

## Tools Needed

* **Keel toolchain** at `/Users/aeziz-local/keel`.
* **Product repo** at `/Users/aeziz-local/health-data-hub`.
* **plan-orchestrator** as the execution kernel.
* **gstack + compiler + SWR** for planning/playbook generation.
* **Claude Code CLI** for coding, review, and specialized review subagents.
* **Codex CLI** for independent review, debugging, or alternate implementation attempts.
* **Python supervisor daemon**, e.g. `ops/autonomy/autokeel.py`.
* **tmux or launchd** for 24/7 process persistence.
* **JSONL event log** for observability.
* **Optional Playwright/MCP browser harness** for Streamlit UI checks and OAuth/browser tasks.
* **GitHub CLI** for PR/branch operations.

## Setup Complexity

**Medium.**

The concept is simple, but implementation requires careful state handling:

* Parse Keel/PO JSON status reliably.
* Build an autonomy profile that prevents fake human approvals.
* Implement evidence directories and event logs.
* Add a `verify_v1.py` acceptance script.
* Add retry and stale-run detection.

It is still less complex than adding a separate workflow engine such as Conductor above Keel.

## Reliability Assessment

**Expected reliability:** highest of the three approaches for this project.

Why:

* It uses PO’s existing worktree isolation and verification model.
* It records every event in files.
* It avoids GUI dependency.
* It does not rely on a single long chat context.
* It can restart from `autonomy_state.json`, git history, and PO status.

Known failure modes:

* **Manual gate leak:** compiler emits a real `manual_gate`; AutoKeel must reject and recompile.
* **External evidence failure:** Oura OAuth, pyEight, or iOS Shortcut cannot be completed; AutoKeel must apply tripwire fallback.
* **Agent false completion:** Claude/Codex claims done while tests fail; `verify_v1.py` prevents loop exit.
* **State divergence:** PO state, git branch state, and autonomy state disagree; supervisor must reconcile by trusting PO `run_state.json` for active runs and git for shipped slices.
* **Long-running credential/browser task stalls:** supervisor must classify and reattempt with narrower prompts or fallback.
* **Health data leakage:** all logs must redact tokens, raw provider payloads, and personal health values unless explicitly part of local evidence.

Mitigations:

* Use one active PO run at a time.
* Require deterministic verification commands.
* Use independent review agents for high-stakes gates.
* Enforce no raw data/secrets in git.
* Record every autonomous decision with evidence path.

## Integration with Keel

AutoKeel wraps the existing Keel cycle:

```text
gstack/autoplan
    ↓
approved slice brief
    ↓
keel-compile or SWR
    ↓
markdown_playbook_v1
    ↓
keel-run list-items + doctor
    ↓
plan-orchestrator supervised run
    ↓
audit + fix + pass/block/escalate
    ↓
ship branch + PR/release notes
    ↓
next slice
```

The integration points are:

1. **Before compile**

   * Inject `autonomy_profile: true` into the slice brief.
   * State that manual gates are disallowed.
   * Require autonomous review artifacts instead.

2. **After compile**

   * Parse the playbook.
   * Reject rows with:

     * `manual_gate`
     * broad `allowed_write_roots`
     * missing verification commands
     * forbidden Health Data Hub vocabulary in UI rows
     * v2 features in v1 rows

3. **During PO run**

   * Use `keel-run supervise status`.
   * Use `keel-run status --format json`.
   * Use `keel-run doctor --format json`.
   * Store command output summaries in `ops/autonomy/events.jsonl`.

4. **At blocked external**

   * Let the outer operator gather real local evidence.
   * Store under `private/evidence/<slice>/<timestamp>/`.
   * Resume with `--external-evidence-dir`.

5. **At ship**

   * Create `ship/<slice>` branch.
   * Run review/ship/land/document/retro.
   * Mark the slice complete only after verification passes.

## Decision-Making at Gates

AutoKeel uses a policy file:

```yaml
autonomous_mode: true
manual_gate_policy: forbidden
if_manual_gate_reached: log_failure_and_recompile
external_evidence_policy: ai_operator_may_gather_local_evidence
security_gate_policy: require_two_independent_ai_reviews
model_gate_policy: never_override_baseline_or_sign_stability_gates
tripwire_policy: apply_design_doc_fallbacks_on_deadline
ship_policy: ship_only_after_tests_and_audit_pass
```

Examples:

* **Schema gate:** autonomous operator compares `schema.sql` to design doc, runs tests, invokes an independent schema-review subagent, writes `docs/reviews/s01-autonomous-schema-review.md`, then continues.
* **Security gate:** requires tests for token, same-host read restriction, no CORS, no token logging, and two independent review artifacts.
* **Model gate:** the operator can build the model pipeline, but it cannot force the UI to show model output if `N_model`, baseline gate, or sign-stability gate fails.
* **Tripwire gate:** if pyEight fails by the deadline, the operator records Oura-only v1 and continues. It does not keep trying indefinitely.

## Failure Point Detection & Recording

AutoKeel logs failures in two layers.

**Event log:**

```json
{
  "ts": "2026-05-23T19:42:11-04:00",
  "slice": "S01",
  "phase": "compile",
  "command": "keel-compile compile ...",
  "exit_code": 0,
  "status": "playbook_created",
  "evidence_path": "docs/playbooks/s01-warehouse.playbook.md"
}
```

**Failure ledger:**

```json
{
  "ts": "2026-05-24T02:13:05-04:00",
  "slice": "S02",
  "failure_class": "manual_gate_leak",
  "severity": "high",
  "description": "Compiled playbook contained manual_gate in autonomous mode.",
  "action_taken": "Rejected playbook and regenerated brief with autonomous_gate_review artifact.",
  "evidence_path": "ops/autonomy/failures/S02-manual-gate-leak-20260524.md"
}
```

Failure classes should include:

* `manual_gate_leak`
* `blocked_external_missing_evidence`
* `provider_auth_failure`
* `test_failure`
* `audit_failure`
* `unsafe_write_root`
* `secret_leak_risk`
* `forbidden_ui_language`
* `model_gate_failed`
* `tripwire_triggered`
* `stale_run`
* `agent_false_done`
* `state_divergence`
* `ship_failure`

## Loop Continuation Strategy

The while-loop is implemented by a daemon:

```python
while not verify_v1():
    state = load_state()
    slice_id = choose_next_slice(state)

    ensure_brief(slice_id)
    ensure_playbook(slice_id, autonomy_profile=True)
    validate_playbook(slice_id)

    run_id = start_or_resume_po(slice_id)
    status = inspect_po(run_id)

    if status == "passed":
        ship_slice(slice_id, run_id)
        mark_slice_complete(slice_id)
    elif status == "blocked_external":
        gather_external_evidence(slice_id, run_id)
        resume_with_evidence(run_id)
    elif status == "awaiting_human_gate":
        record_failure("manual_gate_leak", slice_id, run_id)
        regenerate_without_manual_gate(slice_id)
    elif status == "escalated":
        classify_and_replan(slice_id, run_id)
    else:
        heartbeat_and_continue()
```

The loop exits only when:

```text
python scripts/verify_v1.py returns 0
AND required slices are marked complete
AND no critical open failure exists
```

## Pros

* Most Keel-native.
* Minimal extra machinery.
* Strong observability.
* Preserves PO as execution kernel.
* Avoids GUI dependence.
* Cleanest way to preserve truthfulness around human gates.
* Generalizes easily to other Keel projects by changing `slices.json`, `policy.yaml`, and `verify_project.py`.

## Cons

* Requires writing a custom supervisor.
* Requires an autonomy profile or careful playbook rewriting.
* Not stock Keel semantics; it is Keel with autonomous gate substitution.
* External device/browser tasks remain brittle.
* The operator can still get stuck if acceptance criteria are too vague.

## Best Suited For

This specific Health Data Hub experiment, and any Keel project where the goal is to study zero-human execution while preserving an auditable record of where autonomy fails.

</approach_1>

<approach_2>

**Name:** Ralph-on-Keel Iteration Harness

## General Idea

This approach uses Ralph Loop as the primary operating framework and treats Keel as the tool the Ralph agent invokes. Instead of building a custom Keel-native supervisor, you create a `prd.json` where each item is a Keel slice. The Ralph loop repeatedly launches a fresh Claude Code or Amp/Codex session, tells it to complete one unfinished slice, run Keel, run checks, commit progress, and update `prd.json`.

This is the simplest autonomous framework. It is close to the public Ralph pattern: one feature at a time, progress file, tests, git commits, fresh agent, repeat. ([GitHub][1])

The difference from raw Ralph is that “feature implementation” is not direct coding. The feature implementation is “drive the Keel cycle for this slice.” In other words:

```text
Ralph chooses the slice.
Keel executes the slice.
Ralph records whether it passed.
```

## High-Level Workflow

1. **Create `prd.json`**

   * One item per Health Data Hub slice.
   * Each item has:

     * `id`
     * `priority`
     * `description`
     * `keel_slice`
     * `acceptance_checks`
     * `passes: false`

2. **Create `progress.txt`**

   * Running narrative of attempts, failures, commits, and next actions.

3. **Create `AGENTS.md`**

   * Explain Keel workflow.
   * Explain Health Data Hub invariants.
   * Forbid fake human gate approval.
   * Define autonomous gate substitution.

4. **Run Ralph loop**

   * Fresh agent reads `prd.json`, `progress.txt`, `AGENTS.md`, git log, and Keel docs.
   * Agent chooses highest-priority item with `passes:false`.
   * Agent runs gstack/compile/PO/ship for that slice.
   * Agent runs acceptance checks.
   * Agent updates `prd.json` and `progress.txt`.
   * Agent commits.
   * Loop repeats.

5. **Use objective evaluator**

   * After each iteration, run:

     * tests
     * `keel-doctor`
     * `scripts/verify_slice.py`
     * eventually `scripts/verify_v1.py`

6. **Continue until done**

   * Exit only when all `prd.json` items pass and global v1 verification passes.

## Tools Needed

* Ralph Loop repo or a custom Ralph-style shell script.
* Claude Code CLI or Amp.
* Optional Codex CLI for independent review.
* Keel toolchain.
* `prd.json`.
* `progress.txt`.
* `AGENTS.md`.
* `scripts/verify_slice.py`.
* `scripts/verify_v1.py`.
* Git.
* tmux or launchd.

## Setup Complexity

**Low to Medium.**

This is the fastest to stand up:

* Less custom state-machine code.
* Uses Ralph’s existing pattern.
* Easy to explain and generalize.
* Minimal new infrastructure.

But the simplicity hides risk: the agent has more freedom and less deterministic state management than in Approach 1.

## Reliability Assessment

**Expected reliability:** moderate.

It will probably make progress on simple slices, especially S01 warehouse and S04 features. It is less reliable on slices with PO terminal states, external evidence, or security/model gates.

Known failure modes:

* **Premature completion:** agent marks `passes:true` without enough evidence.
* **Gate confusion:** agent tries to clear a manual gate or gets stuck waiting.
* **Prompt drift:** agent starts coding directly instead of using Keel.
* **Progress-file corruption:** agent overwrites `prd.json` incorrectly.
* **Repeated loop churn:** agent keeps trying the same failed step without reclassifying the failure.
* **Weak failure taxonomy:** unless added explicitly, Ralph records less structured diagnostic information than AutoKeel.

Mitigations:

* Make `prd.json` append-safe or validate it with schema.
* Add `scripts/evaluate_prd.py`.
* Add hard rule: if PO reaches `awaiting_human_gate`, set item to `blocked_manual_gate_semantics`, not pass.
* Add maximum iteration budget per slice.
* Add “replan item” if the same failure appears 3 times.
* Require git commit after every successful slice, not after every attempt.

## Integration with Keel

The Ralph agent receives a task prompt like:

```text
You are operating Keel autonomously.

Pick the highest-priority prd.json item with passes=false.
Do not implement directly unless the Keel playbook row requires it.
Run the Keel cycle:
  gstack/autoplan if needed
  keel-compile or SWR
  keel-run list-items
  keel-doctor
  keel-run supervise run/resume
  inspect PO status
  ship from a ship branch
Run the item's acceptance checks.
Update progress.txt and prd.json.
Commit.
If you encounter awaiting_human_gate, do not approve it.
Record manual_gate_leak and replan with autonomous gate substitution.
```

The Keel cycle remains the same:

```text
gstack → compiler/SWR → playbook → PO → audit → ship
```

But unlike Approach 1, Ralph is both the task allocator and the operator. That is simpler but less controlled.

## Decision-Making at Gates

Gate handling lives in `prd.json` and `AGENTS.md`.

Example `prd.json` item:

```json
{
  "id": "S01",
  "title": "Warehouse foundation",
  "passes": false,
  "acceptance_checks": [
    "python -m pytest tests/warehouse -q",
    "python scripts/check_schema_contract.py",
    "python scripts/check_no_tracked_data.py"
  ],
  "autonomous_gate_policy": {
    "manual_gate_allowed": false,
    "required_review_artifacts": [
      "docs/reviews/s01-autonomous-schema-review.md"
    ]
  }
}
```

Gate decisions are made by the active agent, but must be backed by:

* passing tests
* review artifacts
* no forbidden playbook rows
* no open critical failure ledger entries
* acceptance script pass

This is weaker than Approach 1 because the same loop agent may decide and record. A mitigation is to require independent review agents for high-stakes items:

```text
Claude builder creates candidate.
Codex reviewer writes review.
Claude reviewer writes second review.
Ralph agent reconciles.
```

## Failure Point Detection & Recording

Ralph uses `progress.txt` by default, but for this project it should add:

```text
ops/autonomy/events.jsonl
ops/autonomy/failure_ledger.jsonl
ops/autonomy/prd_history/
```

Each iteration should append:

```text
iteration number
agent used
slice attempted
commands run
PO run ID
terminal state
tests passed/failed
decision taken
commit hash
next action
```

A good Ralph iteration log should look like:

```text
[2026-05-24 01:22] Iteration 7
Target: S02 Mood API
Result: blocked_external
PO run: run_20260524_0118
Failure: iOS Shortcut Local Network evidence missing
Action: created evidence task S02E1; did not mark S02 passed
Next: gather local Shortcut POST proof
Commit: none
```

## Loop Continuation Strategy

The Ralph loop itself supplies continuation:

```bash
for i in $(seq 1 500); do
  claude -p "$(cat prompts/ralph_keel_operator.md)"
  python scripts/evaluate_prd.py || continue
  python scripts/verify_v1.py && break
done
```

A stronger version:

```bash
while true; do
  python scripts/verify_v1.py && exit 0
  python scripts/select_next_prd_item.py > /tmp/next_item.json
  claude -p "$(python scripts/build_operator_prompt.py /tmp/next_item.json)"
  python scripts/record_iteration.py
  python scripts/check_stale_loop.py || python scripts/create_replan_item.py
done
```

The loop should have a **per-slice retry cap**, not a global stop cap. If a slice fails repeatedly, it creates a remediation/replan item and continues if possible.

## Pros

* Fastest to implement.
* Easy to understand.
* Strongly aligned with Ralph Loop best practices.
* Generalizable to other projects.
* Good for finding failure points because it exposes where a generic autonomous loop breaks.
* Minimal infrastructure.

## Cons

* Less Keel-native than Approach 1.
* Higher risk of premature “done.”
* Weaker structured observability unless extended.
* The same agent may plan, execute, decide, and record.
* Gate handling is prompt-enforced rather than architecture-enforced.
* Can loop wastefully on external dependencies.

## Best Suited For

A fast experimental run where the goal is to see how far a simple autonomous coding loop gets before failing. It is less suitable if the goal is maximum reliability.

</approach_2>

<approach_3>

**Name:** Declarative Workflow Manager over Keel

## General Idea

This approach uses a workflow manager—preferably Microsoft Conductor CLI, not the Conductor Mac app—as a deterministic controller above Keel. The workflow is declared in YAML: preflight, plan, compile, validate, execute PO, inspect terminal state, gather evidence, run review agents, ship, verify, and loop.

The advantage is structure. Instead of a single autonomous agent deciding what to do next, deterministic workflow nodes route based on explicit status codes and JSON outputs. Agent calls are used only for bounded tasks: draft a brief, review a schema, diagnose a failure, write a fix, or summarize evidence.

The disadvantage is complexity. Keel already has an execution orchestrator. Adding Conductor creates a second orchestration layer. That can be justified if the project needs dashboards, multiple specialized agents, timeouts, conditional routing, and formal workflow replay. It is overkill for the first Health Data Hub run unless the experimental goal is to compare manager architectures.

## High-Level Workflow

1. **Create workflow YAML**

   * `workflows/healthhub_v1.yaml`
   * Defines stages:

     * preflight
     * slice selection
     * brief generation
     * compile
     * playbook validation
     * PO execution
     * terminal-state router
     * evidence collection
     * autonomous gate review
     * ship
     * v1 verification

2. **Create script nodes**

   * `scripts/keel_preflight.py`
   * `scripts/select_slice.py`
   * `scripts/validate_playbook.py`
   * `scripts/po_status_router.py`
   * `scripts/verify_slice.py`
   * `scripts/verify_v1.py`

3. **Create agent nodes**

   * `schema_reviewer`
   * `security_reviewer`
   * `model_gate_reviewer`
   * `ui_language_reviewer`
   * `failure_diagnoser`
   * `brief_author`

4. **Run workflow**

   * Conductor starts at preflight.
   * Each stage writes artifacts.
   * Routing is determined by JSON outputs, not free-form chat.

5. **Handle PO status**

   * `passed` → ship.
   * `blocked_external` → evidence collection subworkflow.
   * `awaiting_human_gate` → failure recorded; return to compile with autonomy profile.
   * `escalated` → failure diagnoser + remediation subworkflow.

6. **Loop**

   * After each slice, return to slice selection.
   * Stop only when `verify_v1.py` passes.

## Tools Needed

* Microsoft Conductor CLI or equivalent deterministic workflow runner.
* Keel toolchain.
* Claude Code and Codex.
* Python scripts for status parsing and verification.
* YAML workflow definitions.
* Local evidence directories.
* Optional dashboard.
* Optional VS Code or Conductor Mac only for observation/debugging, not primary control.

## Setup Complexity

**High.**

This has the most moving pieces:

* Keel state.
* Workflow-manager state.
* Git state.
* Agent call logs.
* Evidence directories.
* Slice state.
* Acceptance state.

The upside is replayability and structured routing. The downside is that debugging the operator becomes its own project.

## Reliability Assessment

**Expected reliability:** moderate to high after setup; lower during initial setup.

Known failure modes:

* **Double-orchestrator confusion:** Conductor says one thing, PO `run_state.json` says another.
* **Workflow rigidity:** a novel failure does not match any route.
* **Agent-node overuse:** too many agents review and mutate simultaneously.
* **Parallelism hazards:** two nodes touch overlapping files despite Keel worktree isolation.
* **Configuration drag:** YAML workflows require maintenance as Keel evolves.
* **False confidence from dashboards:** visible workflow progress can hide weak acceptance checks.

Mitigations:

* Make PO `run_state.json` authoritative for active PO runs.
* Disable parallel mutation.
* Use parallelism only for independent review agents.
* Keep workflow nodes small.
* Route unknown states to `record_failure_and_replan`, not “continue.”
* Treat Conductor as manager, not executor; PO remains executor.

## Integration with Keel

The Conductor workflow invokes Keel commands as script steps:

```yaml
- id: compile
  run: >
    keel-compile compile
    --repo-root {{ repo_root }}
    --design {{ design_doc }}
    --approved-brief {{ approved_brief }}
    --out {{ playbook }}

- id: validate_playbook
  run: python scripts/validate_playbook.py {{ playbook }}

- id: po_run
  run: >
    keel-run supervise run
    --playbook {{ playbook }}
    --next

- id: po_status
  run: python scripts/po_status_router.py {{ run_id }}
```

Agent nodes are bounded:

```yaml
- id: autonomous_schema_review
  agent: claude
  input:
    files:
      - src/db/schema.sql
      - docs/gstack/health-data-hub-office-hours.md
  output: docs/reviews/s01-autonomous-schema-review.md
```

The integration is strong if the workflow manager treats Keel as the source of execution truth. It becomes weak if Conductor tries to replace PO’s internal fix/audit/state logic.

## Decision-Making at Gates

The workflow includes a deterministic gate router:

```text
if manual_gate_detected:
    record manual_gate_leak
    regenerate playbook with autonomous profile

if blocked_external:
    launch evidence_collection subworkflow

if autonomous_review_passes and tests_pass:
    continue

if review_disagreement:
    launch failure_diagnoser

if model_gate_fails:
    do not override; record gated-empty-state success
```

For high-stakes autonomous gates, require independent agent agreement:

```text
schema/security/model/UI gate passes only if:
    verification commands pass
    Claude reviewer says pass
    Codex reviewer says pass
    policy script says pass
```

If reviewers disagree, the workflow does not ask a human. It creates a remediation item or marks the failure as unresolved and replans.

## Failure Point Detection & Recording

This approach can produce the richest observability:

* Workflow run ID.
* Node-level logs.
* Keel run ID.
* PO terminal state.
* Agent transcript summaries.
* Review artifacts.
* Command output.
* Failure classification.
* Retried route.
* Final slice outcome.

Recommended log shape:

```json
{
  "workflow_run": "healthhub-v1-20260524-001",
  "node": "po_status",
  "slice": "S03",
  "po_run_id": "run_20260524_1030",
  "po_state": "blocked_external",
  "route": "evidence_collection",
  "failure_class": "oura_auth_evidence_missing",
  "evidence_required": "local Oura sleep payload proof"
}
```

## Loop Continuation Strategy

Looping is done declaratively:

```text
slice_select
  → run_slice_subworkflow
  → verify_v1
  → if fail, slice_select
  → if pass, stop
```

Each node has:

* timeout
* retry count
* stale-state rule
* failure route
* artifact output path

This is robust once configured. It is not the fastest first implementation.

## Pros

* Strong structured observability.
* Deterministic routing.
* Good for comparing multiple agents.
* Good for dashboards and run replay.
* Generalizes to many large projects.
* Can encode timeouts and failure routes cleanly.

## Cons

* Highest setup complexity.
* Adds a second orchestrator above PO.
* Can fight Keel’s own state model.
* Overkill for the first Health Data Hub run.
* More configuration to maintain.
* Parallelism can create new failure modes.

## Best Suited For

A later-stage experiment where the goal is to compare autonomous project-management architectures, run multiple agents, or produce formal workflow telemetry. Not the simplest first path.

</approach_3>

<recommendation>

## Recommended approach

Use **Approach 1: Keel-Native Autonomous Supervisor**, with two borrowed ideas:

* Borrow Ralph’s durable progress pattern: `slices.json`, `progress.md`, `events.jsonl`, fresh-context agents, one slice per loop.
* Borrow Conductor-style deterministic routing only as a design principle, not as a new tool.

Do **not** use VS Code or Conductor Mac as the primary operator. They are useful cockpits, but this experiment needs a headless, file-based, restartable, inspectable loop. Do **not** start with Microsoft Conductor CLI either; it adds a second orchestrator before the Keel experiment has even run once.

## Why Approach 1 is best for Health Data Hub

### 1. Keel compatibility

Approach 1 works with Keel rather than around it. It keeps:

```text
gstack → compiler/SWR → playbook → PO → audit → ship
```

The autonomous supervisor simply decides when to invoke each step and how to respond to PO terminal states.

### 2. Gate handling

This is the decisive point. Stock Keel says manual gates are human-only. The experiment says zero human. The honest bridge is an explicit **autonomous-gate profile**, not agent impersonation.

Therefore:

```text
manual_gate = forbidden in autonomous playbooks
autonomous_gate_review = allowed and logged
awaiting_human_gate = failure point, not pause point
```

That makes failure points visible instead of hiding them.

### 3. Complexity

Approach 1 is smaller than a full workflow manager and more robust than a raw Ralph loop. It requires one custom supervisor, a few policy files, and verification scripts. That is the right complexity level.

### 4. Reliability

The Health Data Hub build can run for days or weeks only if the operator can recover from:

* test failures
* stale runs
* failed provider auth
* pyEight breakage
* mood API transport failure
* model gate failure
* forbidden UI language
* backup restore failure

A Keel-native supervisor can classify those using local files and PO status, without relying on one long model context.

### 5. Observability

This project’s goal is partly to discover failure points. Approach 1 gives the cleanest failure ledger:

```text
what happened
where it happened
which command ran
which run ID was active
which evidence existed
what the autonomous decision was
what fallback was applied
```

### 6. Fit with the 16-week plan

The Health Data Hub plan is already ambitious. The operating framework should not become a second major product. Approach 1 is the smallest framework that is still serious enough for zero-human operation.

## Recommended hybrid

Use this hybrid:

```text
Primary operator:
    Keel-Native Autonomous Supervisor

State/memory pattern:
    Ralph-style prd/slices/progress/events files

Agent execution:
    Claude Code for builder/operator tasks
    Codex for independent review/debug where useful

Workflow manager:
    none initially

Optional later:
    Microsoft Conductor CLI only if the first run shows the supervisor needs richer routing/dashboarding
```

## Evaluation table

| Criterion              |                 Approach 1: AutoKeel | Approach 2: Ralph-on-Keel | Approach 3: Workflow Manager |
| ---------------------- | -----------------------------------: | ------------------------: | ---------------------------: |
| Keel compatibility     |                                 High |                    Medium |                  Medium-High |
| Gate handling          | High if autonomy profile is enforced |                    Medium |             High but complex |
| Complexity             |                               Medium |                Low-Medium |                         High |
| Reliability            |                                 High |                    Medium |      Medium-High after setup |
| Observability          |                                 High |                    Medium |                    Very High |
| Failure recovery       |                                 High |                    Medium | High but configuration-heavy |
| Best first experiment? |                                  Yes |                     Maybe |                           No |
| Generalizable?         |                                  Yes |                       Yes |         Yes, but heavyweight |

</recommendation>

<implementation_roadmap>

## 1. Set up the autonomous operator shell first

Create:

```text
/Users/aeziz-local/health-data-hub/
  ops/
    autonomy/
      autokeel.py
      policy.yaml
      slices.json
      autonomy_state.json
      events.jsonl
      failure_ledger.jsonl
      prompts/
        operator.md
        slice_reviewer.md
        failure_diagnoser.md
      failures/
      decisions/
  scripts/
    verify_autonomy_preflight.py
    verify_slice.py
    verify_v1.py
    validate_playbook_autonomous.py
    keel_status_digest.py
```

Start with `slices.json`:

```json
[
  {
    "id": "S01",
    "name": "Warehouse foundation",
    "status": "pending",
    "required": true,
    "acceptance": [
      "python -m pytest tests/warehouse -q",
      "python scripts/check_schema_contract.py",
      "python scripts/check_no_tracked_data.py"
    ]
  },
  {
    "id": "S02",
    "name": "Mood API loop",
    "status": "pending",
    "required": true,
    "acceptance": [
      "python -m pytest tests/test_api_security.py tests/test_mood_date.py tests/test_mood_correction.py -q"
    ]
  },
  {
    "id": "S03",
    "name": "Ingestion provider decision",
    "status": "pending",
    "required": true
  },
  {
    "id": "S04",
    "name": "Feature engineering",
    "status": "pending",
    "required": true
  },
  {
    "id": "S05",
    "name": "Model lifecycle and gates",
    "status": "pending",
    "required": true
  },
  {
    "id": "S06",
    "name": "Counterfactual generator",
    "status": "pending",
    "required": true
  },
  {
    "id": "S07",
    "name": "Read API and Streamlit UI",
    "status": "pending",
    "required": true
  },
  {
    "id": "S08",
    "name": "launchd backups restore",
    "status": "pending",
    "required": true
  },
  {
    "id": "S09",
    "name": "Testing and v1 evaluation",
    "status": "pending",
    "required": true
  }
]
```

## 2. Define the autonomy policy

Create `ops/autonomy/policy.yaml`:

```yaml
mode: autonomous_zero_human

manual_gates:
  allowed: false
  if_detected: record_failure_and_recompile
  forbidden_commands:
    - keel-run mark-manual-gate

external_evidence:
  ai_operator_may_collect: true
  must_store_locally: true
  allowed_dirs:
    - private/evidence
    - docs/evidence
  must_redact_secrets: true

health_data:
  raw_data_in_git: false
  secrets_in_git: false
  full_payloads_in_general_logs: false

model_outputs:
  may_override_baseline_gate: false
  may_override_n_model_gate: false
  may_override_sign_stability_gate: false

tripwires:
  apply_design_doc_tripwires: true
  on_oura_failure_week_1: direct_oura_oauth
  on_pyeight_failure_week_2: oura_only_v1
  on_mood_transport_failure_week_4: streamlit_mobile_form
  on_mood_compliance_failure_week_8: stop_modeling_fix_logging

reviews:
  security_requires_two_ai_reviews: true
  model_requires_two_ai_reviews: true
  ui_language_requires_banned_word_scan: true

loop:
  stop_only_when: scripts/verify_v1.py
  max_retries_per_slice_before_replan: 3
  stale_run_minutes: 60
```

## 3. Write the operator prompt

Create `ops/autonomy/prompts/operator.md`:

```text
You are the autonomous project owner/operator for the Health Data Hub build.

You must operate Keel, not bypass it.

Current paths:
- Keel: /Users/aeziz-local/keel
- Product repo: /Users/aeziz-local/health-data-hub

You must continue until scripts/verify_v1.py passes.

Hard rules:
- Never call keel-run mark-manual-gate.
- Never represent an AI decision as a human approval.
- If a playbook reaches awaiting_human_gate, record manual_gate_leak and recompile/replan under autonomous gate policy.
- All external evidence must be real local evidence under private/evidence or docs/evidence.
- Do not fabricate device/API evidence.
- Do not commit secrets, raw health data, quarantine payloads, snapshots, or tokens.
- Do not weaken Health Data Hub statistical gates.
- Do not use causal language in UI.
- One active slice at a time.

Each loop:
1. Read ops/autonomy/slices.json, autonomy_state.json, events.jsonl, failure_ledger.jsonl.
2. Pick the next pending slice.
3. Ensure slice brief exists.
4. Compile or run SWR as appropriate.
5. Validate autonomous playbook.
6. Run plan-orchestrator under supervision.
7. Inspect PO status.
8. Handle passed/blocked_external/escalated/awaiting_human_gate according to policy.
9. Ship passed slices from ship/<slice>.
10. Update state and logs.
11. Run scripts/verify_v1.py.
12. Continue unless v1 verification passes.
```

## 4. Implement minimal supervisor pseudocode

The first version can be simple:

```python
def main() -> None:
    while True:
        log_heartbeat()

        if run(["python", "scripts/verify_v1.py"]).ok:
            log_event("v1_complete")
            return

        slice_ = choose_next_slice()

        ensure_slice_brief(slice_)
        ensure_playbook(slice_)
        validate_autonomous_playbook(slice_)

        run_id = start_or_resume_po(slice_)
        status = inspect_po(run_id)

        if status == "passed":
            ship_slice(slice_, run_id)
            mark_complete(slice_)
            continue

        if status == "blocked_external":
            gather_external_evidence(slice_, run_id)
            resume_with_evidence(run_id)
            continue

        if status == "awaiting_human_gate":
            record_failure(slice_, "manual_gate_leak", run_id)
            regenerate_autonomous_playbook(slice_)
            continue

        if status == "escalated":
            record_failure(slice_, "po_escalated", run_id)
            diagnose_and_replan(slice_, run_id)
            continue

        if status == "live":
            sleep_and_poll()
            continue

        record_failure(slice_, "unknown_status", run_id)
        diagnose_and_replan(slice_, run_id)
```

Run it under tmux first:

```bash
cd /Users/aeziz-local/health-data-hub
tmux new -s autokeel
python ops/autonomy/autokeel.py
```

Only after the loop is stable should it move to launchd.

## 5. Test the autonomous loop before full deployment

Do not start with Oura, pyEight, or iOS Shortcut. Test the loop on controlled failures.

### Test A: dummy slice

Create a fake slice that writes a small file and runs one test.

Pass condition:

```text
AutoKeel compiles, PO runs, tests pass, slice ships, event log records success.
```

### Test B: manual gate leak

Create a playbook containing a manual gate.

Pass condition:

```text
AutoKeel refuses to call mark-manual-gate.
It records manual_gate_leak.
It regenerates or rejects the playbook.
```

### Test C: blocked external

Create a playbook row that requires external evidence.

Pass condition:

```text
AutoKeel creates private/evidence/<slice>/<timestamp>/.
It writes a real evidence README.
It resumes PO with --external-evidence-dir.
```

### Test D: false done

Make the agent claim done while `verify_slice.py` fails.

Pass condition:

```text
AutoKeel does not mark slice complete.
It records agent_false_done or test_failure.
It retries or replans.
```

## 6. Monitoring and logging

Required files:

```text
ops/autonomy/events.jsonl
ops/autonomy/failure_ledger.jsonl
ops/autonomy/progress.md
ops/autonomy/decisions/*.md
ops/autonomy/heartbeats/latest.json
```

Event categories:

```text
preflight
brief_created
compile_started
compile_failed
playbook_validated
playbook_rejected
po_started
po_status
blocked_external
evidence_created
manual_gate_leak
test_failed
audit_failed
slice_passed
slice_shipped
tripwire_triggered
verify_v1_failed
v1_complete
```

Every major decision gets a Markdown decision record:

```text
ops/autonomy/decisions/
  S01-schema-autonomous-review-20260524.md
  S03-oura-direct-oauth-fallback-20260530.md
  S03-oura-only-v1-decision-20260606.md
  S05-model-gate-suppressed-output-20260710.md
```

## 7. First autonomous slice: S01 warehouse foundation

S01 is the correct first slice because it has:

* no external provider dependency
* no OAuth
* no iOS Shortcut
* concrete files
* concrete tests
* high downstream leverage
* clear schema-contract gate

S01 autonomous brief should require:

```text
Deliverables:
- src/db/schema.sql
- src/warehouse/warehouse.py
- src/warehouse/models.py
- scripts/setup_permissions.py
- tests/warehouse/test_schema.py
- tests/warehouse/test_mood_correction.py
- tests/warehouse/test_quarantine.py
- docs/reviews/s01-autonomous-schema-review.md

Hard constraints:
- exactly five core tables
- no v2 feature columns in daily_features
- hrv_z persisted
- hrv_avg_ms display-only
- mood_entries + mood_current correction flow
- no sleep forward-fill
- quarantine payloads chmod 0600
- data/secrets/quarantine/snapshots gitignored
- no manual_gate rows
```

S01 acceptance should be:

```bash
python -m pytest tests/warehouse -q
python scripts/check_schema_contract.py
python scripts/check_no_tracked_data.py
python scripts/check_autonomous_review_exists.py S01
```

## 8. The first real success criterion

The first success is not “Health Data Hub complete.”

The first success is:

```text
AutoKeel runs S01 from brief → compile → PO → audit → ship
without human intervention,
without fake human approval,
with a complete event log,
and with a truthful failure ledger if anything breaks.
```

That proves the operating framework. Then the same loop can move to the riskier slices: mood API, ingestion, model gates, counterfactuals, UI, and backup/restore.

</implementation_roadmap>

[1]: https://github.com/snarktank/ralph "GitHub - snarktank/ralph: Ralph is an autonomous AI agent loop that runs repeatedly until all PRD items are complete. · GitHub"
[2]: https://addyo.substack.com/p/long-running-agents "Long-running Agents - by Addy Osmani - Elevate"
[3]: https://www.aihero.dev/tips-for-ai-coding-with-ralph-wiggum "11 Tips For AI Coding With Ralph Wiggum"
[4]: https://raw.githubusercontent.com/AysajanE/keel/main/README.md "raw.githubusercontent.com"
[5]: https://raw.githubusercontent.com/AysajanE/gstack-playbook-compiler/main/README.md "raw.githubusercontent.com"
[6]: https://raw.githubusercontent.com/AysajanE/plan-orchestrator/main/docs/playbook-contract.md "raw.githubusercontent.com"
[7]: https://github.com/AysajanE/plan-orchestrator "GitHub - AysajanE/plan-orchestrator: Run approved AI repo changes one item at a time in isolated git worktrees, with verification, dual audits, and explicit human/external stop points. · GitHub"
[8]: https://raw.githubusercontent.com/AysajanE/plan-orchestrator/main/docs/operations-book.md "raw.githubusercontent.com"
[9]: https://raw.githubusercontent.com/AysajanE/plan-orchestrator/main/docs/supervision-guide.md "raw.githubusercontent.com"
[10]: https://raw.githubusercontent.com/AysajanE/plan-orchestrator/main/docs/operator-guide.md "raw.githubusercontent.com"
[11]: https://code.visualstudio.com/docs/copilot/agents/third-party-agents "Third-party agents in Visual Studio Code"
[12]: https://vercel.com/docs/ai-gateway/coding-agents/conductor "Conductor"
[13]: https://www.ycombinator.com/companies/conductor "Conductor: Run a team of coding agents on your Mac | Y Combinator"
[14]: https://github.com/microsoft/conductor "GitHub - microsoft/conductor: A CLI tool for defining and running multi-agent workflows with the GitHub Copilot SDK and Anthropic Agents SDK. · GitHub"
[15]: https://docs.anthropic.com/en/docs/claude-code/memory "How Claude remembers your project - Claude Code Docs"
[16]: https://www.anthropic.com/engineering/effective-harnesses-for-long-running-agents "Effective harnesses for long-running agents \ Anthropic"
[17]: https://developers.openai.com/codex/learn/best-practices "Best practices – Codex | OpenAI Developers"
[18]: https://docs.anthropic.com/en/docs/claude-code/hooks "Hooks reference - Claude Code Docs"
[19]: https://docs.anthropic.com/en/docs/claude-code/sub-agents "Create custom subagents - Claude Code Docs"

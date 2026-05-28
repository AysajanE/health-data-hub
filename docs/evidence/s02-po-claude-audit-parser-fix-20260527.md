# S02 PO Claude Audit Parser Fix

Date: 2026-05-27

## Issue

S02 run `RUN_20260528T012206Z_d1a034d3e30d4b26a26273e07597d115` parked item 02 as `escalated` after implementation and deterministic verification passed.

The escalation manifest reported:

```text
Expecting value: line 1 column 1 (char 0)
```

## Root Cause

The preserved Claude audit report at:

```text
.local/ai/plan_orchestrator/runs/RUN_20260528T012206Z_d1a034d3e30d4b26a26273e07597d115/items/02/attempt-1/claude_audit_report.execute.round-0.json
```

was a valid Claude CLI result envelope. Its `result` field contained explanatory prose followed by an unfenced valid `plan_orchestrator.audit_report.v1` JSON object.

The Keel plan-orchestrator parser accepted bare JSON strings and fenced JSON blocks, but did not extract an unfenced schema JSON object after leading prose. This caused the Claude audit lane to escalate despite:

- execution report success;
- verification report success;
- Codex audit `overall_verdict: pass`;
- Claude result containing `overall_verdict: pass` and `next_recommended_state: pass` inside the embedded audit JSON.

## Fix

Patched the Keel plan-orchestrator toolchain:

```text
/Users/aeziz-local/keel/tools/plan-orchestrator/automation/plan_orchestrator/subprocess_runner.py
```

The parser now:

- continues to accept bare JSON and fenced JSON;
- extracts an embedded JSON object beginning at `schema_version` when Claude returns prose before the object;
- falls back to scanning for report-shaped objects containing `schema_version` or `auditor: claude`;
- still rejects outputs that do not contain an embedded schema JSON object.

Regression test added:

```text
/Users/aeziz-local/keel/tools/plan-orchestrator/automation/plan_orchestrator/tests/test_verification.py
```

## Verification

Commands run:

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest automation/plan_orchestrator/tests/test_verification.py::VerificationTests::test_run_claude_audit_extracts_structured_output automation/plan_orchestrator/tests/test_verification.py::VerificationTests::test_run_claude_audit_extracts_unfenced_result_json_after_prose automation/plan_orchestrator/tests/test_verification.py::VerificationTests::test_run_claude_audit_sanitizes_shell_and_git_override_env -q
```

Result:

```text
3 passed in 0.11s
```

The preserved S02 Claude envelope was also normalized locally and produced:

```text
schema_version: plan_orchestrator.audit_report.v1
audit_lane: claude
overall_verdict: pass
next_recommended_state: pass
```

## Recovery Plan

Resume S02 only through AutoKeel. Do not relaunch SWR. The existing PO run should restart item 02 as a new PO attempt from the corrected Keel toolchain parser.

## AutoKeel Route Drift Follow-Up

After the item 02 escalation, AutoKeel incorrectly marked S02 `replan_required`. That cleared `active_run`; the next S02 iteration archived the validated canonical playbook and started a fresh SWR run:

```text
run_20260528_014537_dc4c7a0d
resp_0f966e52b0a03b8c006a179e46624881948f5eb3b97766731e
```

This was wrong because the failure was PO-audit recoverable after the Keel parser fix. It did not require a playbook replan and did not justify new SWR spend.

The unintended remote response was cancelled:

```text
status: cancelled
```

AutoKeel was patched so future PO `escalated` states record an `audit_failure` but keep the active PO run and mark the slice `pending` for supervised resume after root-cause repair. The canonical S02 playbook was restored from the archived copy and the local state was returned to the existing PO run:

```text
RUN_20260528T012206Z_d1a034d3e30d4b26a26273e07597d115
```

Additional AutoKeel regression coverage:

```bash
python -m pytest tests/autonomy/test_autokeel.py::AutoKeelTests::test_escalated_po_keeps_active_run_for_supervised_resume tests/autonomy/test_autokeel.py::AutoKeelTests::test_active_same_slice_run_invokes_supervise_resume -q
python -m pytest tests/autonomy -q
python scripts/check_no_tracked_data.py
python scripts/validate_playbook_autonomous.py docs/playbooks/s02-mood-api.playbook.md --risk high --json
python -m ops.autonomy.autokeel --doctor
git diff --check
```

Observed results:

```text
2 passed in 0.18s
120 passed in 7.50s
check_no_tracked_data: ok
validate_playbook_autonomous: status ok, row_count 7, errors []
autokeel --doctor: status ok, git_clean warning only from this recovery change set
git diff --check: clean
```

## Repaired PO Resume Guard

The first relaunch after the route fix confirmed that AutoKeel no longer entered SWR, but it also exposed a second recovery gap. AutoKeel invoked supervised PO resume with:

```text
--max-auto-resume-attempts 0
```

Because the existing PO run was already parked in `escalated`, the supervisor correctly refused to reset item 02 and immediately parked the run again. That preserved bounded retry behavior, but it meant AutoKeel had no safe path for an operator-approved repaired retry after the root cause was fixed.

AutoKeel now keeps the default PO retry posture at zero for new runs and normal resumes. For an active PO run that is already `escalated`, it requires the matching `audit_failure` to be closed with local evidence first. Only then does it allow one repaired supervised resume by passing:

```text
--max-auto-resume-attempts 1
```

Regression tests added:

```bash
python -m pytest tests/autonomy/test_autokeel.py::AutoKeelTests::test_escalated_active_run_requires_closed_audit_failure tests/autonomy/test_autokeel.py::AutoKeelTests::test_closed_escalated_audit_failure_allows_one_repaired_resume tests/autonomy/test_autokeel.py::AutoKeelTests::test_active_same_slice_run_invokes_supervise_resume -q
```

Observed result:

```text
3 passed in 0.22s
```

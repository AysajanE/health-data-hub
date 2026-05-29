# S03 Pre-PO Contract Checkpoint Repair Evidence

- Date: 2026-05-29
- Slice: S03
- Failure classes: `compile_failure`, `test_failure`
- Root cause IDs: `S03-COMPILE-FAILURE`, `S03-TEST-FAILURE`

## Failure

The controlled S03 relaunch compiled and validated
`docs/playbooks/s03-ingestion-provider.playbook.md`, then stopped before PO
execution during the real PO contract check.

PO `doctor` rejected the handoff for two reasons:

- The product checkout was dirty with allowed AutoKeel launch artifacts
  (`ops/autonomy/autonomy_state.json`, `ops/autonomy/events.jsonl`, and the
  generated S03 playbook bundle).
- The pre-start contract check did not pass the same reviewed
  `PLAN_ORCHESTRATOR_CLEAN_ENV_CONFIRMED=1` environment confirmation that
  supervised PO execution already uses.

## Root Cause

`AutoKeel.start_or_resume_po()` called
`validate_po_contract_before_start()` before
`checkpoint_allowed_pre_po_changes()`. That ordering violated PO's clean
tracked-checkout invariant: AutoKeel had legitimate local audit and playbook
artifacts ready for launch, but PO `doctor` correctly refused to validate a
dirty checkout.

The contract check also ran without the explicit clean-env confirmation, so PO
treated the ambient agent environment as unreviewed.

## Repair

AutoKeel now checkpoints only allowed pre-PO paths before running the real PO
`list-items` and `doctor` contract checks. It still refuses non-AutoKeel dirty
paths at the same boundary.

AutoKeel also passes
`PLAN_ORCHESTRATOR_CLEAN_ENV_CONFIRMED=1` to both contract commands. The
existing second checkpoint remains in place so the `po_contract_validated`
event is committed before supervised PO execution begins.

## Regression Coverage

Added regression coverage in `tests/autonomy/test_autokeel.py`:

- Contract `list-items` and `doctor` commands must run before PO supervision.
- Contract commands must receive
  `PLAN_ORCHESTRATOR_CLEAN_ENV_CONFIRMED=1`.
- A dirty-but-allowed AutoKeel launch state is checkpointed before PO contract
  validation, and the checkout is still clean when supervised PO execution
  starts.

## Verification

Executed locally:

```bash
python -m pytest tests/autonomy/test_autokeel.py -q
```

Result:

```text
36 passed in 7.91s
```

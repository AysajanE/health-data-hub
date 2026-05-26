# S02 Playbook Validation Failure: Autonomous Gate Term

Date: 2026-05-26
Status: ok

## Issue

The controlled S02 launch compiled `docs/playbooks/s02-mood-api.playbook.md`
and passed Keel PO contract verification, but AutoKeel rejected the playbook
before PO execution:

```text
playbook missing required autonomous gate term: autonomous_gate_review
```

AutoKeel archived the rejected playbook as evidence:

```text
ops/autonomy/failures/archived_playbooks/S02-20260526T132334-0400-s02-mood-api.playbook.md
```

## Root Cause

The S02 autoplan included the required high-risk autonomous gate term, but the
deterministic row-author adapter did not preserve it in the generated playbook
support sections. The adapter also carried S01-specific wording into S02 rows,
including `Required for the S01 warehouse foundation acceptance contract.`

The validator is correct to require the literal `autonomous_gate_review` term
for S02 because `ops/autonomy/policy.yaml` marks the high-risk lane as requiring
that autonomous gate wording. The failure was therefore in the row-author
adapter, not in Keel, the policy, or the review artifacts.

## Fix

`scripts/autokeel_row_author.py` now:

- infers the slice id from row-author context paths such as
  `docs/gstack/s02-mood-api-autoplan.md`;
- emits slice-scoped row language such as `Required for the S02 acceptance
  contract.`;
- emits slice-scoped phase and immediate-next-action text;
- preserves the required `autonomous_gate_review` term in shared autonomous gate
  guidance.

## Verification

Commands run:

```text
python -m py_compile scripts/autokeel_row_author.py
python -m pytest tests/autonomy/test_autokeel_ops_tools.py::AutoKeelOpsToolTests::test_row_author_preserves_high_risk_gate_term_and_slice_label -q
python -m pytest tests/autonomy -q
```

Results:

```text
py_compile: pass
targeted pytest: 1 passed
autonomy pytest: 79 passed
```

A manual row-author smoke check against
`docs/playbooks/s02-mood-api.author_input.json` also confirmed that generated
rows now include `autonomous_gate_review` and S02-scoped acceptance wording.

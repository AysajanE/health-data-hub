# S05 Stage-4 Semantic Rejection: Validator-Forbidden Terms in Packet Text

Date: 2026-06-11
Slice: S05
Closes: the 2026-06-11T08:38+ `audit_failure` row (operator `do_not_approve`
in cycle `gate_and_contract_review_stage_review_c3`).

## What happened

The third stage-4 packet resolved both prior findings (no `.gitignore` row,
no untracked repo_surfaces) but introduced a subtler content defect: its
row-05 `required_verification_commands` embeds a python denylist containing
the literal forbidden phrases ("human approved", "manual approval received",
"manual signoff received", "manual gate active"), and packet prose uses
phrases like "substitute for human signoff" / "must not claim human
approval" in shapes the validator's negation-context patterns do not excuse.
`scripts/validate_playbook_autonomous.py` correctly flags these as active
banned-language occurrences; the operator correctly rejected.

## Resolution

Auto-planned `rerun_single_stage` for `gate_and_contract_review` proceeds
with the c3 blocking finding plus an explicit plan-carried rule: never write
the banned literal phrases at all — assert only the positive
`autonomous_gate_review` marker in verification commands (the repo-wide
validator already enforces banned language), and phrase autonomous-posture
prose without the banned terms.

## Budget classification

Playbook content defect caught by gate review — typed `product_or_playbook`
via sanctioned scope amendment (third of the content-defect family; product
budget 3/5). The review lane behaved correctly throughout.

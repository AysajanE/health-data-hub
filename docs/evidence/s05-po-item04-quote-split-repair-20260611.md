# S05 PO Item 04 Escalation: Quote-Naive Command Splitting

Date: 2026-06-11
Slice: S05
Closes: the 2026-06-11T12:15:59-04:00 `audit_failure` row (PO escalated;
item 04, implementation green).

## Root cause

The playbook's row-04 `.gitignore` assertion is a quoted python -c
one-liner containing semicolons; the adapter's quote-naive splitter
tokenized it into three non-executable fragments, leaving the frozen
verification trail red while the implementation evidence was green. Fix
lanes could not repair a malformed verification gate from inside item write
roots, so the kernel escalated.

## Repairs

1. plan-orchestrator 490e69f (branch fix/split-br-verification-commands):
   command-cell splitting is quote-aware - semicolons inside single/double
   quotes never fragment a command. Suite: 111 passing.
2. refresh-run rebuilt the normalized plan from the unchanged snapshot AT
   THE CURRENT RUN BRANCH HEAD (4d1b20d891e88f6d129a82aabdfbb021da4d672a) - no commit movement, item 01-03
   work untouched; item 04's cell now parses as five clean commands with the
   assertion intact.

## Budget classification

Control-plane kernel parsing defect; root cause id
S05-PO-SEMICOLON-QUOTE-SPLIT. Playbook content unchanged.

# S05 failure_budget_exceeded

- Timestamp: 2026-06-11T08:44:29-04:00
- Severity: high
- Run ID: 
- Evidence: 

- Root Cause ID: S05-FAILURE-BUDGET-EXCEEDED
- Failure Origin: autokeel_wrapper

## Description

root-cause repair budget exceeded for S05: audit_failure:swr supervisor review did not satisfy the fail-closed review-bundle contract.=3

## Action Taken

Stopped the AutoKeel tick instead of converting repeated failures into another generic replan.

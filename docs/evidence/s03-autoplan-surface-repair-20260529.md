# S03 Autoplan Surface Repair Evidence

Timestamp: 2026-05-29T17:25:00-04:00

## Failure Observed

The first controlled S03 AutoKeel tick reached provider evidence collection,
generated `docs/gstack/s03-ingestion-provider-autoplan.md`, compiled
`docs/playbooks/s03-ingestion-provider.playbook.md`, and stopped before PO.

The autonomous playbook validator rejected the compiled playbook because each
row listed `docs/gstack/s03-ingestion-provider-autoplan.md` as a repo surface,
but that path was generated during the same tick and was not tracked at `HEAD`.
The validator therefore reported:

```text
repo_surfaces references path unavailable before row execution: docs/gstack/s03-ingestion-provider-autoplan.md
```

AutoKeel correctly failed closed, archived the rejected playbook, recorded
`unsafe_write_root`, and did not start PO.

## Root Cause

The S03 autoplan was missing before launch, so AutoKeel generated it during the
real tick. The generated playbook then treated the new autoplan as an existing
consult surface. `scripts/validate_playbook_autonomous.py` checks consult
surfaces against `HEAD`, not the dirty worktree, to ensure PO can run from a
clean checkout. Because the autoplan was not yet committed, the row surfaces
were unavailable to a clean PO worktree.

A secondary hardening gap was also observed: the generated autoplan included an
assistant wrapper/code fence around the actual Markdown. The existing autoplan
validator rejected some wrapper phrases, but did not reject this exact wording.

## Repair

- Scrubbed `docs/gstack/s03-ingestion-provider-autoplan.md` to pure Markdown
  content without assistant wrapper text or code fences.
- Hardened `AutoKeel.validate_autoplan_text()` to reject the wrapper phrases
  seen in this run and any fenced-code autoplan output.
- Added regression coverage to `tests/autonomy/test_autokeel.py`.
- The repaired autoplan will be committed before relaunch so the next compiled
  S03 playbook sees the autoplan path as tracked at `HEAD`.

## Verification

Run before closing this failure:

```bash
python -m pytest tests/autonomy/test_autokeel.py -q
python scripts/check_no_tracked_data.py --json
python scripts/validate_playbook_autonomous.py ops/autonomy/failures/archived_playbooks/S03-20260529T171703-0400-s03-ingestion-provider.playbook.md --risk high --json
```

The archived playbook is expected to remain invalid because it was compiled
before the autoplan was committed. The next S03 tick must recompile from the
tracked repaired autoplan instead of reusing the archived playbook.

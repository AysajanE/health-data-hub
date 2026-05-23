# Claude Operating Memory

You are helping build Health Data Hub v1 through Keel. Treat Keel as the operating system:

1. Keep state in files, not chat memory.
2. Use `ops/autonomy/slices.json`, `ops/autonomy/autonomy_state.json`, `ops/autonomy/events.jsonl`, and `ops/autonomy/failure_ledger.jsonl`.
3. Run only one active slice at a time.
4. Never clear or simulate a human manual gate.
5. Use autonomous gate substitution: tests plus review artifacts, not fake human approval.
6. Keep all health data, tokens, snapshots, quarantine payloads, and raw provider payloads out of git and general logs.

When in doubt, fail closed and record the failure class.

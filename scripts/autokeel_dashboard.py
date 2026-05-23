#!/usr/bin/env python3
"""Render a small static AutoKeel status dashboard."""

from __future__ import annotations

import argparse
from collections import Counter
import html
import json
from pathlib import Path
from typing import Any


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    return json.loads(text) if text else default


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def render(root: Path) -> str:
    state = read_json(root / "ops/autonomy/autonomy_state.json", {})
    slices = read_json(root / "ops/autonomy/slices.json", [])
    failures = read_jsonl(root / "ops/autonomy/failure_ledger.jsonl")
    events = read_jsonl(root / "ops/autonomy/events.jsonl")[-25:]
    counts = Counter(str(item.get("status", "unknown")) for item in slices)
    count_rows = "\n".join(f"<li>{html.escape(status)}: {count}</li>" for status, count in sorted(counts.items()))
    rows = "\n".join(
        f"<tr><td>{html.escape(item.get('id', ''))}</td><td>{html.escape(item.get('name', ''))}</td><td>{html.escape(item.get('status', ''))}</td></tr>"
        for item in slices
    )
    open_failures = "\n".join(
        f"<li>{html.escape(item.get('slice', ''))}: {html.escape(item.get('failure_class', ''))} ({html.escape(item.get('severity', ''))})</li>"
        for item in failures
        if item.get("open", True)
    )
    event_rows = "\n".join(
        f"<li>{html.escape(str(item.get('event_id', '')))} {html.escape(item.get('ts', ''))} {html.escape(item.get('event', ''))}</li>"
        for item in events
    )
    return f"""<!doctype html>
<html lang="en">
<meta charset="utf-8">
<title>AutoKeel Dashboard</title>
<style>
body {{ font-family: system-ui, sans-serif; margin: 24px; color: #1f2933; }}
table {{ border-collapse: collapse; width: 100%; margin: 16px 0; }}
td, th {{ border: 1px solid #d8dee4; padding: 8px; text-align: left; }}
code, pre {{ background: #f6f8fa; padding: 2px 4px; }}
</style>
<h1>AutoKeel Dashboard</h1>
<h2>State</h2>
<pre>{html.escape(json.dumps(state, indent=2, sort_keys=True))}</pre>
<h2>Status Counts</h2>
<ul>{count_rows or '<li>none</li>'}</ul>
<h2>Slices</h2>
<table><tr><th>ID</th><th>Name</th><th>Status</th></tr>{rows}</table>
<h2>Open Failures</h2>
<ul>{open_failures or '<li>none</li>'}</ul>
<h2>Recent Events</h2>
<ul>{event_rows or '<li>none</li>'}</ul>
</html>
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render AutoKeel status dashboard HTML.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--out")
    args = parser.parse_args(argv)
    html_text = render(Path(args.root).resolve())
    if args.out:
        Path(args.out).write_text(html_text, encoding="utf-8")
    else:
        print(html_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

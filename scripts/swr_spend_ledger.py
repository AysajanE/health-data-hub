#!/usr/bin/env python3
"""Per-slice ledger of usage-billed SWR stage generations.

SWR stage generations (OpenAI API) are this project's only usage-billed
resource; review lanes and PO agents run on subscription CLIs. This ledger
counts every stage generation ever billed for a slice by scanning the run
directories' stage response artifacts (each generation writes a
response.final.md header carrying response_id, model, and token counts;
regenerations overwrite the file but each distinct response_id observed in
manifests, checkpoints, and events is a distinct billed call).

The ledger is evidence, not bookkeeping: it is recomputed from disk on every
call so it cannot drift from reality or be edited around.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any

RESPONSE_ID_PATTERN = re.compile(r"\bresp_[0-9a-f]{40,}\b")
HEADER_FIELD = re.compile(r"^- (response_id|model|input_tokens|output_tokens|total_tokens): (.+)$", re.M)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return default


def slice_run_dirs(root: Path, slice_id: str) -> list[Path]:
    runs_root = root / ".local/autokeel/swr/runs"
    if not runs_root.exists():
        return []
    needle = f"autokeel-{slice_id.lower()}-"
    return sorted(d for d in runs_root.iterdir() if d.is_dir() and needle in d.name)


def collect_slice_spend(root: Path, slice_id: str) -> dict[str, Any]:
    """Count distinct billed generations and tokens for one slice."""
    generations: dict[str, dict[str, Any]] = {}

    def record(response_id: str, **fields: Any) -> None:
        entry = generations.setdefault(response_id, {"response_id": response_id})
        for key, value in fields.items():
            if value is not None and entry.get(key) in (None, "", 0):
                entry[key] = value

    for run_dir in slice_run_dirs(root, slice_id):
        # Current on-disk artifacts (latest generation per stage).
        for header in run_dir.glob("stages/*/response.final.md"):
            text_head = header.read_text(encoding="utf-8", errors="replace")[:2000]
            fields = dict(HEADER_FIELD.findall(text_head))
            response_id = fields.get("response_id")
            if response_id:
                record(
                    response_id,
                    stage=header.parent.name,
                    model=fields.get("model"),
                    input_tokens=int(fields["input_tokens"]) if str(fields.get("input_tokens", "")).isdigit() else None,
                    output_tokens=int(fields["output_tokens"]) if str(fields.get("output_tokens", "")).isdigit() else None,
                    run_dir=run_dir.name,
                )
        # Manifest + checkpoints catch ids whose artifacts were overwritten.
        for extra in [run_dir / "run_manifest.json", *run_dir.glob("stages/*/stage_checkpoint.json"), *run_dir.glob("stages/*/response.latest.json")]:
            payload = load_json(extra, None)
            if payload is None:
                continue
            for match in RESPONSE_ID_PATTERN.findall(json.dumps(payload)):
                record(match, run_dir=run_dir.name)

    # Events catch regenerations whose run artifacts were fully replaced.
    events_path = root / "ops/autonomy/events.jsonl"
    if events_path.exists():
        slice_token = f'"slice": "{slice_id}"'
        for line in events_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if slice_token not in line:
                continue
            for match in RESPONSE_ID_PATTERN.findall(line):
                record(match)

    total_in = sum(int(g.get("input_tokens") or 0) for g in generations.values())
    total_out = sum(int(g.get("output_tokens") or 0) for g in generations.values())
    measured = sum(1 for g in generations.values() if g.get("input_tokens"))
    return {
        "slice": slice_id,
        "billed_generations": len(generations),
        "generations_with_measured_tokens": measured,
        "input_tokens_measured": total_in,
        "output_tokens_measured": total_out,
        "generations": sorted(generations.values(), key=lambda g: str(g.get("stage") or "")),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Report usage-billed SWR generations per slice.")
    parser.add_argument("slice_id")
    parser.add_argument("--root", default=".")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = collect_slice_spend(Path(args.root).resolve(), args.slice_id.upper())
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print(
            f"{report['slice']}: {report['billed_generations']} billed generations, "
            f"{report['input_tokens_measured']} in / {report['output_tokens_measured']} out tokens measured"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

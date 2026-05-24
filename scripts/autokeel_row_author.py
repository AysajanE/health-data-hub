#!/usr/bin/env python3
"""Deterministic row-author adapter for AutoKeel compiler runs.

The Keel compiler still parses and validates the gstack artifacts. This adapter
only converts compiler task cards into strict po_candidate_rows_v1 JSON so the
controlled autonomous launch does not depend on schema-perfect model output.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any


ACTION_VERBS = {
    "add",
    "build",
    "convert",
    "create",
    "configure",
    "delete",
    "document",
    "draft",
    "expose",
    "extend",
    "extract",
    "generate",
    "implement",
    "introduce",
    "migrate",
    "move",
    "normalize",
    "publish",
    "refactor",
    "register",
    "remove",
    "replace",
    "run",
    "seed",
    "test",
    "turn",
    "update",
    "validate",
    "verify",
    "wire",
    "write",
}


def extract_json_after_marker(prompt: str, marker: str) -> dict[str, Any]:
    marker_index = prompt.find(marker)
    if marker_index < 0:
        raise ValueError(f"missing marker: {marker}")
    start = prompt.find("{", marker_index)
    if start < 0:
        raise ValueError(f"missing JSON object after marker: {marker}")
    decoder = json.JSONDecoder()
    obj, _ = decoder.raw_decode(prompt[start:])
    if not isinstance(obj, dict):
        raise ValueError(f"JSON after marker is not an object: {marker}")
    return obj


def normalize_action(text: str) -> str:
    action = " ".join(str(text).split())
    if not action:
        return "Implement the declared AutoKeel task"
    first = action.split(maxsplit=1)[0].lower().strip(":")
    if first == "author":
        action = "Create" + action[len(action.split(maxsplit=1)[0]):]
    elif first not in ACTION_VERBS:
        action = f"Implement {action[0].lower()}{action[1:]}"
    return action[:240]


def phase_slug(phase: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", phase.lower()).strip("-")
    return slug or "implementation"


def row_for_card(card: dict[str, Any], index: int) -> dict[str, Any]:
    step_id = f"{index:02d}"
    task_id = str(card.get("task_id") or f"task_{index:03d}")
    phase = str(card.get("phase") or "implementation")
    files = [str(path) for path in card.get("declared_deliverables", []) if str(path)]
    surfaces = [str(path) for path in card.get("existing_repo_surfaces", []) if str(path)]
    roots = [str(path) for path in card.get("clamped_allowed_write_roots", []) if str(path)]
    verification = [str(cmd) for cmd in card.get("verification_candidates", []) if str(cmd)]
    behavioral = bool(card.get("behavioral"))
    verification_gated = behavioral or bool(verification)

    if not surfaces:
        surfaces = files
    if not roots:
        roots = sorted({path.rsplit("/", 1)[0] for path in files if "/" in path})[:3]
    if not roots and files:
        roots = [files[0]]

    exit_bits = []
    if files:
        exit_bits.append(f"{', '.join(files)} exist with the required S01 behavior")
    if verification:
        exit_bits.append(f"{', '.join(verification)} passes")
    exit_criteria = "; ".join(exit_bits) or "Declared artifact exists and is referenced by verification evidence"

    return {
        "step_id": step_id,
        "phase": phase,
        "action": normalize_action(card.get("task") or ""),
        "why_now": "Required for the S01 warehouse foundation acceptance contract.",
        "owner_type": "operator",
        "prerequisites": "none" if index == 1 else f"{index - 1:02d}",
        "repo_surfaces": surfaces,
        "deliverable": files,
        "exit_criteria": exit_criteria,
        "allowed_write_roots": roots[:3],
        "requires_red_green": verification_gated,
        "manual_gate": "none",
        "manual_gate_reason": "",
        "manual_gate_evidence": [],
        "external_check": "none",
        "external_dependencies": [],
        "consult_paths": surfaces[:3],
        "required_verification_commands": verification,
        "required_verification_artifacts": [] if verification_gated else files,
        "notes": [f"source_task: {task_id}", "autokeel deterministic row author"],
    }


def main() -> int:
    prompt = sys.stdin.read()
    context = extract_json_after_marker(prompt, "# Input: row_author_context_v1")
    cards = context.get("task_cards", [])
    if not isinstance(cards, list) or not cards:
        raise SystemExit("no task_cards in row_author_context_v1")

    rows = [row_for_card(card, index) for index, card in enumerate(cards, start=1)]
    phases = []
    seen = set()
    for row in rows:
        if row["phase"] in seen:
            continue
        seen.add(row["phase"])
        phases.append(
            {
                "phase_slug": phase_slug(row["phase"]),
                "title": row["phase"],
                "body": f"Executes S01 task rows for {row['phase']}.",
            }
        )

    payload = {
        "schema_version": "po_candidate_rows_v1",
        "rows": rows,
        "support_sections": {
            "plan_context": "S01 builds the local-first warehouse foundation for Health Data Hub v1.",
            "phase_details": phases,
            "shared_guidance": [
                {
                    "title": "Autonomous Gate Policy",
                    "body": "Manual gates are forbidden; deterministic tests and autonomous review artifacts are required instead.",
                },
                {
                    "title": "Health Data Safety",
                    "body": "Do not commit raw health data, provider payloads, secrets, DuckDB files, snapshots, or quarantine payloads.",
                },
            ],
            "risks_and_contingencies": "If verification fails, stop and record an AutoKeel failure instead of fabricating evidence.",
            "immediate_next_actions": "Run S01 acceptance commands only after all rows complete.",
        },
        "compiler_warnings": [],
    }
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

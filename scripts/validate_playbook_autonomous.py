#!/usr/bin/env python3
"""Autonomous-mode validator for Keel markdown_playbook_v1 files."""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Any


EMPTY_VALUES = {"", "none", "n/a", "na", "no", "-", "null"}
BROAD_ROOTS = {".", "/", "src", "src/", "tests", "tests/", "test", "test/", "docs", "docs/"}
FORBIDDEN_ROOT_PREFIXES = (".git", ".local", ".env", ".codex", ".claude", "data", "data/", "private", "private/")
FORBIDDEN_COMMAND_PATTERNS = ("mark-manual-gate", "keel-run mark-manual-gate")
UI_BANNED_PATTERNS = (
    r"\bbiggest drivers\b",
    r"\bdrivers\b",
    r"\bwhat made you tired\b",
    r"\bcaused\b",
    r"\byou should\b",
    r"\byou would have felt\b",
    r"\btomorrow'?s feeling prediction\b",
)
V2_SCOPE_PATTERNS = (
    r"\bprospective\b",
    r"\btomorrow prediction\b",
    r"\brecommendations?/today\b",
    r"\bgarmin\b",
    r"\bwithings\b",
    r"\bchest[- ]strap\b",
    r"\bdiet\b",
    r"\bnutrition\b",
    r"\btraining_load\b",
    r"\bzone4\b",
    r"\bdinner timing\b",
)
CODE_PATH_PREFIXES = ("src/", "app/", "scripts/", "tests/")
DEFAULT_REQUIRED_COLUMNS = {
    "action",
    "deliverable",
    "allowed_write_roots",
    "requires_red_green",
    "required_verification_commands",
    "exit_criteria",
}


def load_validation_policy(playbook_path: Path, explicit_policy: Path | None = None) -> dict[str, Any]:
    policy_path = explicit_policy
    if policy_path is None:
        for parent in [playbook_path.parent, *playbook_path.parents]:
            candidate = parent / "ops" / "autonomy" / "policy.yaml"
            if candidate.exists():
                policy_path = candidate
                break
    if policy_path is None or not policy_path.exists():
        return {}
    try:
        from ops.autonomy.autokeel import load_policy

        policy = load_policy(policy_path)
    except Exception:
        return {}
    profile = policy.get("playbook_validation", {})
    return profile if isinstance(profile, dict) else {}


def split_markdown_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip().strip("`") for cell in stripped.split("|")]


def is_separator(line: str) -> bool:
    cells = split_markdown_row(line)
    return bool(cells) and all(re.fullmatch(r":?-{3,}:?", cell.strip()) for cell in cells)


def parse_tables(text: str) -> list[list[dict[str, str]]]:
    lines = text.splitlines()
    tables: list[list[dict[str, str]]] = []
    index = 0
    while index < len(lines) - 1:
        if "|" not in lines[index] or not is_separator(lines[index + 1]):
            index += 1
            continue
        headers = [header.lower().replace(" ", "_") for header in split_markdown_row(lines[index])]
        rows: list[dict[str, str]] = []
        index += 2
        while index < len(lines) and "|" in lines[index]:
            cells = split_markdown_row(lines[index])
            if len(cells) < 2:
                break
            row = {headers[i]: cells[i] if i < len(cells) else "" for i in range(len(headers))}
            rows.append(row)
            index += 1
        if rows:
            tables.append(rows)
    return tables


def split_list_cell(value: str) -> list[str]:
    return [part.strip().strip("`") for part in re.split(r"[,;\n]+", value or "") if part.strip()]


def is_empty(value: str) -> bool:
    return value.strip().lower() in EMPTY_VALUES


def row_text(row: dict[str, str]) -> str:
    return " ".join(str(value) for value in row.values())


def is_ui_row(row: dict[str, str]) -> bool:
    text = row_text(row).lower()
    return any(token in text for token in ("streamlit", "ui", "insight", "counterfactual", "copy", "language"))


def has_code_deliverable(row: dict[str, str]) -> bool:
    combined = f"{row.get('deliverable', '')} {row.get('deliverables', '')} {row.get('allowed_write_roots', '')}"
    return any(prefix in combined for prefix in CODE_PATH_PREFIXES)


def contains_forbidden_executable_command(value: str, forbidden_commands: tuple[str, ...]) -> str | None:
    lowered_value = value.lower()
    for pattern in forbidden_commands:
        pattern_lower = str(pattern).lower()
        if not pattern_lower:
            continue
        if pattern_lower not in lowered_value:
            continue

        allowed_safety_phrases = (
            f"never call {pattern_lower}",
            f"do not call {pattern_lower}",
            f"must not call {pattern_lower}",
            f"forbidden command: {pattern_lower}",
            f"{pattern_lower} is forbidden",
        )
        if any(phrase in lowered_value for phrase in allowed_safety_phrases):
            continue

        return pattern
    return None


def term_pattern(term: str) -> str:
    return r"\b" + re.escape(term).replace(r"\ ", r"\s+") + r"\b"


def allowed_negative_policy_context(text: str, term: str) -> bool:
    pattern = term_pattern(term)
    allowed_patterns = (
        rf"\b(?:no|not|never|without)\b[^.\n|]{{0,100}}{pattern}",
        rf"\b(?:do not|does not|did not|must not|must never|should not|cannot)\b[^.\n|]{{0,100}}{pattern}",
        rf"\b(?:in lieu of|instead of)\b[^.\n|]{{0,80}}{pattern}",
        rf"{pattern}[^.\n|]{{0,100}}\b(?:not emitted|not claimed|not performed|not part|not required|forbidden|prohibited)\b",
        rf"{pattern}[^.\n|]{{0,100}}\b(?:is|are)\s+(?:forbidden|prohibited|not emitted|not claimed|not performed)\b",
    )
    return any(re.search(allowed, text, re.I) for allowed in allowed_patterns)


def forbidden_banned_language_present(text: str, term: str) -> bool:
    pattern = re.compile(term_pattern(term), re.I)
    for match in pattern.finditer(text):
        start = max(0, match.start() - 120)
        end = min(len(text), match.end() + 120)
        if allowed_negative_policy_context(text[start:end], term):
            continue
        return True
    return False


def allowed_v2_scope_context(text: str, match: re.Match[str]) -> bool:
    start = max(0, match.start() - 120)
    end = min(len(text), match.end() + 120)
    window = text[start:end]
    matched = re.escape(match.group(0))
    allowed_patterns = (
        rf"\b(?:no|not|never|without)\b[^.\n|]{{0,100}}{matched}",
        rf"\b(?:do not|does not|did not|must not|must never|should not|cannot)\b[^.\n|]{{0,100}}{matched}",
        rf"{matched}[^.\n|]{{0,100}}\b(?:not in scope|out of scope|outside scope|deferred|forbidden|prohibited|not implemented|not returned)\b",
        rf"\b(?:not in scope|out of scope|outside scope|deferred|forbidden|prohibited)\b[^.\n|]{{0,100}}{matched}",
    )
    return any(re.search(allowed, window, re.I) for allowed in allowed_patterns)


def validate_playbook(path: Path, policy_path: Path | None = None, risk: str | None = None) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    if not path.exists():
        return {"status": "error", "errors": [f"playbook missing: {path}"], "warnings": [], "row_count": 0}

    profile = load_validation_policy(path, policy_path)
    forbidden_commands = tuple(dict.fromkeys([*FORBIDDEN_COMMAND_PATTERNS, *profile.get("forbidden_commands", [])]))
    broad_roots = set(BROAD_ROOTS) | set(profile.get("forbidden_roots", []))
    # Do not treat playbook contract column names as globally banned language.
    # Active manual gates are checked row-by-row below.
    banned_language = [
        str(term).lower()
        for term in profile.get("banned_language", [])
        if str(term).lower() not in {"manual_gate", "manual gate"}
    ]
    ui_banned_patterns = tuple(UI_BANNED_PATTERNS)
    required_gate_terms = [str(term).lower() for term in profile.get("required_gate_terms", [])]
    required_by_risk = profile.get("required_gate_terms_by_risk", {})
    if risk and isinstance(required_by_risk, dict):
        required_gate_terms.extend(str(term).lower() for term in required_by_risk.get(risk, []))
    required_columns = set(profile.get("required_columns", [])) or DEFAULT_REQUIRED_COLUMNS

    text = path.read_text(encoding="utf-8")
    lowered = text.lower()
    for term in banned_language:
        if not term:
            continue
        if forbidden_banned_language_present(lowered, term):
            errors.append(f"forbidden autonomous playbook language: {term}")
    for term in required_gate_terms:
        if term and term not in lowered:
            errors.append(f"playbook missing required autonomous gate term: {term}")

    tables = parse_tables(text)
    candidate_rows = [
        row
        for table in tables
        for row in table
        if "allowed_write_roots" in row or "manual_gate" in row or "requires_red_green" in row
    ]
    if not candidate_rows:
        errors.append("no markdown_playbook_v1 execution rows found")

    for idx, row in enumerate(candidate_rows, start=1):
        for column in sorted(required_columns):
            if column not in row or is_empty(row.get(column, "")):
                errors.append(f"row {idx}: required column is missing or empty: {column}")

        manual_gate_value = row.get("manual_gate", "")
        if manual_gate_value and not is_empty(manual_gate_value):
            errors.append(f"row {idx}: active manual_gate is forbidden in autonomous mode")

        roots = split_list_cell(row.get("allowed_write_roots", ""))
        if not roots:
            errors.append(f"row {idx}: allowed_write_roots is required")
        for root in roots:
            normalized = root.rstrip("/")
            if root in broad_roots or normalized in broad_roots:
                errors.append(f"row {idx}: broad allowed_write_root forbidden: {root}")
            if root.startswith(FORBIDDEN_ROOT_PREFIXES) or normalized in {"data", "private"}:
                errors.append(f"row {idx}: sensitive allowed_write_root forbidden: {root}")
            if root.startswith("/") or ".." in Path(root).parts:
                errors.append(f"row {idx}: allowed_write_root must be repo-relative and contained: {root}")

        verification = row.get("required_verification_commands", "") or row.get("verification", "")
        requires_red_green = (row.get("requires_red_green") or "").lower() == "true"
        if requires_red_green and is_empty(verification):
            errors.append(f"row {idx}: requires_red_green=true but required_verification_commands is empty")
        if has_code_deliverable(row) and is_empty(verification):
            errors.append(f"row {idx}: code/script/test deliverable lacks verification command")

        executable_fields = [
            row.get("required_verification_commands", ""),
            row.get("verification", ""),
            row.get("command", ""),
            row.get("commands", ""),
        ]
        for field_value in executable_fields:
            forbidden = contains_forbidden_executable_command(field_value, forbidden_commands)
            if forbidden:
                errors.append(f"row {idx}: forbidden executable command appears in row: {forbidden}")

        external = (row.get("external_check") or "").strip().lower()
        if external and external not in EMPTY_VALUES and "private/evidence" not in row_text(row) and "docs/evidence" not in row_text(row):
            if "blocked_external" in row_text(row).lower():
                warnings.append(f"row {idx}: external_check expects blocked_external without local evidence path")
            else:
                errors.append(f"row {idx}: external_check must name private/evidence or docs/evidence local path")

        if is_ui_row(row):
            for pattern in ui_banned_patterns:
                if re.search(pattern, row_text(row), re.I):
                    errors.append(f"row {idx}: forbidden v1 UI language matched /{pattern}/")

        lower_row = row_text(row).lower()
        for pattern in V2_SCOPE_PATTERNS:
            match = re.search(pattern, lower_row, re.I)
            if match and not allowed_v2_scope_context(lower_row, match):
                errors.append(f"row {idx}: v2 scope creep matched /{pattern}/")

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "row_count": len(candidate_rows)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Validate an autonomous Keel playbook.")
    parser.add_argument("playbook")
    parser.add_argument("--policy")
    parser.add_argument("--risk")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = validate_playbook(Path(args.playbook), policy_path=Path(args.policy) if args.policy else None, risk=args.risk)
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        for warning in report["warnings"]:
            print(f"WARNING: {warning}", file=sys.stderr)
        print(f"{report['status']}: {report['row_count']} execution rows")
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

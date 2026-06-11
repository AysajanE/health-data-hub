#!/usr/bin/env python3
"""Autonomous-mode validator for Keel markdown_playbook_v1 files."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

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
UNVERIFIED_DEPENDENCY_PATTERNS = (
    r"\balready available in-memory limiter dependency\b",
    r"\bplanned in-memory limiter dependency\b",
    r"\blimiter dependency is unavailable\b",
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
POLICY_NOTE_FIELDS = {
    "policy_note",
    "policy_notes",
    "safety_note",
    "safety_notes",
    "boundary_note",
    "guardrail",
    "notes",
}
EXECUTABLE_FIELD_NAMES = {"required_verification_commands", "verification", "command", "commands"}
STRICT_FORBIDDEN_ROW_FIELDS = {"action", "deliverable", "deliverables", "exit_criteria"}
REPO_PATH_RE = re.compile(r"\b(?:app|docs|ops|scripts|src|tests)/[A-Za-z0-9._/\-]+\b")


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


def plan_orchestrator_roots(playbook_path: Path) -> list[Path]:
    roots: list[Path] = []
    configured = os.environ.get("KEEL_PO_ROOT")
    if configured:
        roots.append(Path(configured))

    policy_path = None
    for parent in [playbook_path.parent, *playbook_path.parents]:
        candidate = parent / "ops" / "autonomy" / "policy.yaml"
        if candidate.exists():
            policy_path = candidate
            break
    if policy_path is not None:
        try:
            from ops.autonomy.autokeel import load_policy

            policy = load_policy(policy_path)
            if policy.get("plan_orchestrator_root"):
                roots.append(Path(str(policy["plan_orchestrator_root"])))
            elif policy.get("keel_root"):
                roots.append(Path(str(policy["keel_root"])) / "tools" / "plan-orchestrator")
        except Exception:
            pass

    roots.append(REPO_ROOT / "automation")
    roots.append(Path("/Users/aeziz-local/keel/tools/plan-orchestrator"))

    deduped: list[Path] = []
    seen: set[str] = set()
    for root in roots:
        resolved = root.expanduser()
        key = str(resolved)
        if key not in seen:
            deduped.append(resolved)
            seen.add(key)
    return deduped


def ensure_plan_orchestrator_import_path(playbook_path: Path) -> None:
    try:
        import automation.plan_orchestrator  # noqa: F401

        return
    except ModuleNotFoundError:
        pass

    for root in plan_orchestrator_roots(playbook_path):
        if (root / "automation" / "plan_orchestrator").exists():
            candidate = root
        elif root.name == "automation" and (root / "plan_orchestrator").exists():
            candidate = root.parent
        else:
            continue
        candidate_text = str(candidate)
        if candidate_text not in sys.path:
            sys.path.insert(0, candidate_text)
        try:
            import automation.plan_orchestrator  # noqa: F401

            return
        except ModuleNotFoundError:
            continue


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


def split_allowed_write_roots(value: str) -> list[str]:
    return [part.strip().strip("`") for part in re.split(r"[;\n]+", value or "") if part.strip()]


def is_empty(value: str) -> bool:
    return value.strip().lower() in EMPTY_VALUES


def row_text(row: dict[str, str]) -> str:
    return " ".join(str(value) for value in row.values())


def non_table_text(text: str) -> str:
    return "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("|"))


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


def field_contains_term(value: str, term: str) -> bool:
    return re.search(term_pattern(term), value, re.I) is not None


def field_has_v2_scope(value: str, pattern: str) -> re.Match[str] | None:
    return re.search(pattern, value, re.I)


def is_executable_command_field(field_name: str, value: str) -> bool:
    if field_name not in EXECUTABLE_FIELD_NAMES:
        return False
    return not is_empty(value)


def extract_repo_paths(value: str) -> set[str]:
    paths: set[str] = set()
    for match in REPO_PATH_RE.finditer(value or ""):
        paths.add(match.group(0).rstrip(".,);:"))
    return paths


def row_step_id(row: dict[str, str], index: int) -> str:
    for key in ("step_id", "id", "item", "item_id"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return str(index)


def row_prerequisite_text(row: dict[str, str]) -> str:
    fields = ("prerequisites", "prerequisite", "depends_on", "dependencies", "requires")
    return " ".join(str(row.get(field) or "") for field in fields)


def temporal_dependency_errors(playbook_path: Path, rows: list[dict[str, str]]) -> list[str]:
    errors: list[str] = []
    deliverables_by_row = [
        extract_repo_paths(f"{row.get('deliverable', '')} {row.get('deliverables', '')}")
        for row in rows
    ]
    final_index = len(rows)
    available_paths: set[str] = set()
    for index, row in enumerate(rows, start=1):
        verification = row.get("required_verification_commands", "") or row.get("verification", "")
        current_deliverables = deliverables_by_row[index - 1]
        if not verification:
            available_paths.update(current_deliverables)
            continue
        if (
            playbook_path.name.startswith("s03-")
            and "python scripts/verify_s03_readiness.py --json" in verification
            and index != final_index
        ):
            errors.append(f"row {index}: final S03 readiness gate is only allowed on the final S03 item")
        prereq_text = row_prerequisite_text(row)
        prereq_paths = extract_repo_paths(prereq_text)
        for future_index in range(index + 1, final_index + 1):
            future_row = rows[future_index - 1]
            future_step = row_step_id(future_row, future_index)
            future_deliverables = deliverables_by_row[future_index - 1]
            if not future_deliverables:
                continue
            prerequisite_declared = future_step in prereq_text or bool(future_deliverables & prereq_paths)
            if prerequisite_declared:
                continue
            for future_path in sorted(future_deliverables):
                if future_path in available_paths or future_path in current_deliverables:
                    continue
                if future_path in verification:
                    errors.append(
                        f"row {index}: verification command depends on future row {future_index} artifact without prerequisite: {future_path}"
                    )
        available_paths.update(current_deliverables)
    return errors


NEGATION_LEAD_IN_PATTERN = re.compile(
    r"\b(?:does not own|does not include|not in scope|out of scope|outside scope|"
    r"excluded|exclusions|must not|do not|never|prohibited|forbidden|deferred)\b",
    re.I,
)


def bullet_under_negation_lead_in(text: str, match_start: int) -> bool:
    """A bullet item inherits the exclusion context of its list's lead-in line.

    Exclusion lists ("S05 explicitly does not own:" followed by bullets) are
    the standard way scope documents name what they exclude; per-line sentence
    logic cannot see the lead-in, so walk back through consecutive bullet or
    blank lines to the nearest non-bullet line and test it for negation.
    """
    line_start = text.rfind("\n", 0, match_start) + 1
    line = text[line_start : text.find("\n", line_start) if text.find("\n", line_start) != -1 else len(text)]
    if not line.lstrip().startswith(("-", "*")):
        return False
    cursor = line_start
    for _ in range(40):
        if cursor <= 0:
            return False
        prev_end = cursor - 1
        prev_start = text.rfind("\n", 0, prev_end) + 1
        prev_line = text[prev_start:prev_end]
        stripped = prev_line.strip()
        if not stripped or stripped.startswith(("-", "*")):
            cursor = prev_start
            continue
        return bool(NEGATION_LEAD_IN_PATTERN.search(stripped))
    return False


def allowed_v2_scope_context(text: str, match: re.Match[str]) -> bool:
    start = max(0, match.start() - 120)
    end = min(len(text), match.end() + 120)
    window = text[start:end]
    matched = re.escape(match.group(0))
    sentence_start = max(text.rfind(".", 0, match.start()) + 1, text.rfind("\n", 0, match.start()) + 1)
    prefix = text[sentence_start : match.start()]
    if re.search(r"\b(?:no|not|never|without|do not|must not|must never|should not|cannot)\b[^.\n|]{0,500}$", prefix, re.I):
        return True
    if bullet_under_negation_lead_in(text, match.start()):
        return True
    allowed_patterns = (
        rf"\b(?:no|not|never|without)\b[^.\n|]{{0,100}}{matched}",
        rf"\b(?:do not|does not|did not|must not|must never|should not|cannot)\b[^.\n|]{{0,100}}{matched}",
        rf"{matched}[^.\n|]{{0,100}}\b(?:not in scope|out of scope|outside scope|deferred|forbidden|prohibited|not implemented|not returned)\b",
        rf"\b(?:not in scope|out of scope|outside scope|deferred|forbidden|prohibited)\b[^.\n|]{{0,100}}{matched}",
    )
    return any(re.search(allowed, window, re.I) for allowed in allowed_patterns)


def should_validate_po_normalization(text: str) -> bool:
    lowered = text.lower()
    if "format: markdown_playbook_v1" in lowered:
        return True
    return bool(
        re.search(
            r"\|\s*step_id\s*\|[^\n]*\bwhy_now\b[^\n]*\brequires_red_green\b",
            text,
            re.I,
        )
    )


def normalize_plan_orchestrator(path: Path, text: str) -> tuple[list[str], Any | None]:
    if not should_validate_po_normalization(text):
        return [], None
    try:
        ensure_plan_orchestrator_import_path(path)
        from automation.plan_orchestrator.adapters.markdown_playbook import MarkdownPlaybookAdapter
        from automation.plan_orchestrator.playbook_parser import parse_playbook

        parsed = parse_playbook(path)
        plan = MarkdownPlaybookAdapter(path.parent).normalize(parsed, path)
    except Exception as exc:
        return [f"plan-orchestrator normalization failed: {exc}"], None
    return [], plan


def repo_path_tracked_at_head(rel_path: str) -> bool:
    if Path(rel_path).is_absolute() or ".." in Path(rel_path).parts:
        return False
    if not (REPO_ROOT / ".git").exists():
        return (REPO_ROOT / rel_path).exists()
    proc = subprocess.run(
        ["git", "cat-file", "-e", f"HEAD:{rel_path}"],
        cwd=REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    return proc.returncode == 0


def repo_surface_availability_errors(plan: Any | None) -> list[str]:
    if plan is None:
        return []
    errors: list[str] = []
    prior_deliverables: set[str] = set()
    for idx, item in enumerate(getattr(plan, "items", []), start=1):
        for rel_path in getattr(item, "consult_paths", []):
            if rel_path in prior_deliverables or repo_path_tracked_at_head(rel_path):
                continue
            errors.append(
                f"row {idx}: repo_surfaces references path unavailable before row execution: {rel_path}"
            )
        prior_deliverables.update(str(path) for path in getattr(item, "deliverable_paths", []))
    return errors


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
    preamble_lowered = non_table_text(text).lower()
    for term in banned_language:
        if not term:
            continue
        if forbidden_banned_language_present(preamble_lowered, term):
            errors.append(f"forbidden autonomous playbook preamble language: {term}")
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

        raw_roots = row.get("allowed_write_roots", "")
        if "," in raw_roots:
            errors.append(f"row {idx}: allowed_write_roots must use semicolon separators, not commas")
        roots = split_allowed_write_roots(raw_roots)
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

        for field_name, field_value in row.items():
            value_lower = str(field_value).lower()
            if not value_lower:
                continue
            for term in banned_language:
                if not term or not field_contains_term(value_lower, term):
                    continue
                if is_executable_command_field(field_name, field_value):
                    errors.append(f"row {idx}: forbidden autonomous term appears in executable field {field_name}: {term}")
                elif field_name in POLICY_NOTE_FIELDS:
                    if not allowed_negative_policy_context(value_lower, term):
                        errors.append(f"row {idx}: forbidden autonomous term is not boundary language in {field_name}: {term}")
                elif field_name in STRICT_FORBIDDEN_ROW_FIELDS:
                    errors.append(f"row {idx}: forbidden autonomous term appears in {field_name}: {term}")
                elif not allowed_negative_policy_context(value_lower, term):
                    errors.append(f"row {idx}: forbidden autonomous term appears in row: {term}")

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
        for pattern in UNVERIFIED_DEPENDENCY_PATTERNS:
            if re.search(pattern, lower_row, re.I):
                errors.append(f"row {idx}: unverified limiter dependency contract matched /{pattern}/")

        for field_name, field_value in row.items():
            value_lower = str(field_value).lower()
            if not value_lower:
                continue
            for pattern in V2_SCOPE_PATTERNS:
                match = field_has_v2_scope(value_lower, pattern)
                if not match:
                    continue
                if is_executable_command_field(field_name, field_value):
                    errors.append(f"row {idx}: forbidden v2 term appears in executable field {field_name}: /{pattern}/")
                elif field_name in POLICY_NOTE_FIELDS:
                    if not allowed_v2_scope_context(value_lower, match):
                        errors.append(f"row {idx}: v2 scope creep matched /{pattern}/")
                elif field_name in STRICT_FORBIDDEN_ROW_FIELDS:
                    if not allowed_v2_scope_context(value_lower, match):
                        errors.append(f"row {idx}: v2 scope creep matched /{pattern}/")
                elif not allowed_v2_scope_context(value_lower, match):
                    errors.append(f"row {idx}: v2 scope creep matched /{pattern}/")

    errors.extend(temporal_dependency_errors(path, candidate_rows))

    for pattern in UNVERIFIED_DEPENDENCY_PATTERNS:
        if re.search(pattern, lowered, re.I):
            errors.append(f"playbook unverified limiter dependency contract matched /{pattern}/")

    for pattern in V2_SCOPE_PATTERNS:
        for match in re.finditer(pattern, preamble_lowered, re.I):
            if not allowed_v2_scope_context(preamble_lowered, match):
                errors.append(f"playbook v2 scope creep matched /{pattern}/")

    po_errors, po_plan = normalize_plan_orchestrator(path, text)
    errors.extend(po_errors)
    errors.extend(repo_surface_availability_errors(po_plan))

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

#!/usr/bin/env python3
"""Policy-backed allowlist for AutoKeel acceptance commands."""

from __future__ import annotations

import shlex
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


DEFAULT_ALLOW_PREFIXES = (
    "python -m pytest",
)

DEFAULT_ALLOW_COMMANDS = (
    "python scripts/check_schema_contract.py",
    "python scripts/check_no_tracked_data.py",
    "python scripts/check_autonomous_review_exists.py",
    "python scripts/setup_permissions.py",
    "python scripts/evidence/oura_smoke.py",
)


def load_policy(root: Path) -> dict[str, Any]:
    policy_path = root / "ops" / "autonomy" / "policy.yaml"
    if not policy_path.exists():
        return {}
    try:
        from ops.autonomy.autokeel import load_policy as load_autokeel_policy

        return load_autokeel_policy(policy_path)
    except Exception:
        return {}


def acceptance_allow_prefixes(root: Path) -> list[str]:
    policy = load_policy(root)
    prefixes = policy.get("acceptance_commands", {}).get("allow_prefixes", [])
    if not isinstance(prefixes, list) or not prefixes:
        return list(DEFAULT_ALLOW_PREFIXES)
    return [str(prefix) for prefix in prefixes if str(prefix).strip()]


def acceptance_allow_commands(root: Path) -> list[str]:
    policy = load_policy(root)
    commands = policy.get("acceptance_commands", {}).get("allow_commands", [])
    if not isinstance(commands, list) or not commands:
        return list(DEFAULT_ALLOW_COMMANDS)
    return [str(command) for command in commands if str(command).strip()]


def _matches_part(value: str, expected: str, last: bool = False) -> bool:
    if expected.endswith("/"):
        return value.startswith(expected)
    if last and expected == "scripts":
        return value == "scripts" or value.startswith("scripts.")
    return value == expected


def command_allowed(command: str, root: Path) -> bool:
    if "mark-manual-gate" in command:
        return False
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    if not argv:
        return False

    for allowed in acceptance_allow_commands(root):
        try:
            allowed_argv = shlex.split(allowed)
        except ValueError:
            continue
        if len(argv) < len(allowed_argv):
            continue
        if argv[: len(allowed_argv)] == allowed_argv:
            return True

    for prefix in acceptance_allow_prefixes(root):
        try:
            prefix_argv = shlex.split(prefix)
        except ValueError:
            continue
        if len(argv) < len(prefix_argv):
            continue
        if all(
            _matches_part(argv[index], expected, last=index == len(prefix_argv) - 1)
            for index, expected in enumerate(prefix_argv)
        ):
            return True
    return False

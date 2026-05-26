#!/usr/bin/env python3
"""Global v1 verification gate for AutoKeel.

This is the final while-loop exit condition. It must be strict: a slice being
marked complete is not enough. Required deliverables, reviews, acceptance
commands, data-safety checks, and state consistency must all pass.
"""

from __future__ import annotations

import argparse
import json
import re
import shlex
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_autonomous_review_exists import check_review
from scripts.check_no_tracked_data import check_no_tracked_data
from scripts.acceptance_policy import command_allowed
from scripts.swr_lane_policy import validate_swr_lane_requirements
from scripts.validate_playbook_autonomous import validate_playbook


CRITICAL_FAILURES = {
    "manual_gate_leak",
    "secret_leak_risk",
    "unsafe_write_root",
    "forbidden_ui_language",
    "state_divergence",
    "ship_failure",
    "tripwire_triggered",
    "compile_failure",
    "provider_auth_failure",
    "autoplan_invalid",
    "review_artifact_invalid",
    "acceptance_command_rejected",
}

UI_BANNED_RE = re.compile(
    r"\b(biggest drivers|drivers|what made you tired|caused|you should|you would have felt|tomorrow prediction)\b",
    re.I,
)

SECRET_RE = re.compile(
    r"(?i)(access_token|refresh_token|mood_token|x-mood-token|client_secret|password|authorization)"
    r"([\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]{8,})"
)


def redact_text(text: str) -> str:
    return SECRET_RE.sub(r"\1\2[REDACTED]", text)


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def iter_jsonl(path: Path):
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def scan_ui_language(root: Path) -> list[str]:
    errors: list[str] = []
    for base in ("app", "src"):
        path = root / base
        if not path.exists():
            continue
        for file in path.rglob("*"):
            if file.suffix.lower() not in {".py", ".md", ".html", ".txt", ".js", ".ts", ".tsx"}:
                continue
            try:
                text = file.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if UI_BANNED_RE.search(text):
                errors.append(f"forbidden v1 UI language in {file.relative_to(root)}")
    return errors


def git_verify(root: Path, *argv: str) -> bool:
    proc = subprocess.run(["git", *argv], cwd=str(root), stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    return proc.returncode == 0


def git_stdout(root: Path, *argv: str) -> tuple[bool, str, str]:
    proc = subprocess.run(
        ["git", *argv],
        cwd=str(root),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    return proc.returncode == 0, proc.stdout.strip(), proc.stderr.strip()


def run_acceptance(root: Path, command: str, timeout: int) -> dict[str, Any]:
    if "mark-manual-gate" in command:
        return {
            "command": command,
            "exit_code": 99,
            "stdout_tail": "",
            "stderr_tail": "forbidden manual-gate command",
        }
    if not command_allowed(command, root):
        return {
            "command": command,
            "exit_code": 98,
            "stdout_tail": "",
            "stderr_tail": "acceptance command is not allowlisted",
        }

    try:
        proc = subprocess.run(
            shlex.split(command),
            cwd=str(root),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
        return {
            "command": command,
            "exit_code": proc.returncode,
            "stdout_tail": redact_text(proc.stdout[-2000:]),
            "stderr_tail": redact_text(proc.stderr[-2000:]),
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": command,
            "exit_code": 124,
            "stdout_tail": redact_text((exc.stdout or "")[-2000:] if isinstance(exc.stdout, str) else ""),
            "stderr_tail": f"timeout after {timeout}s",
        }


def verify_v1(root: Path, run_acceptance_commands: bool = True, timeout: int = 900) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    command_results: list[dict[str, Any]] = []

    slices = load_json(root / "ops" / "autonomy" / "slices.json", [])
    if not isinstance(slices, list):
        errors.append("ops/autonomy/slices.json must contain a list")
        slices = []

    required = [item for item in slices if item.get("required")]
    incomplete = [item.get("id", "<unknown>") for item in required if item.get("status") != "complete"]
    if incomplete:
        errors.append(f"required slices incomplete: {', '.join(incomplete)}")

    for item in required:
        slice_id = item.get("id", "<unknown>")

        errors.extend(validate_swr_lane_requirements(root, item))

        if item.get("status") == "complete" and not item.get("run_id"):
            errors.append(f"completed slice has no run_id recorded: {slice_id}")

        if item.get("status") == "complete":
            ship_branch = item.get("ship_branch")
            ship_commit = item.get("ship_commit")
            if not ship_branch:
                errors.append(f"{slice_id}: completed slice missing ship_branch")
            elif not git_verify(root, "rev-parse", "--verify", str(ship_branch)):
                errors.append(f"{slice_id}: ship_branch is not reachable: {ship_branch}")
            if not ship_commit:
                errors.append(f"{slice_id}: completed slice missing ship_commit")
            elif not git_verify(root, "cat-file", "-e", str(ship_commit)):
                errors.append(f"{slice_id}: ship_commit is not reachable: {ship_commit}")
            if ship_branch and ship_commit:
                ok, branch_head, stderr = git_stdout(root, "rev-parse", "--verify", str(ship_branch))
                if ok and branch_head != str(ship_commit):
                    errors.append(
                        f"{slice_id}: ship_branch {ship_branch} points to {branch_head} "
                        f"but recorded ship_commit is {ship_commit}"
                    )
                elif not ok:
                    errors.append(f"{slice_id}: could not resolve ship_branch {ship_branch}: {stderr}")

            playbook_rel = item.get("playbook")
            if playbook_rel:
                playbook_report = validate_playbook(root / playbook_rel, risk=item.get("risk"))
                if playbook_report["status"] != "ok":
                    errors.extend([f"{slice_id}: playbook validation failed: {err}" for err in playbook_report["errors"]])
            else:
                errors.append(f"{slice_id}: completed slice has no playbook path")

        for rel in item.get("deliverables", []):
            if not (root / rel).exists():
                errors.append(f"{slice_id}: missing deliverable: {rel}")

        review = check_review(root, slice_id)
        errors.extend([f"{slice_id}: {err}" for err in review["errors"]])

        if run_acceptance_commands and item.get("status") == "complete":
            for command in item.get("acceptance", []):
                if "verify_v1" in command:
                    warnings.append(f"{slice_id}: skipped recursive verify_v1 command: {command}")
                    continue
                if not command_allowed(command, root):
                    errors.append(f"{slice_id}: acceptance command is not allowlisted: {command}")
                    command_results.append({"slice": slice_id, "command": command, "exit_code": 98, "stderr_tail": "acceptance command is not allowlisted"})
                    continue
                result = run_acceptance(root, command, timeout=timeout)
                result["slice"] = slice_id
                command_results.append(result)
                if result["exit_code"] != 0:
                    errors.append(f"{slice_id}: acceptance command failed ({result['exit_code']}): {command}")

    failures = list(iter_jsonl(root / "ops" / "autonomy" / "failure_ledger.jsonl") or [])
    open_critical = [
        item
        for item in failures
        if item.get("open", True)
        and (
            item.get("severity") in {"high", "critical"}
            or item.get("failure_class") in CRITICAL_FAILURES
        )
    ]
    if open_critical:
        errors.append(f"open critical/high failures: {len(open_critical)}")

    tracked = check_no_tracked_data(root)
    errors.extend(tracked["errors"])
    warnings.extend(tracked.get("warnings", []))

    errors.extend(scan_ui_language(root))

    state = load_json(root / "ops" / "autonomy" / "autonomy_state.json", {})
    if state.get("active_run"):
        errors.append("active_run still present in autonomy_state.json")
    if state.get("current_slice"):
        warnings.append(f"current_slice still set: {state.get('current_slice')}")

    return {
        "status": "ok" if not errors else "error",
        "errors": errors,
        "warnings": warnings,
        "required_slices": [item.get("id") for item in required],
        "incomplete_slices": incomplete,
        "open_critical_failures": len(open_critical),
        "commands": command_results,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Verify Health Data Hub v1 autonomous completion.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--skip-acceptance", action="store_true")
    parser.add_argument("--timeout", type=int, default=900)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    report = verify_v1(
        Path(args.root).resolve(),
        run_acceptance_commands=not args.skip_acceptance,
        timeout=args.timeout,
    )

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for error in report["errors"]:
            print(f"ERROR: {error}", file=sys.stderr)
        for warning in report["warnings"]:
            print(f"WARNING: {warning}", file=sys.stderr)
        print(report["status"])
    return 0 if report["status"] == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

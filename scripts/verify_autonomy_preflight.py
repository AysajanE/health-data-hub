#!/usr/bin/env python3
"""Preflight checks for running AutoKeel on Health Data Hub."""

from __future__ import annotations

import argparse
import importlib.util
import json
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.check_no_tracked_data import check_no_tracked_data
from ops.autonomy.autokeel import load_policy, read_json


def command_exists(command: str) -> bool:
    return shutil.which(command) is not None


def git_status(root: Path) -> tuple[bool, str]:
    proc = subprocess.run(["git", "status", "--porcelain"], cwd=str(root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        return False, proc.stderr.strip()
    return proc.stdout.strip() == "", proc.stdout


def validate_policy_shape(root: Path) -> list[str]:
    errors: list[str] = []
    try:
        policy = load_policy(root / "ops" / "autonomy" / "policy.yaml")
    except Exception as exc:
        return [f"policy.yaml does not parse: {exc}"]
    required = ("mode", "keel_root", "manual_gates", "external_evidence", "health_data", "loop", "compile", "autoplan", "slice_statuses")
    for key in required:
        if key not in policy:
            errors.append(f"policy.yaml missing required key: {key}")
    compile_policy = policy.get("compile", {})
    for key in ("design_doc", "row_author", "row_author_command"):
        if key not in compile_policy:
            errors.append(f"policy.yaml compile missing required key: {key}")
    return errors


def validate_schema_minimal(instance: Any, schema: dict[str, Any], label: str) -> list[str]:
    errors: list[str] = []
    expected_type = schema.get("type")
    if expected_type == "object" and not isinstance(instance, dict):
        return [f"{label} must be an object"]
    if expected_type == "array" and not isinstance(instance, list):
        return [f"{label} must be an array"]
    if isinstance(instance, dict):
        for key in schema.get("required", []):
            if key not in instance:
                errors.append(f"{label} missing required schema key: {key}")
    if isinstance(instance, list) and isinstance(schema.get("items"), dict):
        item_schema = schema["items"]
        required = item_schema.get("required", [])
        for index, item in enumerate(instance):
            if not isinstance(item, dict):
                errors.append(f"{label}[{index}] must be an object")
                continue
            for key in required:
                if key not in item:
                    errors.append(f"{label}[{index}] missing required schema key: {key}")
    return errors


def validate_schema_files(root: Path) -> list[str]:
    errors: list[str] = []
    schema_dir = root / "ops" / "autonomy" / "schemas"
    targets = [
        ("policy.yaml", load_policy(root / "ops" / "autonomy" / "policy.yaml"), schema_dir / "policy.schema.json"),
        ("slices.json", read_json(root / "ops" / "autonomy" / "slices.json", []), schema_dir / "slices.schema.json"),
        ("autonomy_state.json", read_json(root / "ops" / "autonomy" / "autonomy_state.json", {}), schema_dir / "state.schema.json"),
    ]
    for label, instance, schema_path in targets:
        if not schema_path.exists():
            errors.append(f"missing schema file: {schema_path.relative_to(root)}")
            continue
        schema = json.loads(schema_path.read_text(encoding="utf-8"))
        try:
            import jsonschema  # type: ignore
        except ImportError:
            errors.extend(validate_schema_minimal(instance, schema, label))
        else:
            try:
                jsonschema.validate(instance=instance, schema=schema)
            except jsonschema.ValidationError as exc:
                errors.append(f"{label} schema validation failed: {exc.message}")
    decision_schema_path = schema_dir / "decision.schema.json"
    decisions_dir = root / "ops" / "autonomy" / "decisions"
    if decisions_dir.exists():
        if not decision_schema_path.exists():
            errors.append(f"missing schema file: {decision_schema_path.relative_to(root)}")
        else:
            decision_schema = json.loads(decision_schema_path.read_text(encoding="utf-8"))
            for decision in decisions_dir.glob("*.json"):
                try:
                    payload = json.loads(decision.read_text(encoding="utf-8"))
                except json.JSONDecodeError as exc:
                    errors.append(f"{decision.relative_to(root)} does not parse as JSON: {exc}")
                    continue
                label = str(decision.relative_to(root))
                try:
                    import jsonschema  # type: ignore
                except ImportError:
                    errors.extend(validate_schema_minimal(payload, decision_schema, label))
                else:
                    try:
                        jsonschema.validate(instance=payload, schema=decision_schema)
                    except jsonschema.ValidationError as exc:
                        errors.append(f"{label} schema validation failed: {exc.message}")
    return errors


def validate_slices_shape(root: Path) -> list[str]:
    errors: list[str] = []
    slices = read_json(root / "ops" / "autonomy" / "slices.json", [])
    if not isinstance(slices, list):
        return ["slices.json must contain a list"]
    seen: set[str] = set()
    for idx, slice_ in enumerate(slices, start=1):
        slice_id = slice_.get("id")
        if not slice_id:
            errors.append(f"slice {idx} missing id")
            continue
        if slice_id in seen:
            errors.append(f"duplicate slice id: {slice_id}")
        seen.add(slice_id)
        for key in ("slug", "status", "required", "playbook", "brief", "autoplan"):
            if key not in slice_:
                errors.append(f"{slice_id} missing required key: {key}")
        for dep in slice_.get("depends_on", []):
            if dep not in seen and not any(item.get("id") == dep for item in slices):
                errors.append(f"{slice_id} depends on unknown slice: {dep}")
    return errors


def preflight(root: Path, keel_root: Path, strict_tools: bool = False, run_keel_smoke: bool = False, strict_clean: bool = False) -> dict[str, Any]:
    errors: list[str] = []
    warnings: list[str] = []
    checks: dict[str, Any] = {}
    checks["product_repo_exists"] = root.exists()
    if not root.exists():
        errors.append(f"product repo missing: {root}")
    checks["keel_root_exists"] = keel_root.exists()
    if not keel_root.exists():
        errors.append(f"keel root missing: {keel_root}")

    for wrapper in ("keel-smoke", "keel-compile", "keel-run", "keel-doctor", "keel-swr"):
        exists = (keel_root / "bin" / wrapper).exists()
        checks[f"{wrapper}_exists"] = exists
        if not exists:
            errors.append(f"missing Keel wrapper: {keel_root / 'bin' / wrapper}")

    for command in ("git", "python"):
        if not command_exists(command):
            errors.append(f"missing required command: {command}")
    for command in ("claude", "codex", "gh", "jq"):
        if not command_exists(command):
            (errors if strict_tools else warnings).append(f"optional tool missing: {command}")

    required_modules = ("duckdb",)
    checks["python_modules"] = {}
    for module in required_modules:
        available = importlib.util.find_spec(module) is not None
        checks["python_modules"][module] = available
        if not available:
            errors.append(f"missing required Python module: {module}")

    for rel in ("ops/autonomy/policy.yaml", "ops/autonomy/slices.json", "ops/autonomy/autonomy_state.json", "ops/autonomy/events.jsonl", "ops/autonomy/failure_ledger.jsonl"):
        if not (root / rel).exists():
            errors.append(f"missing autonomy file: {rel}")

    errors.extend(validate_policy_shape(root))
    errors.extend(validate_slices_shape(root))
    errors.extend(validate_schema_files(root))

    in_git = subprocess.run(["git", "rev-parse", "--is-inside-work-tree"], cwd=str(root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    checks["git_repo"] = in_git.returncode == 0 and in_git.stdout.strip() == "true"
    if not checks["git_repo"]:
        errors.append(f"not a git repository: {root}")
    else:
        clean, status_text = git_status(root)
        checks["git_clean"] = clean
        if not clean:
            (errors if strict_clean else warnings).append("git worktree has uncommitted or untracked files")
            checks["git_status_porcelain"] = status_text.splitlines()[:20]

    tracked = check_no_tracked_data(root)
    errors.extend(tracked["errors"])
    warnings.extend(tracked.get("warnings", []))

    if run_keel_smoke and (keel_root / "bin" / "keel-smoke").exists():
        proc = subprocess.run([str(keel_root / "bin" / "keel-smoke")], cwd=str(keel_root), text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        checks["keel_smoke_exit_code"] = proc.returncode
        if proc.returncode != 0:
            errors.append("keel-smoke failed")

    return {"status": "ok" if not errors else "error", "errors": errors, "warnings": warnings, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run AutoKeel preflight checks.")
    parser.add_argument("--root", default=".")
    parser.add_argument("--keel-root", default="/Users/aeziz-local/keel")
    parser.add_argument("--strict-tools", action="store_true")
    parser.add_argument("--strict-clean", action="store_true")
    parser.add_argument("--run-keel-smoke", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    report = preflight(
        Path(args.root).resolve(),
        Path(args.keel_root).resolve(),
        strict_tools=args.strict_tools,
        run_keel_smoke=args.run_keel_smoke,
        strict_clean=args.strict_clean,
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

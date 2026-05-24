#!/usr/bin/env python3
"""Keel-native autonomous supervisor for the Health Data Hub build.

AutoKeel is intentionally a thin wrapper around Keel. It owns durable
autonomy state, policy enforcement, event/failure logging, and terminal-state
routing. Keel and plan-orchestrator remain the execution kernel.
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shlex
import signal
import subprocess
import sys
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable


class AutoKeelError(RuntimeError):
    """Base exception for AutoKeel failures."""


class PolicyError(AutoKeelError):
    """Raised when a requested action violates autonomy policy."""


class AutoKeelLock:
    def __init__(self, path: Path):
        self.path = path
        self.handle = None

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.handle = self.path.open("w")
        try:
            fcntl.flock(self.handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise AutoKeelError(f"another AutoKeel instance is already running: {self.path}") from exc
        self.handle.write(f"{os.getpid()}\n")
        self.handle.flush()
        return self

    def __exit__(self, exc_type, exc, tb):
        if self.handle:
            fcntl.flock(self.handle, fcntl.LOCK_UN)
            self.handle.close()


@dataclass(frozen=True)
class CommandResult:
    argv: list[str]
    exit_code: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def slug_ts() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")


def read_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    text = path.read_text(encoding="utf-8").strip()
    if not text:
        return default
    return json.loads(text)


def write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def _parse_scalar(value: str) -> Any:
    value = value.strip()
    if value in {"", "null", "Null", "NULL", "~"}:
        return None
    lowered = value.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_parse_scalar(part.strip()) for part in inner.split(",")]
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _simple_yaml_load(text: str) -> Any:
    """Load the small YAML subset used by policy.yaml without a dependency."""
    lines: list[tuple[int, str]] = []
    for raw in text.splitlines():
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        content = raw.split(" #", 1)[0].rstrip()
        lines.append((len(content) - len(content.lstrip(" ")), content.lstrip(" ")))

    def parse_block(index: int, indent: int) -> tuple[Any, int]:
        if index >= len(lines):
            return {}, index
        is_list = lines[index][1].startswith("- ")
        if is_list:
            result: list[Any] = []
            while index < len(lines) and lines[index][0] == indent and lines[index][1].startswith("- "):
                item = lines[index][1][2:].strip()
                if item:
                    result.append(_parse_scalar(item))
                    index += 1
                else:
                    child, index = parse_block(index + 1, lines[index + 1][0])
                    result.append(child)
            return result, index

        result_dict: dict[str, Any] = {}
        while index < len(lines) and lines[index][0] == indent and not lines[index][1].startswith("- "):
            line = lines[index][1]
            if ":" not in line:
                raise ValueError(f"Unsupported YAML line: {line}")
            key, raw_value = line.split(":", 1)
            key = key.strip()
            raw_value = raw_value.strip()
            index += 1
            if raw_value:
                result_dict[key] = _parse_scalar(raw_value)
                continue
            if index < len(lines) and lines[index][0] > indent:
                child, index = parse_block(index, lines[index][0])
                result_dict[key] = child
            else:
                result_dict[key] = {}
        return result_dict, index

    payload, final = parse_block(0, lines[0][0] if lines else 0)
    if final != len(lines):
        raise ValueError("Could not parse complete YAML document")
    return payload


def load_policy(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    try:
        import yaml  # type: ignore
    except ImportError:
        payload = _simple_yaml_load(text) or {}
    else:
        payload = yaml.safe_load(text) or {}

    if not isinstance(payload, dict):
        raise PolicyError(f"Policy file must parse to an object: {path}")
    return payload


SECRET_KEY_RE = re.compile(r"(token|secret|password|api[_-]?key|authorization)", re.I)
SECRET_VALUE_RE = re.compile(
    r"(?i)(token|secret|password|api[_-]?key|authorization|x-mood-token)"
    r"([\"']?\s*[:=]\s*[\"']?)([^\"'\s,}]{8,})"
)
RAW_PATH_RE = re.compile(r"(?i)(data/(raw|secrets|quarantine|snapshots)|warehouse\.duckdb)")


def redact(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            if SECRET_KEY_RE.search(str(key)):
                redacted[key] = "[REDACTED]"
            else:
                redacted[key] = redact(item)
        return redacted
    if isinstance(value, list):
        return [redact(item) for item in value]
    if isinstance(value, str):
        value = SECRET_VALUE_RE.sub(r"\1\2[REDACTED]", value)
        value = RAW_PATH_RE.sub("[SENSITIVE_PATH]", value)
        return value
    return value


class CommandRunner:
    def __init__(self, root: Path, policy: dict[str, Any], dry_run: bool = False, timeout: int = 600):
        self.root = root
        self.policy = policy
        self.dry_run = dry_run
        self.timeout = timeout

    def assert_allowed(self, argv: list[str]) -> None:
        text = " ".join(shlex.quote(part) for part in argv)
        forbidden = self.policy.get("manual_gates", {}).get("forbidden_commands", [])
        for command in forbidden:
            if command and command in text:
                raise PolicyError(f"Forbidden command under autonomous policy: {command}")
        if any("mark-manual-gate" in part for part in argv):
            raise PolicyError("Forbidden command under autonomous policy: mark-manual-gate")

    def run(
        self,
        argv: list[str],
        cwd: Path | None = None,
        env: dict[str, str] | None = None,
        execute_in_dry_run: bool = False,
        timeout: int | None = None,
    ) -> CommandResult:
        self.assert_allowed(argv)
        if self.dry_run and not execute_in_dry_run:
            return CommandResult(argv=argv, exit_code=0, stdout='{"dry_run": true}', stderr="")

        effective_timeout = self.timeout if timeout is None else timeout
        proc: subprocess.Popen[str] | None = None
        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd or self.root),
                env={**os.environ, **(env or {})},
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            stdout, stderr = proc.communicate(timeout=effective_timeout)
            return CommandResult(argv=argv, exit_code=proc.returncode, stdout=stdout, stderr=stderr)
        except subprocess.TimeoutExpired as exc:
            if proc is not None:
                try:
                    os.killpg(proc.pid, signal.SIGTERM)
                    stdout, stderr = proc.communicate(timeout=10)
                except subprocess.TimeoutExpired:
                    os.killpg(proc.pid, signal.SIGKILL)
                    stdout, stderr = proc.communicate()
            else:
                stdout = exc.stdout if isinstance(exc.stdout, str) else ""
                stderr = exc.stderr if isinstance(exc.stderr, str) else ""
            timeout_msg = f"command timed out after {effective_timeout}s: {' '.join(shlex.quote(part) for part in argv)}"
            return CommandResult(
                argv=argv,
                exit_code=124,
                stdout=stdout or "",
                stderr=(stderr + "\n" + timeout_msg).strip(),
            )


class AutoKeel:
    ACTIONABLE_STATUSES = {"pending", "waiting_for_playbook", "replan_required", "evidence_ready"}
    BLOCKED_STATUSES = {"blocked", "blocked_external", "blocked_external_waiting_for_evidence", "blocked_compile_inputs", "complete"}
    DRY_RUN_RESTORE_PATHS = (
        "ops/autonomy/autonomy_state.json",
        "ops/autonomy/events.jsonl",
        "ops/autonomy/failure_ledger.jsonl",
        "ops/autonomy/progress.md",
        "ops/autonomy/slices.json",
    )

    def __init__(self, root: Path, dry_run: bool = False):
        self.root = root.resolve()
        self.autonomy_dir = self.root / "ops" / "autonomy"
        self.policy_path = self.autonomy_dir / "policy.yaml"
        self.slices_path = self.autonomy_dir / "slices.json"
        self.state_path = self.autonomy_dir / "autonomy_state.json"
        self.events_path = self.autonomy_dir / "events.jsonl"
        self.failure_path = self.autonomy_dir / "failure_ledger.jsonl"
        self.progress_path = self.autonomy_dir / "progress.md"
        self.policy = load_policy(self.policy_path)
        self.runner = CommandRunner(self.root, self.policy, dry_run=dry_run)
        self.dry_run = dry_run
        statuses = self.policy.get("slice_statuses", {})
        self.actionable_statuses = set(statuses.get("actionable", self.ACTIONABLE_STATUSES))
        self.blocked_statuses = set(statuses.get("blocked", self.BLOCKED_STATUSES))

    def snapshot_dry_run_state(self) -> dict[Path, bytes | None]:
        snapshots: dict[Path, bytes | None] = {}
        for rel in self.DRY_RUN_RESTORE_PATHS:
            path = self.root / rel
            snapshots[path] = path.read_bytes() if path.exists() else None
        return snapshots

    def restore_dry_run_state(self, snapshots: dict[Path, bytes | None]) -> None:
        for path, content in snapshots.items():
            if content is None:
                if path.exists():
                    path.unlink()
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)

    def load_state(self) -> dict[str, Any]:
        return read_json(self.state_path, {})

    def save_state(self, state: dict[str, Any]) -> None:
        write_json_atomic(self.state_path, state)

    def load_slices(self) -> list[dict[str, Any]]:
        payload = read_json(self.slices_path, [])
        if not isinstance(payload, list):
            raise AutoKeelError("slices.json must contain a list")
        return payload

    def save_slices(self, slices: list[dict[str, Any]]) -> None:
        write_json_atomic(self.slices_path, slices)

    def log_event(self, event_type: str, details: dict[str, Any] | None = None, slice_id: str | None = None) -> dict[str, Any]:
        state = self.load_state()
        event_id = int(state.get("last_event_id") or 0) + 1
        payload = {
            "event_id": event_id,
            "ts": now_iso(),
            "event": event_type,
            "slice": slice_id,
            "details": redact(details or {}),
        }
        append_jsonl(self.events_path, payload)
        state["last_event_id"] = event_id
        self.save_state(state)
        return payload

    def append_progress(self, slice_id: str | None, event_type: str, details: dict[str, Any] | None = None) -> None:
        details = redact(details or {})
        parts = []
        for key in ("status", "reason", "run_id", "playbook", "evidence_request", "ship_branch", "ship_commit", "failure_path"):
            value = details.get(key)
            if value:
                parts.append(f"{key}={value}")
        suffix = f": {', '.join(parts)}" if parts else ""
        self.progress_path.parent.mkdir(parents=True, exist_ok=True)
        with self.progress_path.open("a", encoding="utf-8") as handle:
            handle.write(f"- {now_iso()} {slice_id or '-'} {event_type}{suffix}\n")

    def log_heartbeat(self) -> None:
        state = self.load_state()
        heartbeat = {
            "ts": now_iso(),
            "mode": self.policy.get("mode"),
            "current_slice": state.get("current_slice"),
            "active_run": state.get("active_run"),
            "v1_complete": bool(state.get("v1_complete")),
        }
        heartbeat_path = self.root / self.policy.get("loop", {}).get("heartbeat_path", "ops/autonomy/heartbeats/latest.json")
        write_json_atomic(heartbeat_path, heartbeat)
        state["last_heartbeat_at"] = heartbeat["ts"]
        self.save_state(state)
        self.log_event("heartbeat", heartbeat)

    def choose_next_slice(self, requested: str | None = None, force: bool = False) -> dict[str, Any] | None:
        slices = self.load_slices()
        completed = {item.get("id") for item in slices if item.get("status") == "complete"}

        if requested:
            for slice_ in slices:
                if slice_.get("id") == requested or slice_.get("slug") == requested:
                    if slice_.get("status") == "complete":
                        return None
                    if not force:
                        deps = set(slice_.get("depends_on", []))
                        missing = sorted(deps - completed)
                        if missing:
                            raise AutoKeelError(f"Requested slice {requested} has incomplete dependencies: {', '.join(missing)}")
                    return slice_
            raise AutoKeelError(f"Unknown slice: {requested}")

        for slice_ in slices:
            if not slice_.get("required"):
                continue
            status = slice_.get("status", "pending")
            if status in self.blocked_statuses:
                continue

            deps = set(slice_.get("depends_on", []))
            if deps and not deps.issubset(completed):
                continue

            if status in self.actionable_statuses:
                return slice_

        return None

    def mark_slice_status(self, slice_id: str, status: str, **extra: Any) -> None:
        slices = self.load_slices()
        found = False

        for slice_ in slices:
            if slice_.get("id") != slice_id:
                continue

            if status == "replan_required":
                retry_count = int(slice_.get("retry_count") or 0) + 1
                slice_["retry_count"] = retry_count
                max_retries = int(self.policy.get("loop", {}).get("max_retries_per_slice_before_replan", 3))
                if retry_count >= max_retries:
                    status = "blocked"
                    extra.setdefault("reason", "retry cap exceeded")

            if status == "complete":
                slice_["retry_count"] = 0

            slice_["status"] = status
            slice_["updated_at"] = now_iso()
            slice_.update(extra)
            found = True
            break

        if not found:
            raise AutoKeelError(f"Unknown slice: {slice_id}")

        self.save_slices(slices)

        state = self.load_state()
        active = state.get("active_run") or {}
        active_belongs_to_slice = active.get("slice") == slice_id

        if status == "complete":
            completed = list(dict.fromkeys([*state.get("completed_slices", []), slice_id]))
            state["completed_slices"] = completed
            if state.get("current_slice") == slice_id:
                state["current_slice"] = None
            if active_belongs_to_slice:
                state["active_run"] = None

        elif status in {"replan_required", "blocked", "blocked_compile_inputs"}:
            state["current_slice"] = slice_id if status == "replan_required" else None
            if active_belongs_to_slice:
                state["active_run"] = None

        elif status in {"blocked_external", "blocked_external_waiting_for_evidence"}:
            state["current_slice"] = None

        else:
            state["current_slice"] = slice_id

        self.save_state(state)
        self.append_progress(slice_id, "slice_status_updated", {"status": status, **extra})
        self.log_event("slice_status_updated", {"status": status, **extra}, slice_id=slice_id)

    def ensure_slice_brief(self, slice_: dict[str, Any]) -> Path:
        brief_path = self.root / slice_["brief"]
        if brief_path.exists():
            return brief_path
        brief_path.parent.mkdir(parents=True, exist_ok=True)
        deliverables = "\n".join(f"- `{path}`" for path in slice_.get("deliverables", [])) or "- See slice playbook."
        constraints = "\n".join(f"- {item}" for item in slice_.get("hard_constraints", [])) or "- Preserve Health Data Hub v1 invariants."
        text = f"""# {slice_['id']} {slice_['name']} Autonomous Brief

Autonomy profile: true.

Manual gates are forbidden for this autonomous run. Any former signoff must be represented as an `autonomous_gate_review` artifact, deterministic tests, and recorded evidence.

## Deliverables

{deliverables}

## Hard Constraints

{constraints}

## Required Policy

- Never emit active `manual_gate` rows.
- Never call `keel-run mark-manual-gate`.
- Use narrow repo-relative write roots only.
- Keep raw health data, secrets, tokens, quarantine payloads, snapshots, and DuckDB files out of git and general logs.
- Preserve the retrospective-only v1 scope and statistical gates from the design document.
"""
        brief_path.write_text(text, encoding="utf-8")
        self.log_event("brief_created", {"path": str(brief_path.relative_to(self.root))}, slice_id=slice_["id"])
        return brief_path

    def design_doc_path(self, slice_: dict[str, Any]) -> Path:
        design_rel = slice_.get("design_doc") or self.policy.get("compile", {}).get("design_doc") or "docs/gstack/health-data-hub-office-hours.md"
        design = self.root / design_rel
        if design.exists():
            return design
        fallback = self.root / "aeziz-local-AysajanE-health-data-hub-design-20260515-114138.md"
        if fallback.exists():
            return fallback
        raise AutoKeelError(f"missing design doc: {design}")

    def plan_orchestrator_root(self) -> str:
        configured = os.environ.get("KEEL_PO_ROOT") or self.policy.get("plan_orchestrator_root")
        if configured:
            return str(configured)
        return str(Path(self.policy.get("keel_root", "/Users/aeziz-local/keel")) / "tools" / "plan-orchestrator")

    def ensure_plan_orchestrator_product_shim(self) -> Path:
        """Expose the Keel PO runtime under this product repo for repo-root resolution."""
        tool_root = Path(self.plan_orchestrator_root())
        tool_runner = tool_root / "automation" / "run_plan_orchestrator.py"
        tool_package = tool_root / "automation" / "plan_orchestrator"
        if not tool_runner.is_file():
            raise AutoKeelError(f"plan-orchestrator runner not found: {tool_runner}")
        if not tool_package.is_dir():
            raise AutoKeelError(f"plan-orchestrator package not found: {tool_package}")

        automation_dir = self.root / "automation"
        automation_dir.mkdir(exist_ok=True)
        runner = automation_dir / "run_plan_orchestrator.py"
        package = automation_dir / "plan_orchestrator"
        self._ensure_local_shim_path(package, tool_package)
        self._ensure_local_shim_path(runner, tool_runner)
        return runner

    def _ensure_local_shim_path(self, path: Path, target: Path) -> None:
        if path.is_symlink():
            if path.resolve() == target.resolve():
                return
            path.unlink()
        elif path.exists():
            return
        path.symlink_to(target, target_is_directory=target.is_dir())

    def plan_orchestrator_command(self, *args: str) -> list[str]:
        runner = self.ensure_plan_orchestrator_product_shim()
        return [sys.executable, str(runner), *args]

    def po_timeout_seconds(self) -> int:
        return int(self.policy.get("loop", {}).get("po_timeout_seconds", 7200))

    def checkpoint_allowed_pre_po_changes(self, slice_id: str) -> CommandResult:
        status = self._git_status_paths()
        if status is None or not status:
            return CommandResult(["git", "status", "--porcelain"], 0, "clean", "")

        disallowed = [path for path in status if not self._is_pre_po_checkpoint_path(path)]
        if disallowed:
            return CommandResult(
                ["git", "status", "--porcelain"],
                42,
                "",
                "refusing to start PO with non-AutoKeel dirty paths: " + ", ".join(disallowed),
            )

        paths = sorted(set(status))
        add = subprocess.run(
            ["git", "add", "--", *paths],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if add.returncode != 0:
            return CommandResult(["git", "add", "--", *paths], add.returncode, add.stdout, add.stderr)

        diff = subprocess.run(
            ["git", "diff", "--cached", "--quiet"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if diff.returncode == 0:
            return CommandResult(["git", "diff", "--cached", "--quiet"], 0, "nothing staged", "")

        commit = subprocess.run(
            ["git", "commit", "-m", f"Record AutoKeel {slice_id} pre-PO state"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        return CommandResult(
            ["git", "commit", "-m", f"Record AutoKeel {slice_id} pre-PO state"],
            commit.returncode,
            commit.stdout,
            commit.stderr,
        )

    def _git_status_paths(self) -> list[str] | None:
        probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if probe.returncode != 0:
            return None
        status = subprocess.run(
            ["git", "status", "--porcelain=v1", "--untracked-files=all"],
            cwd=self.root,
            text=True,
            capture_output=True,
            check=False,
        )
        if status.returncode != 0:
            return None
        paths: list[str] = []
        for line in status.stdout.splitlines():
            if not line.strip():
                continue
            path = line[3:] if len(line) > 3 else line
            if " -> " in path:
                paths.extend(part.strip('"') for part in path.split(" -> ", 1))
            else:
                paths.append(path.strip('"'))
        return paths

    def _is_pre_po_checkpoint_path(self, path: str) -> bool:
        return (
            path.startswith("ops/autonomy/")
            or path.startswith("docs/briefs/")
            or path.startswith("docs/evidence/")
            or path.startswith("docs/gstack/")
            or path.startswith("docs/playbooks/")
        )

    def validate_autoplan_text(self, slice_: dict[str, Any], text: str) -> list[str]:
        lowered = text.lower()
        errors: list[str] = []
        slice_id = str(slice_.get("id", "")).lower()
        if "write permission was denied" in lowered or "let me know if" in lowered:
            errors.append("autoplan contains assistant wrapper/refusal text")
        if slice_id and slice_id not in lowered:
            errors.append(f"autoplan missing slice id {slice_.get('id')}")
        if "deliverable" not in lowered:
            errors.append("autoplan missing deliverables section or term")
        if "verification" not in lowered:
            errors.append("autoplan missing verification expectations")
        if "manual gate" not in lowered and "manual_gate" not in lowered:
            errors.append("autoplan missing explicit no manual gate policy")
        if "implementation tasks" not in lowered:
            errors.append("autoplan missing Implementation Tasks section")
        has_files = re.search(r"(?im)^\s*files\s*:", text) or re.search(r"(?im)^\s*\|.*\bfiles\b.*\|", text)
        has_verify = re.search(r"(?im)^\s*verify\s*:", text) or re.search(r"(?im)^\s*\|.*\bverify\b.*\|", text)
        if not has_files:
            errors.append("autoplan missing compiler-parseable Files fields")
        if not has_verify:
            errors.append("autoplan missing compiler-parseable Verify fields")
        if slice_.get("risk") == "high" and "autonomous_gate_review" not in lowered:
            errors.append("high-risk autoplan missing autonomous_gate_review requirement")
        return errors

    def build_autoplan_prompt(
        self,
        slice_: dict[str, Any],
        design: Path,
        autoplan_rel: str,
        corrective_errors: list[str] | None = None,
    ) -> str:
        correction = ""
        if corrective_errors:
            correction = (
                "\nPrevious autoplan attempt failed these checks:\n"
                + "\n".join(f"- {error}" for error in corrective_errors)
                + "\n\nRewrite the autoplan so every failed check is explicitly satisfied.\n"
            )
        return f"""Create a Keel autoplan artifact for exactly one slice.

Repository: {self.root}
Slice ID: {slice_["id"]}
Slice name: {slice_.get("name")}
Lane: {slice_.get("lane")}
Risk: {slice_.get("risk")}

Use the design doc at:
{design}

Autonomy requirements:
- Manual gates are forbidden.
- Use autonomous_gate_review artifacts instead of human approval.
- Keep write roots narrow and repo-relative.
- Preserve Health Data Hub v1 scope.
- Include concrete deliverables and verification expectations.
- Include a `## Implementation Tasks` section.
- Under each implementation task, include `Files:` with concrete repo-relative paths.
- Under each implementation task, include `Verify:` with concrete acceptance commands.
- Do not include v2 features or prospective recommendations.
{correction}
Slice JSON:
{json.dumps(redact(slice_), indent=2, sort_keys=True)}

Return only a Markdown autoplan suitable to save at:
{autoplan_rel}
"""

    def archive_invalid_autoplan(self, slice_: dict[str, Any], autoplan: Path, errors: list[str]) -> Path:
        archive_dir = self.root / "ops" / "autonomy" / "failures" / "archived_autoplans"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive = archive_dir / f"{slice_['id']}-{slug_ts()}-{uuid.uuid4().hex[:8]}-{autoplan.name}"
        os.replace(autoplan, archive)
        self.log_event(
            "autoplan_archived_for_retry",
            {
                "from": str(autoplan.relative_to(self.root)),
                "to": str(archive.relative_to(self.root)),
                "errors": errors,
            },
            slice_id=slice_["id"],
        )
        return archive

    def generate_autoplan(
        self,
        slice_: dict[str, Any],
        autoplan: Path,
        autoplan_rel: str,
        corrective_errors: list[str] | None = None,
    ) -> CommandResult:
        autoplan_policy = self.policy.get("autoplan", {})
        design = self.design_doc_path(slice_)
        autoplan.parent.mkdir(parents=True, exist_ok=True)

        command = autoplan_policy.get("command") or self.policy.get("compile", {}).get("row_author_command") or "claude -p"
        prompt = self.build_autoplan_prompt(slice_, design, autoplan_rel, corrective_errors=corrective_errors)
        argv = shlex.split(command) + [prompt]
        if self.dry_run:
            self.log_event(
                "autoplan_generation_planned",
                {"path": str(autoplan.relative_to(self.root)), "command": command, "corrective": bool(corrective_errors)},
                slice_id=slice_["id"],
            )
            return CommandResult(argv, 0, "dry run autoplan generation planned", "")

        result = self.runner.run(argv, cwd=self.root)

        if not result.ok:
            self.log_event(
                "autoplan_generation_failed",
                {"exit_code": result.exit_code, "stdout": result.stdout[-2000:], "stderr": result.stderr[-2000:]},
                slice_id=slice_["id"],
            )
            return result

        if len(result.stdout.strip()) < 200:
            self.log_event(
                "autoplan_generation_failed",
                {"reason": "autoplan output too short", "stdout": result.stdout[-1000:]},
                slice_id=slice_["id"],
            )
            return CommandResult(argv, 23, result.stdout, "autoplan output too short")

        errors = self.validate_autoplan_text(slice_, result.stdout)
        if errors:
            self.log_event(
                "autoplan_invalid",
                {"errors": errors, "stdout": result.stdout[-1000:], "corrective": bool(corrective_errors)},
                slice_id=slice_["id"],
            )
            return CommandResult(argv, 24, result.stdout, "; ".join(errors))

        autoplan.write_text(result.stdout, encoding="utf-8")
        self.log_event("autoplan_created", {"path": str(autoplan.relative_to(self.root))}, slice_id=slice_["id"])
        return CommandResult(argv, 0, result.stdout, result.stderr)

    def ensure_autoplan(self, slice_: dict[str, Any]) -> CommandResult:
        autoplan_rel = slice_.get("autoplan")
        if not autoplan_rel:
            self.mark_slice_status(slice_["id"], "blocked_compile_inputs", reason="missing autoplan path")
            return CommandResult([], 20, "", "missing autoplan path in slices.json")

        autoplan = self.root / autoplan_rel
        if autoplan.exists():
            errors = self.validate_autoplan_text(slice_, autoplan.read_text(encoding="utf-8"))
            if not errors:
                return CommandResult(["test", "-f", str(autoplan)], 0, "autoplan exists", "")
            self.archive_invalid_autoplan(slice_, autoplan, errors)
            result = self.generate_autoplan(slice_, autoplan, str(autoplan_rel), corrective_errors=errors)
            if result.ok:
                return result
            failure = self.record_failure(
                slice_["id"],
                "autoplan_invalid",
                "high",
                "Existing autoplan was invalid and one corrective regeneration attempt failed.",
                "Archived the invalid autoplan and blocked compilation after the bounded retry.",
                autoplan,
            )
            self.mark_slice_status(slice_["id"], "blocked_compile_inputs", failure_path=str(failure.relative_to(self.root)), reason="autoplan invalid")
            return result

        autoplan_policy = self.policy.get("autoplan", {})
        auto_generate = bool(autoplan_policy.get("auto_generate_missing", True))
        if not auto_generate:
            self.mark_slice_status(slice_["id"], "blocked_compile_inputs", reason=f"missing {autoplan_rel}")
            return CommandResult([], 21, "", f"missing autoplan: {autoplan}")

        result = self.generate_autoplan(slice_, autoplan, str(autoplan_rel))
        if result.ok or result.exit_code not in {23, 24}:
            return result

        retry = self.generate_autoplan(slice_, autoplan, str(autoplan_rel), corrective_errors=[result.stderr or "autoplan generation failed validation"])
        if retry.ok:
            return retry

        failure = self.record_failure(
            slice_["id"],
            "autoplan_invalid",
            "high",
            "Generated autoplan does not contain the required autonomous compiler facts after one corrective retry.",
            "Rejected generated autoplan and blocked compilation until a valid autoplan exists.",
            None,
        )
        self.mark_slice_status(slice_["id"], "blocked_compile_inputs", failure_path=str(failure.relative_to(self.root)), reason="autoplan invalid")
        return retry

    def archive_playbook_for_replan(self, slice_: dict[str, Any]) -> Path | None:
        playbook = self.root / slice_["playbook"]
        if not playbook.exists():
            return None
        archive_dir = self.root / "ops" / "autonomy" / "failures" / "archived_playbooks"
        archive_dir.mkdir(parents=True, exist_ok=True)
        archive = archive_dir / f"{slice_['id']}-{slug_ts()}-{playbook.name}"
        os.replace(playbook, archive)
        self.log_event(
            "playbook_archived_for_replan",
            {"from": str(playbook.relative_to(self.root)), "to": str(archive.relative_to(self.root))},
            slice_id=slice_["id"],
        )
        return archive

    def ensure_playbook(self, slice_: dict[str, Any]) -> CommandResult:
        playbook = self.root / slice_["playbook"]

        if slice_.get("status") == "replan_required":
            self.archive_playbook_for_replan(slice_)

        if playbook.exists():
            return CommandResult(["test", "-f", str(playbook)], 0, "playbook exists", "")

        autoplan_result = self.ensure_autoplan(slice_)
        if not autoplan_result.ok:
            return autoplan_result

        autoplan_rel = slice_.get("autoplan")
        autoplan = self.root / autoplan_rel
        brief = self.root / slice_["brief"]
        design = self.design_doc_path(slice_)

        playbook.parent.mkdir(parents=True, exist_ok=True)

        keel_compile = Path(self.policy.get("keel_root", "/Users/aeziz-local/keel")) / "bin" / "keel-compile"
        cmd = [
            str(keel_compile),
            "compile",
            "--repo-root",
            str(self.root),
            "--design",
            str(design),
            "--autoplan",
            str(autoplan),
            "--approved-brief",
            str(brief),
            "--out",
            str(playbook),
            "--row-author",
            self.policy.get("compile", {}).get("row_author", "external-json"),
            "--row-author-command",
            self.policy.get("compile", {}).get("row_author_command", "claude -p"),
            "--plan-orchestrator-root",
            self.plan_orchestrator_root(),
            "--human-approved-by",
            "AUTO-KEEL-AUTONOMOUS-NOT-HUMAN",
        ]
        compile_policy = self.policy.get("compile", {})
        if compile_policy.get("row_author_allow_repo_cwd"):
            cmd.append("--row-author-allow-repo-cwd")
        allow_warnings_reason = str(compile_policy.get("allow_warnings_reason") or "").strip()
        if allow_warnings_reason:
            cmd.extend(["--allow-warnings", allow_warnings_reason])

        result = self.runner.run(cmd, cwd=self.root)
        self.log_event(
            "playbook_compile_passed" if result.ok else "playbook_compile_failed",
            {
                "exit_code": result.exit_code,
                "playbook": str(playbook.relative_to(self.root)),
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            },
            slice_id=slice_["id"],
        )
        return result

    def validate_playbook(self, slice_: dict[str, Any]) -> CommandResult:
        playbook = self.root / slice_["playbook"]
        if not playbook.exists():
            return CommandResult(["python", "-m", "scripts.validate_playbook_autonomous", str(playbook)], 2, "", "missing playbook")
        result = self.runner.run(
            ["python", "-m", "scripts.validate_playbook_autonomous", str(playbook), "--risk", str(slice_.get("risk", "")), "--json"],
            cwd=self.root,
            execute_in_dry_run=True,
        )
        event = "playbook_validated" if result.ok else "playbook_rejected"
        self.log_event(event, {"playbook": str(playbook.relative_to(self.root)), "exit_code": result.exit_code, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}, slice_id=slice_["id"])
        return result

    def run_verify_v1(self) -> CommandResult:
        result = self.runner.run(["python", "-m", "scripts.verify_v1", "--json"], cwd=self.root, execute_in_dry_run=True)
        self.log_event("verify_v1_passed" if result.ok else "verify_v1_failed", {"exit_code": result.exit_code, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]})
        if result.ok:
            state = self.load_state()
            state["v1_complete"] = True
            self.save_state(state)
        return result

    def evaluate_tripwires(self) -> CommandResult:
        result = self.runner.run(["python", "-m", "scripts.evaluate_tripwires", "--json"], cwd=self.root, execute_in_dry_run=True)
        self.log_event(
            "tripwires_ok" if result.ok else "tripwires_fired",
            {"exit_code": result.exit_code, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]},
        )
        return result

    def write_decision(self, decision_id: str, payload: dict[str, Any]) -> Path:
        decision_dir = self.root / "ops" / "autonomy" / "decisions"
        decision_dir.mkdir(parents=True, exist_ok=True)
        path = decision_dir / f"{decision_id}-{slug_ts()}-{uuid.uuid4().hex[:8]}.json"
        write_json_atomic(path, {"created_at": now_iso(), **redact(payload)})
        self.log_event("decision_recorded", {"decision_id": decision_id, "path": str(path.relative_to(self.root)), **payload})
        return path

    def apply_tripwire_fallbacks(self, report: dict[str, Any]) -> bool:
        fired = report.get("fired", [])
        if not isinstance(fired, list) or not fired:
            return False

        state = self.load_state()
        decisions = state.setdefault("tripwire_decisions", {})
        changed = False

        # Only these actions are safe to auto-accept because they reduce scope
        # without bypassing required v1 functionality.
        auto_accept_actions = {"oura_only_v1"}

        for item in fired:
            if not isinstance(item, dict):
                continue

            name = str(item.get("name") or "tripwire")
            action = str(item.get("action") or "fallback_required")

            if decisions.get(name):
                continue

            safe_to_auto_accept = action in auto_accept_actions

            decision_payload = {
                "status": "fallback_accepted" if safe_to_auto_accept else "fallback_required",
                "tripwire": name,
                "action": action,
                "source": "autokeel_tripwire",
                "evidence_status": item.get("evidence_status"),
                "reason": (
                    "Auto-accepted because this fallback reduces optional scope."
                    if safe_to_auto_accept
                    else "Not auto-accepted because this fallback requires implementation or hard-stop behavior."
                ),
            }
            decision_path = self.write_decision(name, decision_payload)

            if safe_to_auto_accept:
                evidence_rel = item.get("evidence")
                if evidence_rel:
                    evidence_path = self.root / str(evidence_rel)
                    report_dir = evidence_path.parent if evidence_path.suffix else evidence_path
                    report_dir.mkdir(parents=True, exist_ok=True)
                    fallback_report = report_dir / f"{name}-fallback-{slug_ts()}-{uuid.uuid4().hex[:8]}.json"
                    write_json_atomic(
                        fallback_report,
                        {
                            "status": "fallback_accepted",
                            "tripwire": name,
                            "action": action,
                            "decision": str(decision_path.relative_to(self.root)),
                            "created_at": now_iso(),
                        },
                    )
                    os.chmod(fallback_report, 0o600)

            decisions[name] = {
                "action": action,
                "decision": str(decision_path.relative_to(self.root)),
                "status": decision_payload["status"],
            }
            changed = True

            if not safe_to_auto_accept:
                self.record_failure(
                    "GLOBAL",
                    "tripwire_triggered",
                    "high",
                    f"Tripwire {name} fired and requires non-automatic fallback action: {action}.",
                    f"Recorded decision artifact {decision_path.relative_to(self.root)}; did not fabricate success evidence.",
                    decision_path,
                )

        if changed:
            self.save_state(state)

        # Return True only if every fired tripwire was safely auto-accepted.
        return bool(fired) and all(
            isinstance(item, dict) and str(item.get("action") or "") in auto_accept_actions
            for item in fired
        )

    def verify_slice_acceptance(self, slice_id: str) -> CommandResult:
        result = self.runner.run(
            ["python", "-m", "scripts.verify_slice", slice_id, "--json"],
            cwd=self.root,
            execute_in_dry_run=True,
        )
        self.log_event(
            "slice_acceptance_passed" if result.ok else "slice_acceptance_failed",
            {
                "exit_code": result.exit_code,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            },
            slice_id=slice_id,
        )
        return result

    def find_slice(self, slice_id: str) -> dict[str, Any] | None:
        return next((item for item in self.load_slices() if item.get("id") == slice_id), None)

    def build_reviewer_prompt(self, slice_: dict[str, Any], artifact_rel: str, run_id: str) -> str:
        prompt_path = self.root / "ops" / "autonomy" / "prompts" / "slice_reviewer.md"
        base_prompt = prompt_path.read_text(encoding="utf-8")
        return f"""{base_prompt}

## Review Request

Slice ID: {slice_.get("id")}
Slice name: {slice_.get("name")}
Run ID: {run_id}
Target review artifact: {artifact_rel}

Slice JSON:
```json
{json.dumps(redact(slice_), indent=2, sort_keys=True)}
```

Write only the complete Markdown review artifact for `{artifact_rel}`.
Use local files and commands only. If evidence is missing, write a failing review.
"""

    def run_reviewer_for_artifact(self, slice_: dict[str, Any], artifact_rel: str, run_id: str) -> CommandResult:
        reviews = self.policy.get("reviews", {})
        command = reviews.get("reviewer_command") or self.policy.get("compile", {}).get("row_author_command") or "claude -p"
        artifact = self.root / artifact_rel
        prompt = self.build_reviewer_prompt(slice_, artifact_rel, run_id)
        argv = shlex.split(command) + [prompt]

        if self.dry_run:
            self.log_event(
                "review_generation_planned",
                {"artifact": artifact_rel, "command": command, "run_id": run_id},
                slice_id=slice_["id"],
            )
            return CommandResult(argv, 0, "dry run review generation planned", "")

        result = self.runner.run(argv, cwd=self.root)
        if not result.ok:
            self.log_event(
                "review_generation_failed",
                {"artifact": artifact_rel, "exit_code": result.exit_code, "stdout": result.stdout[-2000:], "stderr": result.stderr[-2000:]},
                slice_id=slice_["id"],
            )
            return result

        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(result.stdout, encoding="utf-8")
        self.log_event(
            "review_artifact_created",
            {"artifact": artifact_rel, "run_id": run_id, "chars": len(result.stdout)},
            slice_id=slice_["id"],
        )
        return result

    def ensure_review_artifacts(self, slice_id: str, run_id: str) -> CommandResult:
        slice_ = self.find_slice(slice_id)
        if not slice_:
            return CommandResult([], 60, "", f"unknown slice: {slice_id}")

        artifacts = [str(item) for item in slice_.get("review_artifacts", [])]
        if not artifacts:
            return CommandResult([], 0, "no review artifacts required", "")

        missing = [artifact for artifact in artifacts if not (self.root / artifact).exists()]
        reviews_policy = self.policy.get("reviews", {})
        if missing and not bool(reviews_policy.get("auto_generate_missing", True)):
            return self.runner.run(
                ["python", "-m", "scripts.check_autonomous_review_exists", slice_id, "--json"],
                cwd=self.root,
                execute_in_dry_run=True,
            )
        if self.dry_run and missing:
            for artifact in missing:
                self.run_reviewer_for_artifact(slice_, artifact, run_id)
            return CommandResult([], 0, "dry run review generation planned", "")

        for artifact in missing:
            result = self.run_reviewer_for_artifact(slice_, artifact, run_id)
            if not result.ok:
                return result

        check = self.runner.run(
            ["python", "-m", "scripts.check_autonomous_review_exists", slice_id, "--json"],
            cwd=self.root,
            execute_in_dry_run=True,
        )
        self.log_event(
            "review_artifacts_validated" if check.ok else "review_artifacts_rejected",
            {"exit_code": check.exit_code, "stdout": check.stdout[-4000:], "stderr": check.stderr[-4000:]},
            slice_id=slice_id,
        )
        return check

    def active_run_is_stale(self, active: dict[str, Any]) -> bool:
        started_at = active.get("started_at")
        if not started_at:
            return False
        try:
            started = datetime.fromisoformat(str(started_at))
        except ValueError:
            return False
        stale_minutes = int(self.policy.get("loop", {}).get("stale_run_minutes", 60))
        age_seconds = (datetime.now().astimezone() - started.astimezone()).total_seconds()
        return age_seconds > stale_minutes * 60

    def clear_active_run(self) -> None:
        state = self.load_state()
        state["active_run"] = None
        self.save_state(state)

    def resume_po_with_evidence(self, slice_: dict[str, Any], active: dict[str, Any]) -> CommandResult:
        run_id = active.get("run_id")
        evidence_rel = slice_.get("evidence_request") or active.get("evidence_request")
        if not run_id:
            return CommandResult([], 50, "", "cannot resume evidence: missing run_id")
        if not evidence_rel:
            return CommandResult([], 51, "", "cannot resume evidence: missing evidence_request")

        evidence_dir = self.root / str(evidence_rel)
        if not evidence_dir.exists():
            return CommandResult([], 52, "", f"cannot resume evidence: missing directory {evidence_dir}")

        checkpoint = self.checkpoint_allowed_pre_po_changes(slice_["id"])
        if not checkpoint.ok:
            return checkpoint

        result = self.runner.run(
            self.plan_orchestrator_command(
                "supervise",
                "resume",
                "--run-id",
                str(run_id),
                "--external-evidence-dir",
                str(evidence_dir),
            ),
            cwd=self.root,
            env={"PLAN_ORCHESTRATOR_CLEAN_ENV_CONFIRMED": "1"},
            timeout=self.po_timeout_seconds(),
        )
        self.log_event(
            "po_resumed_with_evidence" if result.ok else "po_resume_with_evidence_failed",
            {
                "run_id": run_id,
                "evidence_dir": str(evidence_dir.relative_to(self.root)),
                "exit_code": result.exit_code,
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-2000:],
            },
            slice_id=slice_["id"],
        )
        return result

    def start_or_resume_po(self, slice_: dict[str, Any]) -> CommandResult:
        state = self.load_state()
        active = state.get("active_run") or {}
        if active.get("slice") == slice_["id"] and active.get("run_id") and slice_.get("status") == "evidence_ready":
            return self.resume_po_with_evidence(slice_, active)
        if active.get("slice") == slice_["id"] and active.get("run_id"):
            if self.active_run_is_stale(active):
                self.record_failure(
                    slice_["id"],
                    "stale_run",
                    "medium",
                    "PO run exceeded the configured stale_run_minutes threshold.",
                    "Cleared active_run and moved the slice to replan_required.",
                    None,
                    run_id=active.get("run_id"),
                )
                self.clear_active_run()
                self.mark_slice_status(slice_["id"], "replan_required", run_id=active.get("run_id"), reason="stale run")
                return CommandResult([], 40, "", "active PO run is stale")
            self.log_event("po_resume_existing", active, slice_id=slice_["id"])
            return CommandResult([], 0, json.dumps(active), "")

        playbook = self.root / slice_["playbook"]
        if not playbook.exists():
            self.log_event("playbook_missing", {"playbook": str(playbook.relative_to(self.root))}, slice_id=slice_["id"])
            return CommandResult([], 3, "", f"missing playbook: {playbook}")
        checkpoint = self.checkpoint_allowed_pre_po_changes(slice_["id"])
        if not checkpoint.ok:
            return checkpoint
        result = self.runner.run(
            self.plan_orchestrator_command(
                "supervise",
                "run",
                "--playbook",
                str(playbook.relative_to(self.root)),
                "--next",
            ),
            cwd=self.root,
            env={"PLAN_ORCHESTRATOR_CLEAN_ENV_CONFIRMED": "1"},
            timeout=self.po_timeout_seconds(),
        )
        run_id = self._extract_run_id(result.stdout) or self._extract_run_id(result.stderr)
        if not result.ok:
            run_id = None
        if result.ok and run_id:
            state["active_run"] = {"slice": slice_["id"], "run_id": run_id, "started_at": now_iso()}
            state["current_slice"] = slice_["id"]
            history = state.setdefault("run_history", [])
            history.append({"slice": slice_["id"], "run_id": run_id, "started_at": now_iso()})
            self.save_state(state)
        self.log_event("po_started" if result.ok else "po_start_failed", {"exit_code": result.exit_code, "run_id": run_id, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}, slice_id=slice_["id"])
        return result

    def ensure_lane_decision(self, slice_: dict[str, Any]) -> None:
        lane = slice_.get("lane")
        if lane != "swr_preferred":
            return
        policy = self.policy.get("lanes", {})
        mode = policy.get("swr_preferred", "compile_with_decision")
        if mode != "compile_with_decision":
            return
        existing = slice_.get("lane_decision")
        if existing and (self.root / str(existing)).exists():
            return
        decision = self.write_decision(
            f"{slice_['id']}-swr-downgrade",
            {
                "status": "accepted",
                "slice": slice_["id"],
                "lane": lane,
                "decision": "compile_with_keel_compile_for_now",
                "reason": "SWR-preferred lane is preserved as policy metadata; current AutoKeel milestone uses compiler path with explicit downgrade decision.",
            },
        )
        self.mark_slice_status(slice_["id"], slice_.get("status", "pending"), lane_decision=str(decision.relative_to(self.root)))

    def optional_evidence_fallback(self, slice_: dict[str, Any], command: str) -> str | None:
        try:
            argv = shlex.split(command)
        except ValueError:
            return None
        stems = [Path(part).stem for part in argv if part.startswith("scripts/evidence/")]
        fallbacks = slice_.get("fallbacks", {})
        if not isinstance(fallbacks, dict):
            return None
        for stem in stems:
            for key in (f"{stem}_failure", stem):
                action = fallbacks.get(key)
                if action:
                    return str(action)
        return None

    def run_optional_evidence(self, slice_: dict[str, Any]) -> None:
        for command in slice_.get("optional_evidence", []):
            result = self.runner.run(shlex.split(command), cwd=self.root)
            details = {
                "command": command,
                "exit_code": result.exit_code,
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-2000:],
            }
            if not result.ok:
                fallback = self.optional_evidence_fallback(slice_, command)
                if fallback:
                    details["fallback_action"] = fallback
                    details["decision_hint"] = "optional evidence failure must be paired with a decision or tripwire fallback before completion"
            self.log_event(
                "optional_evidence_collected" if result.ok else "optional_evidence_failed",
                details,
                slice_id=slice_["id"],
            )

    def inspect_po_status(self, run_id: str) -> dict[str, Any]:
        self.ensure_plan_orchestrator_product_shim()
        result = self.runner.run(["python", "-m", "scripts.keel_status_digest", "--run-id", run_id], cwd=self.root, execute_in_dry_run=True)
        if not result.ok:
            self.log_event("po_status_failed", {"run_id": run_id, "stderr": result.stderr})
            return {"terminal_state": "unknown", "run_id": run_id, "error": result.stderr}
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError:
            return {"terminal_state": "unknown", "run_id": run_id, "raw": result.stdout}

    def handle_po_status(self, slice_id: str, run_id: str, status: dict[str, Any]) -> str:
        terminal = str(status.get("terminal_state") or status.get("state") or "unknown")
        self.log_event("po_status", {"run_id": run_id, "terminal_state": terminal, "status": status}, slice_id=slice_id)

        if terminal == "passed":
            shipped = self.ship_slice(slice_id, run_id)
            if not shipped.ok:
                failure = self.record_failure(
                    slice_id,
                    "ship_failure",
                    "high",
                    "PO passed but AutoKeel could not create the ship branch.",
                    "Recorded ship failure; slice not marked complete.",
                    None,
                    run_id=run_id,
                )
                self.mark_slice_status(slice_id, "replan_required", run_id=run_id, failure_path=str(failure.relative_to(self.root)))
                return "ship_failed"

            reviews = self.ensure_review_artifacts(slice_id, run_id)
            if not reviews.ok:
                failure = self.record_failure(
                    slice_id,
                    "review_artifact_invalid",
                    "high",
                    "PO passed but required autonomous review artifacts were missing or invalid.",
                    "Rejected completion; reviewer artifacts must be generated and pass validation before slice acceptance.",
                    None,
                    run_id=run_id,
                )
                self.mark_slice_status(slice_id, "replan_required", run_id=run_id, failure_path=str(failure.relative_to(self.root)))
                return "review_failed"

            acceptance = self.verify_slice_acceptance(slice_id)
            if not acceptance.ok:
                failure = self.record_failure(
                    slice_id,
                    "agent_false_done",
                    "high",
                    "PO passed but slice acceptance verification failed.",
                    "Rejected completion; slice requires remediation.",
                    None,
                    run_id=run_id,
                )
                self.mark_slice_status(slice_id, "replan_required", run_id=run_id, failure_path=str(failure.relative_to(self.root)))
                return "acceptance_failed"

            self.mark_slice_status(
                slice_id,
                "complete",
                run_id=run_id,
                completed_at=now_iso(),
                ship_branch=f"ship/{slice_id.lower()}",
                ship_commit=self.runner.run(["git", "rev-parse", "HEAD"], cwd=self.root).stdout.strip(),
            )
            return "complete"

        if terminal == "blocked_external":
            state = self.load_state()
            active = state.get("active_run") or {}
            existing_request = active.get("evidence_request")
            if existing_request:
                self.log_event("blocked_external_existing_request", {"evidence_request": existing_request}, slice_id=slice_id)
                self.mark_slice_status(slice_id, "blocked_external", run_id=run_id, evidence_request=existing_request)
                return "blocked_external"

            evidence = self.create_external_evidence_request(slice_id, run_id, status)
            active["evidence_request"] = str(evidence.relative_to(self.root))
            state["active_run"] = active
            self.save_state(state)

            self.record_failure(
                slice_id,
                "blocked_external_missing_evidence",
                "medium",
                "PO requires local external evidence before the run can continue.",
                "Created local evidence request directory; did not fabricate evidence.",
                evidence,
                run_id=run_id,
            )
            self.mark_slice_status(slice_id, "blocked_external", run_id=run_id, evidence_request=str(evidence.relative_to(self.root)))
            return "blocked_external"

        if terminal == "awaiting_human_gate":
            failure = self.record_failure(
                slice_id,
                "manual_gate_leak",
                "high",
                "PO reached awaiting_human_gate in autonomous mode.",
                "Rejected terminal state; replan/recompile without manual gates.",
                None,
                run_id=run_id,
            )
            self.mark_slice_status(slice_id, "replan_required", run_id=run_id, failure_path=str(failure.relative_to(self.root)))
            return "manual_gate_leak"

        if terminal == "escalated":
            failure = self.record_failure(
                slice_id,
                "audit_failure",
                "high",
                "PO escalated the slice.",
                "Recorded escalation for diagnosis and bounded replan.",
                None,
                run_id=run_id,
            )
            self.mark_slice_status(slice_id, "replan_required", run_id=run_id, failure_path=str(failure.relative_to(self.root)))
            return "escalated"

        return "live"

    def create_external_evidence_request(self, slice_id: str, run_id: str, status: dict[str, Any]) -> Path:
        evidence_dir = self.root / "private" / "evidence" / slice_id / f"{slug_ts()}-{run_id}"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "README.md").write_text(
            "# External Evidence Request\n\n"
            f"Slice: {slice_id}\n\n"
            f"Run ID: {run_id}\n\n"
            "AutoKeel stopped here because PO requested evidence outside the repo.\n"
            "Place real local evidence files in this directory, redact secrets, then resume PO with "
            "`keel-run supervise resume --external-evidence-dir <this-dir>`.\n\n"
            "Status digest:\n\n"
            f"```json\n{json.dumps(redact(status), indent=2, sort_keys=True)}\n```\n",
            encoding="utf-8",
        )
        self.log_event("evidence_request_created", {"path": str(evidence_dir.relative_to(self.root))}, slice_id=slice_id)
        return evidence_dir

    def record_failure(self, slice_id: str, failure_class: str, severity: str, description: str, action_taken: str, evidence_path: Path | None, run_id: str | None = None) -> Path:
        failure_dir = self.root / "ops" / "autonomy" / "failures"
        failure_dir.mkdir(parents=True, exist_ok=True)
        failure_file = failure_dir / f"{slice_id}-{failure_class}-{slug_ts()}-{uuid.uuid4().hex[:8]}.md"
        evidence_text = str(evidence_path.relative_to(self.root)) if evidence_path and evidence_path.is_absolute() else str(evidence_path or "")
        failure_file.write_text(
            f"# {slice_id} {failure_class}\n\n"
            f"- Timestamp: {now_iso()}\n"
            f"- Severity: {severity}\n"
            f"- Run ID: {run_id or ''}\n"
            f"- Evidence: {evidence_text}\n\n"
            f"## Description\n\n{description}\n\n"
            f"## Action Taken\n\n{action_taken}\n",
            encoding="utf-8",
        )
        payload = {"ts": now_iso(), "slice": slice_id, "run_id": run_id, "failure_class": failure_class, "severity": severity, "description": description, "action_taken": action_taken, "evidence_path": evidence_text or str(failure_file.relative_to(self.root)), "open": True}
        append_jsonl(self.failure_path, redact(payload))
        self.log_event("failure_recorded", payload, slice_id=slice_id)
        return failure_file

    def ship_slice(self, slice_id: str, run_id: str) -> CommandResult:
        branch = f"ship/{slice_id.lower()}"
        run_branch = f"orchestrator/run/{run_id}"

        verify = self.runner.run(["git", "rev-parse", "--verify", run_branch], cwd=self.root)
        if not verify.ok:
            self.log_event(
                "slice_ship_failed",
                {"run_id": run_id, "run_branch": run_branch, "stderr": verify.stderr[-2000:]},
                slice_id=slice_id,
            )
            return CommandResult(["git", "rev-parse", "--verify", run_branch], 30, verify.stdout, verify.stderr)

        checkout = self.runner.run(["git", "checkout", "-B", branch, run_branch], cwd=self.root)
        ship_commit = ""
        if checkout.ok:
            head = self.runner.run(["git", "rev-parse", "HEAD"], cwd=self.root)
            ship_commit = head.stdout.strip() if head.ok else ""
        self.log_event(
            "slice_ship_branch_created" if checkout.ok else "slice_ship_failed",
            {
                "run_id": run_id,
                "run_branch": run_branch,
                "ship_branch": branch,
                "ship_commit": ship_commit,
                "exit_code": checkout.exit_code,
                "stdout": checkout.stdout[-2000:],
                "stderr": checkout.stderr[-2000:],
            },
            slice_id=slice_id,
        )
        return checkout

    def run_once(self, requested_slice: str | None = None, force_slice: bool = False) -> int:
        if not self.dry_run:
            return self._run_once_impl(requested_slice=requested_slice, force_slice=force_slice)

        snapshots = self.snapshot_dry_run_state()
        try:
            return self._run_once_impl(requested_slice=requested_slice, force_slice=force_slice)
        finally:
            self.restore_dry_run_state(snapshots)

    def _run_once_impl(self, requested_slice: str | None = None, force_slice: bool = False) -> int:
        self.log_heartbeat()

        verify = self.run_verify_v1()
        if verify.ok:
            self.log_event("v1_complete", {})
            return 0

        tripwires = self.evaluate_tripwires()
        if not tripwires.ok:
            try:
                tripwire_report = json.loads(tripwires.stdout)
            except json.JSONDecodeError:
                tripwire_report = {}
            if self.apply_tripwire_fallbacks(tripwire_report):
                tripwires = self.evaluate_tripwires()
                if tripwires.ok:
                    self.log_event("tripwire_fallbacks_applied", {})
                else:
                    self.log_event("tripwire_fallbacks_unresolved", {"stdout": tripwires.stdout[-4000:], "stderr": tripwires.stderr[-4000:]})
                    return 6
            else:
                self.log_event("tripwire_failure_unresolved", {"stdout": tripwires.stdout[-4000:], "stderr": tripwires.stderr[-4000:]})
                return 6

        slice_ = self.choose_next_slice(requested_slice, force=force_slice)
        if not slice_:
            self.log_event("no_actionable_slice", {})
            return 1

        self.ensure_slice_brief(slice_)
        self.ensure_lane_decision(slice_)
        if slice_.get("lane") == "compiler_external_evidence":
            self.run_optional_evidence(slice_)

        compiled = self.ensure_playbook(slice_)
        if not compiled.ok:
            self.log_event(
                "waiting_for_playbook_or_compile_inputs",
                {"exit_code": compiled.exit_code, "stderr": compiled.stderr[-2000:]},
                slice_id=slice_["id"],
            )
            if compiled.exit_code not in {20, 21, 22, 24}:
                failure = self.record_failure(
                    slice_["id"],
                    "compile_failure",
                    "high",
                    "Playbook compilation failed for an actionable slice.",
                    "Recorded compile failure and moved the slice through bounded retry/replan handling.",
                    self.root / slice_["playbook"],
                )
                self.mark_slice_status(slice_["id"], "replan_required", failure_path=str(failure.relative_to(self.root)))
            return compiled.exit_code or 2

        if self.dry_run and not (self.root / slice_["playbook"]).exists():
            self.log_event("dry_run_playbook_validation_skipped", {"playbook": slice_.get("playbook")}, slice_id=slice_["id"])
            return 0

        validation = self.validate_playbook(slice_)
        if not validation.ok:
            evidence_path = self.archive_playbook_for_replan(slice_) or (self.root / slice_["playbook"])
            self.record_failure(
                slice_["id"],
                "unsafe_write_root",
                "high",
                "Autonomous playbook validation failed.",
                "Rejected and archived the playbook before PO execution.",
                evidence_path,
            )
            self.mark_slice_status(slice_["id"], "replan_required")
            return 3

        if self.dry_run:
            self.log_event("dry_run_po_start_skipped", {"playbook": slice_.get("playbook")}, slice_id=slice_["id"])
            return 0

        run = self.start_or_resume_po(slice_)
        if not run.ok:
            self.record_failure(
                slice_["id"],
                "test_failure",
                "medium",
                "PO did not start or resume successfully.",
                "Recorded failure and left slice pending for diagnosis.",
                None,
            )
            return run.exit_code or 4

        state = self.load_state()
        run_id = (state.get("active_run") or {}).get("run_id") or self._extract_run_id(run.stdout)
        if not run_id:
            self.log_event("po_run_id_missing", {"stdout": run.stdout, "stderr": run.stderr}, slice_id=slice_["id"])
            return 5

        status = self.inspect_po_status(run_id)
        self.handle_po_status(slice_["id"], run_id, status)
        return 0

    def run_loop(self, max_loops: int | None = None, requested_slice: str | None = None, force_slice: bool = False) -> int:
        loops = 0
        while True:
            code = self.run_once(requested_slice=requested_slice, force_slice=force_slice)
            loops += 1
            if code == 0 and self.load_state().get("v1_complete"):
                return 0
            if max_loops is not None and loops >= max_loops:
                return code
            time.sleep(30)

    @staticmethod
    def _extract_run_id(text: str) -> str | None:
        if not text:
            return None
        try:
            payload = json.loads(text)
            for key in ("run_id", "id"):
                if isinstance(payload, dict) and payload.get(key):
                    candidate = str(payload[key])
                    if re.fullmatch(r"RUN_[A-Za-z0-9_.:-]+", candidate):
                        return candidate
        except json.JSONDecodeError:
            pass
        match = re.search(r"\b(RUN_[A-Za-z0-9_.:-]+)\b", text)
        if match:
            return match.group(1)
        return None


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the AutoKeel autonomous supervisor.")
    parser.add_argument("--root", default=".", help="Product repo root.")
    parser.add_argument("--once", action="store_true", help="Run one supervisor iteration.")
    parser.add_argument("--dry-run", action="store_true", help="Log intended commands without executing them.")
    parser.add_argument("--max-loops", type=int, default=None, help="Maximum loop count before exiting.")
    parser.add_argument("--slice", dest="slice_id", default=None, help="Force a specific slice id or slug.")
    parser.add_argument("--force", action="store_true", help="Allow --slice to bypass dependency checks.")
    parser.add_argument("--status", action="store_true", help="Print current autonomy state and exit.")
    parser.add_argument("--failures", action="store_true", help="Include failure ledger rows with --status.")
    parser.add_argument("--doctor", action="store_true", help="Run AutoKeel preflight checks and exit.")
    parser.add_argument("--strict", action="store_true", help="Use strict mode with --doctor.")
    parser.add_argument("--next-slice", action="store_true", help="Print the next actionable slice and exit.")
    parser.add_argument("--replay-events", action="store_true", help="Print event log rows and exit.")
    parser.add_argument("--unblock-evidence", nargs=2, metavar=("SLICE_ID", "EVIDENCE_DIR"), help="Mark a blocked slice evidence_ready with a local evidence dir.")
    parser.add_argument("--close-failure", nargs=2, metavar=("SLICE_ID", "FAILURE_CLASS"), help="Close matching open failures.")
    parser.add_argument("--closure-evidence", help="Repo-relative evidence path for --close-failure.")
    parser.add_argument("--closure-note", help="Closure note for --close-failure.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    root = Path(args.root).resolve()
    lock_path = root / "ops" / "autonomy" / ".autokeel.lock"

    with AutoKeelLock(lock_path):
        autokeel = AutoKeel(root=root, dry_run=args.dry_run)
        if args.doctor:
            cmd = ["python", "-m", "scripts.verify_autonomy_preflight", "--json"]
            if args.strict:
                cmd.extend(["--strict-tools", "--strict-clean"])
            result = autokeel.runner.run(cmd, cwd=root, execute_in_dry_run=True)
            print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="")
            return result.exit_code
        if args.next_slice:
            print(json.dumps(autokeel.choose_next_slice(args.slice_id, force=args.force), indent=2, sort_keys=True))
            return 0
        if args.replay_events:
            print(json.dumps(list(iter_jsonl(autokeel.events_path) or []), indent=2, sort_keys=True))
            return 0
        if args.unblock_evidence:
            slice_id, evidence_dir = args.unblock_evidence
            evidence_path = root / evidence_dir
            if not evidence_path.exists():
                raise AutoKeelError(f"evidence directory missing: {evidence_dir}")
            autokeel.mark_slice_status(slice_id, "evidence_ready", evidence_request=str(evidence_path.relative_to(root)))
            print(json.dumps({"status": "ok", "slice": slice_id, "evidence_request": str(evidence_path.relative_to(root))}, indent=2, sort_keys=True))
            return 0
        if args.close_failure:
            if not args.closure_evidence or not args.closure_note:
                raise AutoKeelError("--close-failure requires --closure-evidence and --closure-note")
            from scripts.close_failure import close_failure

            report = close_failure(root, args.close_failure[0], args.close_failure[1], args.closure_evidence, args.closure_note)
            print(json.dumps(report, indent=2, sort_keys=True))
            return 0 if report["status"] == "ok" else 1
        if args.status:
            payload = {"state": autokeel.load_state(), "slices": autokeel.load_slices()}
            if args.failures:
                payload["failures"] = list(iter_jsonl(autokeel.failure_path) or [])
            print(json.dumps(payload, indent=2, sort_keys=True))
            return 0
        if args.once:
            return autokeel.run_once(requested_slice=args.slice_id, force_slice=args.force)
        return autokeel.run_loop(max_loops=args.max_loops, requested_slice=args.slice_id, force_slice=args.force)


if __name__ == "__main__":
    raise SystemExit(main())

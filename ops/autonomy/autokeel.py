#!/usr/bin/env python3
"""Keel-native autonomous supervisor for the Health Data Hub build.

AutoKeel is intentionally a thin wrapper around Keel. It owns durable
autonomy state, policy enforcement, event/failure logging, and terminal-state
routing. Keel and plan-orchestrator remain the execution kernel.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import time
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable


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
            # A leftover lock file is indistinguishable from a crash artifact;
            # remove it on clean exit so its presence is a meaningful signal.
            try:
                self.path.unlink()
            except OSError:
                pass


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


def env_present(name: str) -> bool:
    return bool(str(os.environ.get(name, "")).strip())


# Decision-record statuses that describe the review TRANSPORT (subprocess,
# schema plumbing, workspace snapshot), not the reviewer's semantic verdict.
# These must never be budgeted or reported as a semantic review rejection.
TRANSPORT_DECISION_STATUSES = {
    "error",
    "failed",
    "interrupted",
    "malformed_output",
    "missing_cli",
    "read_only_violation",
    "timeout",
}

STATE_DIGEST_PATHS = (
    "ops/autonomy/slices.json",
    "ops/autonomy/events.jsonl",
    "ops/autonomy/failure_ledger.jsonl",
)


def load_local_env(root: Path) -> list[str]:
    """Load KEY=VALUE pairs from .env.local / .env into os.environ.

    Existing environment variables always win. Returns the names (never the
    values) of variables that were loaded, for diagnostics.
    """
    loaded: list[str] = []
    for name in (".env.local", ".env"):
        path = root / name
        if not path.exists():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if not key or key in os.environ:
                continue
            os.environ[key] = value
            loaded.append(key)
    return loaded


def compute_state_digest(root: Path) -> dict[str, Any]:
    digest: dict[str, Any] = {}
    for rel in STATE_DIGEST_PATHS:
        path = root / rel
        digest[rel] = file_sha256(path) if path.exists() else None
    return digest


def update_state_digest_sidecar(root: Path) -> None:
    payload = {
        "schema_version": "autokeel.state_digest.v1",
        "updated_at": now_iso(),
        "digests": compute_state_digest(root),
    }
    write_json_atomic(root / "ops" / "autonomy" / "state_digest.json", payload)


def slug_ts() -> str:
    return datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z")


def now_slug() -> str:
    return slug_ts()


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
    SWR_ACTIVE_RUN_STATUSES = {"running", "waiting_for_review"}
    SWR_ACTIVE_STAGE_STATUSES = {"submitted", "in_progress"}
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
        self.authorization_policy_path = self.autonomy_dir / "authorization_policy.yaml"
        self.slices_path = self.autonomy_dir / "slices.json"
        self.state_path = self.autonomy_dir / "autonomy_state.json"
        self.events_path = self.autonomy_dir / "events.jsonl"
        self.failure_path = self.autonomy_dir / "failure_ledger.jsonl"
        self.progress_path = self.autonomy_dir / "progress.md"
        self.policy = load_policy(self.policy_path)
        self.authorization_policy = load_policy(self.authorization_policy_path) if self.authorization_policy_path.exists() else {}
        # Provider credentials live only in gitignored .env.local; AutoKeel and
        # every keel-swr subprocess it spawns inherit them via os.environ.
        self.loaded_env_names = load_local_env(self.root)
        self.runner = CommandRunner(self.root, self.policy, dry_run=dry_run)
        self.dry_run = dry_run
        statuses = self.policy.get("slice_statuses", {})
        self.actionable_statuses = set(statuses.get("actionable", self.ACTIONABLE_STATUSES))
        self.blocked_statuses = set(statuses.get("blocked", self.BLOCKED_STATUSES))
        self._swr_materializations: dict[str, dict[str, Any]] = {}

    def repo_relative(self, path: Path | str) -> str:
        candidate = Path(path)
        if not candidate.is_absolute():
            candidate = self.root / candidate
        return str(candidate.resolve().relative_to(self.root))

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

    def last_event_id_from_log(self) -> int:
        last_event_id = 0
        for event in iter_jsonl(self.events_path):
            try:
                event_id = int(event.get("event_id") or 0)
            except (TypeError, ValueError):
                continue
            last_event_id = max(last_event_id, event_id)
        return last_event_id

    def load_slices(self) -> list[dict[str, Any]]:
        payload = read_json(self.slices_path, [])
        if not isinstance(payload, list):
            raise AutoKeelError("slices.json must contain a list")
        return payload

    def save_slices(self, slices: list[dict[str, Any]]) -> None:
        write_json_atomic(self.slices_path, slices)
        self.sync_state_digest()

    def sync_state_digest(self) -> None:
        if self.dry_run:
            return
        update_state_digest_sidecar(self.root)

    def verify_state_digest(self) -> CommandResult:
        """Fail closed when control-plane state was edited out-of-band.

        The sidecar is rewritten by every sanctioned writer (save_slices,
        log_event, record_failure, scripts/record_intervention.py). A mismatch
        means slices.json / events.jsonl / failure_ledger.jsonl changed with no
        sanctioned writer running — the exact shape of the unrecorded 2026-06-01
        S05 reset.
        """
        if not bool(self.policy.get("loop", {}).get("enforce_state_digest", True)):
            return CommandResult([], 0, "state digest enforcement disabled by policy", "")
        sidecar = read_json(self.autonomy_dir / "state_digest.json", None)
        if not isinstance(sidecar, dict) or not isinstance(sidecar.get("digests"), dict):
            # First run after upgrade: establish the baseline instead of failing.
            self.sync_state_digest()
            return CommandResult([], 0, "state digest baseline established", "")
        recorded = sidecar["digests"]
        current = compute_state_digest(self.root)
        mismatched = sorted(rel for rel in current if recorded.get(rel) != current.get(rel))
        if not mismatched:
            return CommandResult([], 0, "state digest verified", "")
        return CommandResult(
            [],
            39,
            "",
            (
                "control-plane state changed out-of-band since the last sanctioned write: "
                + ", ".join(mismatched)
                + "; ratify via 'python scripts/record_intervention.py ratify ...' before the next tick"
            ),
        )

    def log_event(self, event_type: str, details: dict[str, Any] | None = None, slice_id: str | None = None) -> dict[str, Any]:
        state = self.load_state()
        event_id = max(int(state.get("last_event_id") or 0), self.last_event_id_from_log()) + 1
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
        self.sync_state_digest()
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

            if status in {"complete", "waiting_for_playbook", "pending"}:
                slice_["retry_count"] = 0
                for stale_key in ("failure_path", "stopped_run_id", "reason"):
                    slice_.pop(stale_key, None)

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
            run_id = extra.get("run_id")
            if run_id:
                history = state.setdefault("run_history", [])
                if not any(item.get("slice") == slice_id and item.get("run_id") == run_id for item in history if isinstance(item, dict)):
                    history.append(
                        {
                            "slice": slice_id,
                            "run_id": run_id,
                            "completed_at": extra.get("completed_at") or now_iso(),
                            "ship_branch": extra.get("ship_branch"),
                            "ship_commit": extra.get("ship_commit"),
                        }
                    )
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

    def po_supervisor_wait_seconds(self) -> int:
        return int(self.policy.get("loop", {}).get("po_supervisor_wait_seconds", 5))

    def po_max_auto_resume_attempts(self) -> int:
        return int(self.policy.get("loop", {}).get("po_max_auto_resume_attempts", 0))

    def po_repaired_escalation_resume_attempts(self) -> int:
        return int(self.policy.get("loop", {}).get("po_repaired_escalation_resume_attempts", 1))

    def open_failures(self, slice_id: str, failure_class: str | None = None, run_id: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in iter_jsonl(self.failure_path):
            if row.get("slice") != slice_id:
                continue
            if failure_class and row.get("failure_class") != failure_class:
                continue
            if run_id and row.get("run_id") != run_id:
                continue
            if row.get("open", True):
                rows.append(row)
        return rows

    def failure_closure_evidence_valid(self, row: dict[str, Any]) -> bool:
        evidence = str(row.get("closure_evidence") or "")
        note = str(row.get("closure_note") or "")
        if not evidence or not note:
            return False

        evidence_path = Path(evidence)
        if evidence_path.is_absolute():
            candidate = evidence_path.resolve()
        else:
            candidate = (self.root / evidence).resolve()
        try:
            candidate.relative_to(self.root.resolve())
        except ValueError:
            return False
        return candidate.exists()

    def failure_counts_against_budget(self, row: dict[str, Any]) -> bool:
        if row.get("open", True):
            return True
        # Safety invariant: a closed ledger row stops consuming retry budget
        # only when local closure evidence still exists and explains the repair.
        return not self.failure_closure_evidence_valid(row)

    def failure_counts_against_closed_repair_budget(self, row: dict[str, Any]) -> bool:
        if row.get("open", True) or not self.failure_closure_evidence_valid(row):
            return False
        return self.repair_budget_scope(row) != "meta_guardrail"

    def failure_description_key(self, row: dict[str, Any]) -> str:
        raw = str(row.get("description") or row.get("failure_description") or row.get("reason") or "")
        return " ".join(raw.lower().split())[:240]

    def failure_scope_text(self, row: dict[str, Any]) -> str:
        raw = " ".join(
            str(row.get(key) or "")
            for key in (
                "description",
                "failure_description",
                "reason",
                "action_taken",
                "evidence_path",
                "closure_note",
                "closure_evidence",
            )
        )
        return " ".join(raw.lower().split())

    def root_cause_budget_key(self, row: dict[str, Any]) -> str:
        repair_policy = self.policy.get("repair_budget", {})
        pattern = str(repair_policy.get("generic_root_cause_pattern") or r"^S[0-9]{2}-[A-Z_]+$")
        root = str(row.get("root_cause_id") or "")
        failure_class = str(row.get("failure_class") or "unclassified")
        slice_id = str(row.get("slice") or "UNKNOWN")
        generated_root = f"{slice_id}-{failure_class}".upper().replace("_", "-")
        if root and (re.match(pattern, root) or root == generated_root):
            return f"{row.get('failure_class')}:{self.failure_description_key(row)}"
        return root or f"{row.get('failure_class')}:{self.failure_description_key(row)}"

    def repair_budget_scope(self, row: dict[str, Any]) -> str:
        # Explicitly typed scope on the row always wins over free-text
        # inference; new failures are recorded with this field so budget
        # classification stops depending on closure-note prose.
        explicit_scope = str(row.get("repair_scope") or "")
        if explicit_scope in {"meta_guardrail", "autokeel_control_plane", "swr_review_lane", "product_or_playbook"}:
            return explicit_scope

        failure_class = str(row.get("failure_class") or "")
        origin = str(row.get("failure_origin") or "")
        description = self.failure_description_key(row)
        scope_text = self.failure_scope_text(row)
        repair_policy = self.policy.get("repair_budget", {})
        meta_classes = set(repair_policy.get("meta_guardrail_failure_classes", []))

        if failure_class in meta_classes:
            return "meta_guardrail"
        if failure_class in {"swr_review_transport_failure", "swr_kernel_unpinned"}:
            return "autokeel_control_plane"
        if origin == "autokeel_wrapper":
            return "autokeel_control_plane"
        if "readiness gate" in description or "pre-launch readiness" in description:
            return "autokeel_control_plane"
        if "exit code" in description or "generic compile failure" in description:
            return "autokeel_control_plane"
        if "swr supervisor review" in description and any(
            marker in scope_text
            for marker in (
                "review-decision schema",
                "schema validation",
                "malformed_output",
                "malformed reviewer",
                "stale malformed",
                "invalid review-history",
                "cycle ids were normalized",
                "schema adapter",
                "schema-adapter",
                "review-decision canonicalization",
                "transport failure",
                "reviewer transport",
            )
        ):
            # Safety invariant: malformed review-decision records are
            # supervisor/control-plane defects. Valid semantic reviewer
            # rejections still fall through to the SWR review-lane budget.
            return "autokeel_control_plane"
        if "swr supervisor review" in description or "review bundle" in description or "review history" in description:
            return "swr_review_lane"
        if failure_class in {"compile_failure", "test_failure", "agent_false_done", "model_gate_failed"}:
            return "product_or_playbook"
        return "product_or_playbook"

    def evidence_closed_failures(self, slice_id: str, failure_class: str | None = None, run_id: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in iter_jsonl(self.failure_path):
            if row.get("slice") != slice_id:
                continue
            if failure_class and row.get("failure_class") != failure_class:
                continue
            if run_id and row.get("run_id") != run_id:
                continue
            if row.get("open", True):
                continue
            if self.failure_closure_evidence_valid(row):
                rows.append(row)
        return rows

    def high_or_critical_open_failures(self, slice_id: str | None = None) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for row in iter_jsonl(self.failure_path):
            if slice_id is not None and row.get("slice") != slice_id:
                continue
            if not row.get("open", True):
                continue
            if row.get("severity") in {"high", "critical"}:
                rows.append(row)
        return rows

    def assert_no_open_high_failures(self, slice_id: str, phase: str) -> CommandResult:
        failures = self.high_or_critical_open_failures(slice_id)
        if not failures:
            return CommandResult([], 0, "no open high/critical failures", "")
        return CommandResult(
            [],
            36,
            "",
            f"open high/critical failures block {phase}: "
            + ", ".join(str(item.get("failure_class") or "unknown") for item in failures),
        )

    def swr_review_lane_budget_release_path(self, slice_id: str) -> Path:
        return self.root / "docs" / "evidence" / f"{slice_id.lower()}-swr-review-lane-budget-release.json"

    def swr_review_repair_plan_key(self, slice_: dict[str, Any]) -> str:
        import hashlib
        import json

        repair = slice_.get("swr_review_repair") if isinstance(slice_.get("swr_review_repair"), dict) else {}
        material = {
            "slice": slice_.get("id"),
            "run_id": repair.get("run_id"),
            "run_manifest": repair.get("run_manifest"),
            "repair_action": repair.get("repair_action"),
            "repair_stage_id": repair.get("repair_stage_id"),
            "created_at": repair.get("created_at"),
        }
        return hashlib.sha256(json.dumps(material, sort_keys=True).encode("utf-8")).hexdigest()

    def swr_review_lane_budget_release_valid(self, slice_: dict[str, Any]) -> CommandResult:
        from datetime import datetime

        slice_id = str(slice_.get("id") or "")
        path = self.swr_review_lane_budget_release_path(slice_id)
        if not path.exists():
            return CommandResult([], 37, "", f"SWR review-lane budget release missing: {path.relative_to(self.root)}")

        payload = read_json(path, {})
        if not isinstance(payload, dict):
            return CommandResult([], 37, "", "SWR review-lane budget release is not a JSON object")

        if payload.get("schema_version") != "autokeel.swr_review_lane_budget_release.v1":
            return CommandResult([], 37, "", "SWR review-lane budget release has unexpected schema_version")
        if payload.get("slice") != slice_id:
            return CommandResult([], 37, "", "SWR review-lane budget release slice mismatch")
        if payload.get("verdict") != "pass" or payload.get("allow_one_next_repair_tick") is not True:
            return CommandResult([], 37, "", "SWR review-lane budget release is not passing")
        if payload.get("consumed_at"):
            return CommandResult([], 37, "", "SWR review-lane budget release is already consumed")
        if payload.get("release_key") != self.swr_review_repair_plan_key(slice_):
            return CommandResult([], 37, "", "SWR review-lane budget release does not match current repair plan")

        expires_raw = str(payload.get("expires_at") or "")
        try:
            expires_at = datetime.fromisoformat(expires_raw)
        except ValueError:
            return CommandResult([], 37, "", "SWR review-lane budget release expires_at is invalid")
        if datetime.now().astimezone() > expires_at:
            return CommandResult([], 37, "", "SWR review-lane budget release is expired")

        repair = slice_.get("swr_review_repair")
        if not isinstance(repair, dict) or repair.get("status") != "planned":
            return CommandResult([], 37, "", "SWR review-lane budget release requires a planned swr_review_repair")
        if repair.get("repair_action") != "rerun_review_lane":
            return CommandResult([], 37, "", "SWR review-lane budget release only allows rerun_review_lane")
        manifest_rel = str(repair.get("run_manifest") or "")
        if not manifest_rel or not (self.root / manifest_rel).exists():
            return CommandResult([], 37, "", "SWR review-lane budget release repair manifest is missing")

        return CommandResult([], 0, f"SWR review-lane budget release valid: {path.relative_to(self.root)}", "")

    def consume_swr_review_lane_budget_release(self, slice_: dict[str, Any]) -> None:
        """Consume the release before the next real repair tick.

        This intentionally consumes even if the subsequent repair fails; a failed
        next tick must produce new evidence rather than silently reusing the same
        release.
        """
        if self.dry_run:
            return

        path = self.swr_review_lane_budget_release_path(str(slice_.get("id") or ""))
        payload = read_json(path, {})
        if not isinstance(payload, dict):
            return
        payload["consumed_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
        write_json_atomic(path, payload)
        # Preserve the consumption audit trail: a consumed release is archived
        # under a timestamped name so a re-minted release can never overwrite it.
        archive = path.with_name(f"{path.stem}-consumed-{now_slug()}{path.suffix}")
        shutil.copy2(path, archive)

        slices = self.load_slices()
        for item in slices:
            if item.get("id") != slice_.get("id"):
                continue
            repair = item.get("swr_review_repair")
            if isinstance(repair, dict):
                repair["budget_release_consumed_at"] = payload["consumed_at"]
                repair["budget_release_evidence"] = str(path.relative_to(self.root))
            item["updated_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            break
        write_json_atomic(self.root / "ops/autonomy/slices.json", slices)
        self.log_event(
            "swr_review_lane_budget_release_consumed",
            {
                "release_evidence": str(path.relative_to(self.root)),
                "release_key": payload.get("release_key"),
            },
            slice_id=str(slice_.get("id") or ""),
        )

    def stability_checkpoint_error(self, slice_id: str) -> str | None:
        """Validate the control-plane stability checkpoint by content, not existence.

        A checkpoint is meaningful only if it passed and is fresh; a stale file
        from a prior incident must not waive a safety budget indefinitely.
        """
        repair_policy = self.policy.get("repair_budget", {})
        max_age_hours = float(repair_policy.get("stability_checkpoint_max_age_hours", 72))
        checkpoint_path = self.root / "docs" / "evidence" / f"{slice_id.lower()}-autokeel-stability-checkpoint.json"
        if not checkpoint_path.exists():
            return f"stability checkpoint missing: {checkpoint_path.relative_to(self.root)}"
        payload = read_json(checkpoint_path, None)
        if not isinstance(payload, dict):
            return "stability checkpoint is not a JSON object"
        if payload.get("status") != "ok":
            return f"stability checkpoint status is not ok: {payload.get('status')}"
        if str(payload.get("slice") or "") not in {"", slice_id}:
            return "stability checkpoint slice mismatch"
        created_raw = str(payload.get("created_at") or "")
        try:
            created_at = datetime.fromisoformat(created_raw)
        except ValueError:
            return "stability checkpoint created_at is invalid"
        age_hours = (datetime.now().astimezone() - created_at.astimezone()).total_seconds() / 3600
        if age_hours > max_age_hours:
            return (
                f"stability checkpoint is stale ({age_hours:.1f}h > {max_age_hours:.1f}h); "
                "re-mint via scripts/verify_autokeel_stability_checkpoint.py"
            )
        return None

    def failure_budget_exceeded(self, slice_: dict[str, Any]) -> CommandResult:
        loop = self.policy.get("loop", {})
        max_total = int(loop.get("max_total_failures_per_slice", 8))
        max_same = int(loop.get("max_same_failure_class_per_slice", 2))
        max_closed_total = int(loop.get("max_closed_repairs_total_per_slice", 5))
        max_closed_same_root = int(loop.get("max_closed_repairs_same_root_cause_per_slice", 2))
        max_retargets = int(loop.get("max_run_retargets_per_slice", 2))
        all_rows = [row for row in iter_jsonl(self.failure_path) if row.get("slice") == slice_["id"]]
        rows = [row for row in all_rows if self.failure_counts_against_budget(row)]
        if len(rows) > max_total:
            return CommandResult([], 37, "", f"failure budget exceeded for {slice_['id']}: {len(rows)} > {max_total}")
        counts: dict[str, int] = {}
        for row in rows:
            failure_class = str(row.get("failure_class") or "unclassified")
            counts[failure_class] = counts.get(failure_class, 0) + 1
        repeated = sorted(f"{key}={value}" for key, value in counts.items() if value > max_same)
        if repeated:
            return CommandResult([], 37, "", f"same failure class budget exceeded for {slice_['id']}: {', '.join(repeated)}")

        repair_policy = self.policy.get("repair_budget", {})
        max_product = int(repair_policy.get("max_closed_product_or_playbook_repairs_per_slice", max_closed_total))
        max_swr_review = int(repair_policy.get("max_closed_swr_review_lane_repairs_per_slice", 3))
        max_control = int(repair_policy.get("max_closed_control_plane_repairs_before_stability_checkpoint", 3))

        evidence_closed_rows = [
            row for row in all_rows
            if not row.get("open", True)
            and self.failure_closure_evidence_valid(row)
            and self.failure_counts_against_closed_repair_budget(row)
        ]

        by_scope: dict[str, list[dict[str, Any]]] = {}
        for row in evidence_closed_rows:
            by_scope.setdefault(self.repair_budget_scope(row), []).append(row)

        if len(by_scope.get("product_or_playbook", [])) > max_product:
            return CommandResult([], 37, "", f"closed product/playbook repair budget exceeded for {slice_['id']}: {len(by_scope['product_or_playbook'])} > {max_product}")

        swr_review_rows = by_scope.get("swr_review_lane", [])
        if len(swr_review_rows) > max_swr_review:
            release = self.swr_review_lane_budget_release_valid(slice_)
            if release.ok:
                self.log_event(
                    "swr_review_lane_budget_release_accepted",
                    {
                        "closed_swr_review_lane_repairs": len(swr_review_rows),
                        "max_closed_swr_review_lane_repairs": max_swr_review,
                        "release_stdout": release.stdout,
                    },
                    slice_id=slice_["id"],
                )
                self.consume_swr_review_lane_budget_release(slice_)
                return CommandResult([], 0, "SWR review-lane budget release accepted for one repair tick", "")

            return CommandResult(
                [],
                37,
                "",
                (
                    f"closed SWR review-lane repair budget exceeded for {slice_['id']}: "
                    f"{len(swr_review_rows)} > {max_swr_review}; {release.stderr}"
                ),
            )

        if len(by_scope.get("autokeel_control_plane", [])) > max_control:
            if repair_policy.get("require_stability_checkpoint_after_control_plane_budget", True):
                checkpoint_error = self.stability_checkpoint_error(slice_["id"])
                if checkpoint_error:
                    return CommandResult(
                        [],
                        37,
                        "",
                        (
                            f"control-plane repair checkpoint required for {slice_['id']}: "
                            f"{len(by_scope['autokeel_control_plane'])} > {max_control}; {checkpoint_error}"
                        ),
                    )

        root_counts: dict[str, int] = {}
        for row in evidence_closed_rows:
            if self.repair_budget_scope(row) != "product_or_playbook":
                continue
            root_cause = self.root_cause_budget_key(row)
            root_counts[root_cause] = root_counts.get(root_cause, 0) + 1
        repeated_roots = sorted(f"{key}={value}" for key, value in root_counts.items() if value > max_closed_same_root)
        if repeated_roots:
            return CommandResult([], 37, "", f"root-cause repair budget exceeded for {slice_['id']}: {', '.join(repeated_roots)}")
        retarget_count = len(self.retarget_evidence_paths(slice_["id"]))
        if bool(loop.get("stop_on_retarget_budget_exceeded", False)) and retarget_count > max_retargets:
            return CommandResult([], 37, "", f"run retarget budget exceeded for {slice_['id']}: {retarget_count} > {max_retargets}")
        resolved = len(all_rows) - len(rows)
        return CommandResult([], 0, f"failure budgets ok; unresolved={len(rows)} resolved={resolved}", "")

    def failure_budget_root_key(self, row: dict[str, Any]) -> str:
        failure_class = str(row.get("failure_class") or "unclassified")
        slice_id = str(row.get("slice") or "UNKNOWN")
        root_cause = str(row.get("root_cause_id") or failure_class)
        generic_root = f"{slice_id}-{failure_class}".upper().replace("_", "-")
        if root_cause and root_cause != generic_root:
            return root_cause
        # Safety invariant: generated root_cause_id values such as
        # S05-AUDIT-FAILURE identify only the failure class, not the actual
        # diagnosis. Do not let unrelated, evidence-closed repairs consume the
        # same-root budget merely because they share a generic class label.
        description = str(row.get("description") or failure_class).lower()
        signature = re.sub(r"[^a-z0-9]+", "-", description).strip("-") or failure_class
        return f"{generic_root}:{signature[:96]}"

    def retarget_evidence_paths(self, slice_id: str) -> list[Path]:
        evidence_dir = self.root / "docs/evidence"
        if not evidence_dir.exists():
            return []
        return sorted(evidence_dir.glob(f"{slice_id}-run-retarget*.json"))

    def verify_run_retarget_evidence(self, slice_id: str) -> CommandResult:
        paths = self.retarget_evidence_paths(slice_id)
        if not paths:
            return CommandResult([], 0, "no run retarget evidence for slice", "")

        failures: list[str] = []
        for path in paths:
            rel = str(path.relative_to(self.root))
            result = self.runner.run(
                ["python", "scripts/verify_run_retarget_evidence.py", rel, "--json"],
                cwd=self.root,
                execute_in_dry_run=True,
            )
            self.log_event(
                "run_retarget_evidence_validated" if result.ok else "run_retarget_evidence_rejected",
                {"evidence": rel, "exit_code": result.exit_code, "stdout": result.stdout[-2000:], "stderr": result.stderr[-2000:]},
                slice_id=slice_id,
            )
            if not result.ok:
                failures.append(rel)
        if failures:
            return CommandResult([], 37, "", "run retarget evidence validation failed: " + ", ".join(failures))
        return CommandResult([], 0, f"run retarget evidence ok; count={len(paths)}", "")

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

    def git_path_exists_at_head(self, rel_path: str) -> bool:
        path = Path(rel_path)
        if path.is_absolute() or ".." in path.parts:
            return False
        probe = subprocess.run(
            ["git", "rev-parse", "--is-inside-work-tree"],
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        if probe.returncode != 0:
            return (self.root / rel_path).exists()
        result = subprocess.run(
            ["git", "cat-file", "-e", f"HEAD:{rel_path}"],
            cwd=self.root,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        return result.returncode == 0

    def assert_autoplan_tracked_at_head(self, slice_: dict[str, Any]) -> CommandResult:
        autoplan = str(slice_.get("autoplan") or "")
        if not autoplan:
            return CommandResult([], 41, "", "slice missing autoplan path")
        if not self.git_path_exists_at_head(autoplan):
            return CommandResult([], 41, "", f"autoplan must be tracked at HEAD before compile: {autoplan}")
        return CommandResult(["git", "cat-file", "-e", f"HEAD:{autoplan}"], 0, "autoplan tracked at HEAD", "")

    def validate_autoplan_text(self, slice_: dict[str, Any], text: str) -> list[str]:
        lowered = text.lower()
        errors: list[str] = []
        slice_id = str(slice_.get("id", "")).lower()
        wrapper_markers = (
            "write permission was denied",
            "write wasn't approved",
            "write was not approved",
            "here is the autoplan",
            "i attempted to save",
            "approve the write",
            "let me know if",
        )
        if any(marker in lowered for marker in wrapper_markers) or "```" in text:
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

    def lane_decision_payload(self, slice_: dict[str, Any]) -> dict[str, Any]:
        decision_rel = slice_.get("lane_decision")
        if not decision_rel:
            return {}
        path = self.root / str(decision_rel)
        payload = read_json(path, {})
        return payload if isinstance(payload, dict) else {}

    def swr_required(self, slice_: dict[str, Any]) -> bool:
        if slice_.get("lane") != "swr_preferred":
            return False
        lane_mode = self.policy.get("lanes", {}).get("swr_preferred", "compile_with_decision")
        if lane_mode == "use_swr":
            return True
        return self.lane_decision_payload(slice_).get("decision") == "use_swr"

    def swr_evidence_path(self, slice_: dict[str, Any]) -> Path:
        rel = slice_.get("swr_evidence") or f"docs/evidence/{slice_.get('slug', str(slice_['id']).lower())}-swr-playbook-evidence.json"
        return self.root / str(rel)

    def swr_evidence_matches_playbook(self, slice_: dict[str, Any], playbook: Path) -> bool:
        evidence = self.swr_evidence_path(slice_)
        if not playbook.exists() or not evidence.exists():
            return False
        payload = read_json(evidence, {})
        return (
            isinstance(payload, dict)
            and payload.get("status") == "ok"
            and payload.get("tool") == "keel-swr"
            and payload.get("playbook") == str(playbook.relative_to(self.root))
            and payload.get("playbook_sha256") == file_sha256(playbook)
        )

    def swr_required_env(self) -> list[str]:
        values = self.policy.get("swr", {}).get("required_env", [])
        return [str(item) for item in values] if isinstance(values, list) else []

    def strict_swr_provider_preflight(self, slice_: dict[str, Any]) -> CommandResult:
        if not self.swr_required(slice_) or not bool(self.policy.get("swr", {}).get("provider_auth_preflight", False)):
            return CommandResult([], 0, "SWR provider preflight not required", "")
        if self.dry_run:
            return CommandResult([], 0, '{"dry_run": true, "secret_values_logged": false}', "")
        missing = [name for name in self.swr_required_env() if not env_present(name)]
        report = {
            "status": "ok" if not missing else str(self.policy.get("swr", {}).get("provider_auth_failure_status", "blocked_external")),
            "provider": "openai",
            "slice": slice_["id"],
            "missing_env": missing,
            "secret_values_logged": False,
        }
        if not missing:
            return CommandResult([], 0, json.dumps(report, sort_keys=True), "")
        return CommandResult([], 26, json.dumps(report, sort_keys=True), "missing required SWR provider environment")

    def write_swr_provider_preflight_evidence(self, slice_: dict[str, Any], preflight: CommandResult) -> Path:
        evidence = self.root / "docs" / "evidence" / f"{str(slice_['slug'])}-swr-provider-preflight-{slug_ts()}.json"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        payload = self.parse_json_stdout(preflight, {})
        if not isinstance(payload, dict):
            payload = {
                "status": "blocked_external",
                "provider": "openai",
                "slice": slice_["id"],
                "missing_env": self.swr_required_env(),
                "secret_values_logged": False,
            }
        write_json_atomic(evidence, payload)
        self.log_event(
            "swr_provider_preflight_evidence_recorded",
            {"evidence": str(evidence.relative_to(self.root)), "status": payload.get("status")},
            slice_id=slice_["id"],
        )
        return evidence

    def swr_lease_path(self, slice_id: str) -> Path:
        lease_root = self.policy.get("swr", {}).get("lease_root", ".local/autokeel/swr/leases")
        return self.root / str(lease_root) / f"{slice_id}.json"

    def read_swr_lease(self, slice_id: str) -> dict[str, Any] | None:
        payload = read_json(self.swr_lease_path(slice_id), None)
        return payload if isinstance(payload, dict) else None

    def write_swr_lease(self, slice_: dict[str, Any], manifest_path: Path, payload: dict[str, Any], observed_at: str | None = None) -> Path:
        manifest_path = manifest_path.resolve()
        stage = self.current_swr_stage(payload) if isinstance(payload, dict) else {}
        interval = int(self.policy.get("swr", {}).get("monitor_min_interval_seconds", 300))
        checked = datetime.fromisoformat(observed_at) if observed_at else datetime.now().astimezone()
        next_check = datetime.fromtimestamp(checked.timestamp() + interval, tz=checked.tzinfo)
        lease = {
            "slice": slice_["id"],
            "run_id": payload.get("run_id"),
            "run_dir": payload.get("run_dir") or self.repo_relative(manifest_path.parent),
            "run_manifest": self.repo_relative(manifest_path),
            "stage_id": payload.get("current_stage_id"),
            "response_id": stage.get("response_id") if isinstance(stage, dict) else None,
            "status": payload.get("status"),
            "created_at": (self.read_swr_lease(slice_["id"]) or {}).get("created_at") or now_iso(),
            "last_checked_at": observed_at or now_iso(),
            "next_check_not_before": next_check.isoformat(timespec="seconds"),
            "lease_owner_pid": None,
        }
        path = self.swr_lease_path(slice_["id"])
        write_json_atomic(path, lease)
        return path

    def clear_swr_lease(self, slice_id: str, reason: str) -> None:
        path = self.swr_lease_path(slice_id)
        if path.exists():
            path.unlink()
            self.log_event("swr_lease_cleared", {"lease": str(path.relative_to(self.root)), "reason": reason}, slice_id=slice_id)

    def active_swr_manifest_from_lease(self, slice_: dict[str, Any]) -> tuple[Path | None, CommandResult | None]:
        lease = self.read_swr_lease(slice_["id"])
        if not lease:
            return None, None
        manifest_rel = lease.get("run_manifest")
        if not isinstance(manifest_rel, str) or not manifest_rel:
            failure = self.record_failure(
                slice_["id"],
                "state_divergence",
                "high",
                "SWR lease exists without a run_manifest.",
                "Blocked duplicate SWR launch until the lease is repaired or cleared with evidence.",
                self.swr_lease_path(slice_["id"]),
            )
            self.mark_slice_status(slice_["id"], "blocked_compile_inputs", failure_path=str(failure.relative_to(self.root)), reason="SWR lease missing manifest")
            return None, CommandResult([], 35, "", "SWR active lease missing run_manifest")
        manifest = self.root / manifest_rel
        if not manifest.exists():
            failure = self.record_failure(
                slice_["id"],
                "state_divergence",
                "high",
                "SWR lease points at a missing run_manifest.",
                "Blocked duplicate SWR launch; check the remote response or recover the local manifest before relaunch.",
                self.swr_lease_path(slice_["id"]),
            )
            self.mark_slice_status(slice_["id"], "blocked_compile_inputs", failure_path=str(failure.relative_to(self.root)), reason="SWR lease manifest missing")
            return None, CommandResult([], 35, "", f"SWR active lease manifest missing: {manifest_rel}")
        payload = read_json(manifest, {})
        if isinstance(payload, dict) and self.swr_run_is_active(payload):
            return manifest, None
        self.clear_swr_lease(slice_["id"], "lease manifest is no longer active")
        return None, None

    def swr_run_is_active(self, payload: dict[str, Any]) -> bool:
        if payload.get("autokeel_quarantined"):
            return False
        if str(payload.get("status", "")) in self.SWR_ACTIVE_RUN_STATUSES:
            return True
        stages = payload.get("stages")
        if not isinstance(stages, list):
            return False
        return any(
            isinstance(stage, dict) and str(stage.get("status", "")) in self.SWR_ACTIVE_STAGE_STATUSES
            for stage in stages
        )

    def latest_swr_manifest_for_slice(self, slice_: dict[str, Any]) -> Path | None:
        output_root = self.root / str(self.policy.get("swr", {}).get("output_root", ".local/autokeel/swr/runs"))
        if not output_root.exists():
            return None
        prefix = f"autokeel-{str(slice_['id']).lower()}-"
        candidates: list[tuple[float, Path]] = []
        for manifest in output_root.glob("*/run_manifest.json"):
            payload = read_json(manifest, {})
            if not isinstance(payload, dict):
                continue
            if not str(payload.get("run_name", "")).startswith(prefix):
                continue
            if not self.swr_run_is_active(payload):
                continue
            candidates.append((manifest.stat().st_mtime, manifest))
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def active_swr_manifest_from_state(self, slice_: dict[str, Any]) -> Path | None:
        lease_manifest, lease_error = self.active_swr_manifest_from_lease(slice_)
        if lease_error is not None:
            return None
        if lease_manifest is not None:
            return lease_manifest
        state = self.load_state()
        active = state.get("active_swr_run")
        if not isinstance(active, dict) or active.get("slice") != slice_["id"]:
            return None
        manifest_rel = active.get("run_manifest")
        if not isinstance(manifest_rel, str) or not manifest_rel:
            return None
        manifest = self.root / manifest_rel
        payload = read_json(manifest, {})
        if isinstance(payload, dict) and self.swr_run_is_active(payload):
            return manifest
        return None

    def swr_execution_phase(self, slice_: dict[str, Any]) -> str:
        """Return the canonical execution phase for a high-risk SWR slice.

        The result is intentionally small and closed. All launch/readiness/resume
        decisions must consult this before applying pre-launch gates.
        """
        if slice_.get("lane") != "swr_preferred" or slice_.get("risk") != "high":
            return "not_swr"

        repair = slice_.get("swr_review_repair")
        if isinstance(repair, dict):
            status = str(repair.get("status") or "")
            manifest_rel = str(repair.get("run_manifest") or "")
            if status == "planned" and manifest_rel and (self.root / manifest_rel).exists():
                return "swr_review_repair"
            if status == "planned":
                return "swr_repair_malformed"

        state = self.load_state()
        active = state.get("active_swr_run")
        if isinstance(active, dict) and active.get("slice") == slice_.get("id"):
            manifest_rel = str(active.get("run_manifest") or "")
            if manifest_rel and (self.root / manifest_rel).exists():
                manifest = read_json(self.root / manifest_rel, {})
                if manifest.get("autokeel_quarantined") or manifest.get("status") == "quarantined":
                    return "swr_quarantined"
                return "active_swr_resume"

        manifest_rel = str(slice_.get("swr_run_manifest") or "")
        if manifest_rel and (self.root / manifest_rel).exists():
            manifest = read_json(self.root / manifest_rel, {})
            if manifest.get("autokeel_quarantined") or manifest.get("status") == "quarantined":
                return "swr_quarantined"

        return "fresh_launch"

    def should_run_slice_readiness(self, slice_: dict[str, Any]) -> bool:
        phase = self.swr_execution_phase(slice_)
        return phase in {"not_swr", "fresh_launch"}

    def record_active_swr_run(
        self,
        slice_: dict[str, Any],
        manifest_path: Path,
        reason: str,
        observed_result: CommandResult | None = None,
    ) -> None:
        manifest_path = manifest_path.resolve()
        payload = read_json(manifest_path, {})
        if not isinstance(payload, dict):
            payload = {}
        current_stage_id = payload.get("current_stage_id")
        current_stage: dict[str, Any] = {}
        stages = payload.get("stages")
        if isinstance(stages, list):
            for stage in stages:
                if isinstance(stage, dict) and stage.get("stage_id") == current_stage_id:
                    current_stage = stage
                    break
        state = self.load_state()
        previous_active = state.get("active_swr_run")
        manifest_rel = self.repo_relative(manifest_path)
        run_id = payload.get("run_id")
        previous_matches = (
            isinstance(previous_active, dict)
            and previous_active.get("slice") == slice_["id"]
            and (
                previous_active.get("run_manifest") == manifest_rel
                or (run_id is not None and previous_active.get("run_id") == run_id)
            )
        )
        previous_active = previous_active if previous_matches else {}
        recorded_at = now_iso()
        last_remote_check_at = previous_active.get("last_remote_check_at")
        last_remote_check_exit_code = previous_active.get("last_remote_check_exit_code")
        last_remote_check_stderr = previous_active.get("last_remote_check_stderr")
        if observed_result is not None:
            last_remote_check_at = recorded_at
            last_remote_check_exit_code = observed_result.exit_code
            last_remote_check_stderr = observed_result.stderr[-1000:]
        elif not last_remote_check_at:
            last_remote_check_at = recorded_at
        self.write_swr_lease(slice_, manifest_path, payload, observed_at=last_remote_check_at or recorded_at)
        state["active_swr_run"] = {
            "slice": slice_["id"],
            "run_id": run_id,
            "run_name": payload.get("run_name"),
            "run_dir": payload.get("run_dir"),
            "run_manifest": manifest_rel,
            "workflow_id": payload.get("workflow_id"),
            "status": payload.get("status"),
            "current_stage_id": current_stage_id,
            "current_stage_status": current_stage.get("status"),
            "response_id": current_stage.get("response_id"),
            "supervisor_session_id": previous_active.get("supervisor_session_id"),
            "last_remote_check_at": last_remote_check_at,
            "last_remote_check_exit_code": last_remote_check_exit_code,
            "last_remote_check_stderr": last_remote_check_stderr,
            "recorded_at": recorded_at,
        }
        self.save_state(state)
        self.log_event(
            "swr_run_active",
            {
                "run_id": payload.get("run_id"),
                "run_manifest": manifest_rel,
                "lease": self.repo_relative(self.swr_lease_path(slice_["id"])),
                "status": payload.get("status"),
                "current_stage_id": current_stage_id,
                "current_stage_status": current_stage.get("status"),
                "response_id": current_stage.get("response_id"),
                "reason": reason,
            },
            slice_id=slice_["id"],
        )
        self.mark_slice_status(
            slice_["id"],
            "waiting_for_playbook",
            swr_run_manifest=manifest_rel,
            swr_run_id=payload.get("run_id"),
            reason=reason,
        )

    def swr_wait_timeout_in_progress(self, result: CommandResult) -> bool:
        text = f"{result.stdout}\n{result.stderr}".lower()
        return "did not reach a terminal state within" in text and "last_status=" in text

    def archive_swr_evidence_for_replan(self, slice_: dict[str, Any], archive_dir: Path | None = None) -> Path | None:
        evidence = self.swr_evidence_path(slice_)
        if not evidence.exists():
            return None
        target_dir = archive_dir or self.root / "ops" / "autonomy" / "failures" / "archived_playbooks"
        target_dir.mkdir(parents=True, exist_ok=True)
        archive = target_dir / f"{slice_['id']}-{slug_ts()}-{evidence.name}"
        os.replace(evidence, archive)
        self.log_event(
            "swr_evidence_archived_for_replan",
            {"from": str(evidence.relative_to(self.root)), "to": str(archive.relative_to(self.root))},
            slice_id=slice_["id"],
        )
        return archive

    def archive_swr_playbook_and_evidence(self, slice_: dict[str, Any], reason: str) -> tuple[Path | None, Path | None]:
        playbook_archive = self.archive_playbook_for_replan(slice_)
        archive_dir = playbook_archive.parent if playbook_archive is not None else None
        evidence_archive = self.archive_swr_evidence_for_replan(slice_, archive_dir=archive_dir)
        self.log_event(
            "swr_artifacts_archived_for_replan",
            {
                "reason": reason,
                "playbook_archive": str(playbook_archive.relative_to(self.root)) if playbook_archive else None,
                "evidence_archive": str(evidence_archive.relative_to(self.root)) if evidence_archive else None,
            },
            slice_id=slice_["id"],
        )
        return playbook_archive, evidence_archive

    def archive_swr_playbook_and_evidence_for_validation_repair(
        self, slice_: dict[str, Any], reason: str
    ) -> tuple[Path | None, Path | None]:
        archive_dir = self.root / "ops" / "autonomy" / "failures" / "archived_playbooks"
        archive_dir.mkdir(parents=True, exist_ok=True)

        playbook_archive: Path | None = None
        playbook = self.root / slice_["playbook"]
        if playbook.exists():
            playbook_archive = archive_dir / f"{slice_['id']}-{slug_ts()}-{playbook.name}"
            os.replace(playbook, playbook_archive)
            self.log_event(
                "swr_playbook_archived_for_validation_repair",
                {"from": str(playbook.relative_to(self.root)), "to": str(playbook_archive.relative_to(self.root))},
                slice_id=slice_["id"],
            )

        evidence_archive: Path | None = None
        evidence = self.swr_evidence_path(slice_)
        if evidence.exists():
            evidence_archive = archive_dir / f"{slice_['id']}-{slug_ts()}-{evidence.name}"
            os.replace(evidence, evidence_archive)
            self.log_event(
                "swr_evidence_archived_for_validation_repair",
                {"from": str(evidence.relative_to(self.root)), "to": str(evidence_archive.relative_to(self.root))},
                slice_id=slice_["id"],
            )

        self.log_event(
            "swr_artifacts_archived_for_validation_repair",
            {
                "reason": reason,
                "playbook_archive": str(playbook_archive.relative_to(self.root)) if playbook_archive else None,
                "evidence_archive": str(evidence_archive.relative_to(self.root)) if evidence_archive else None,
            },
            slice_id=slice_["id"],
        )
        return playbook_archive, evidence_archive

    def extract_swr_manifest_path(self, result: CommandResult) -> Path | None:
        for stream in (result.stdout, result.stderr):
            for line in reversed(stream.splitlines()):
                value = line.strip()
                if not value or "run_manifest.json" not in value:
                    continue
                candidate = Path(value)
                if not candidate.is_absolute():
                    candidate = self.root / candidate
                if candidate.exists() and candidate.name == "run_manifest.json":
                    return candidate
        return None

    def extract_response_output_text(self, response_json: dict[str, Any]) -> str:
        texts: list[str] = []
        output = response_json.get("output")
        if not isinstance(output, list):
            return ""
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for part in content:
                if isinstance(part, dict) and part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    text = part["text"].strip()
                    if text:
                        texts.append(text)
        return "\n\n".join(texts).strip()

    def validation_errors_from_result(self, validation: CommandResult) -> list[str]:
        payload = self.parse_json_stdout(validation, {})
        if isinstance(payload, dict):
            errors = payload.get("errors")
            if isinstance(errors, list):
                return [str(error) for error in errors]
            if isinstance(errors, str) and errors:
                return [errors]
        lines = [line.strip() for line in f"{validation.stdout}\n{validation.stderr}".splitlines()]
        return [line for line in lines if line][-20:]

    def swr_materialization_source_for_slice(self, slice_: dict[str, Any]) -> dict[str, str]:
        materialization = self._swr_materializations.get(slice_["id"], {})
        source = materialization.get("swr_source")
        return source if isinstance(source, dict) else {}

    def swr_manifest_from_materialization_source(self, source: dict[str, str]) -> Path | None:
        manifest_rel = source.get("manifest") or source.get("run_manifest")
        if not isinstance(manifest_rel, str) or not manifest_rel:
            return None
        manifest_path = Path(manifest_rel)
        if not manifest_path.is_absolute():
            manifest_path = self.root / manifest_path
        try:
            manifest_path.resolve().relative_to(self.root)
        except ValueError:
            return None
        if manifest_path.exists() and manifest_path.name == "run_manifest.json":
            return manifest_path
        return None

    def swr_stage_summary(self, payload: dict[str, Any], stage_id: str) -> dict[str, Any]:
        for stage in payload.get("stages", []) if isinstance(payload.get("stages"), list) else []:
            if isinstance(stage, dict) and stage.get("stage_id") == stage_id:
                return stage
        return {}

    def swr_stage_index(self, payload: dict[str, Any], stage_id: str) -> int | None:
        stage_order = payload.get("stage_order")
        if isinstance(stage_order, list) and stage_id in stage_order:
            return stage_order.index(stage_id)
        for index, stage in enumerate(payload.get("stages", []) if isinstance(payload.get("stages"), list) else []):
            if isinstance(stage, dict) and stage.get("stage_id") == stage_id:
                return index
        return None

    def swr_stage_response_artifact_errors(self, payload: dict[str, Any], stage_id: str) -> list[str]:
        stage = self.swr_stage_summary(payload, stage_id)
        if not stage:
            return [f"{stage_id}: stage summary is missing"]
        errors: list[str] = []
        checkpoint: dict[str, Any] = {}
        checkpoint_path = self.repo_artifact_path(str(stage.get("checkpoint_path") or ""))
        if checkpoint_path is not None and checkpoint_path.exists():
            checkpoint_payload = read_json(checkpoint_path, {})
            checkpoint = checkpoint_payload if isinstance(checkpoint_payload, dict) else {}
        response_completed = stage.get("response_status") == "completed" or checkpoint.get("terminal") is True
        if not response_completed:
            errors.append(f"{stage_id}: response is not recorded as completed")
        stage_dir = str(stage.get("stage_dir") or "")
        for label, rel_key, final_key, sha_key, extension in (
            ("response markdown", "response_markdown_path", "response_final_markdown_path", "response_markdown_sha256", "md"),
            ("response JSON", "response_json_path", "response_final_json_path", "response_json_sha256", "json"),
        ):
            rel = stage.get(rel_key) or stage.get(final_key)
            if not rel and stage_dir:
                rel = f"{stage_dir}/response.final.{extension}"
            if not isinstance(rel, str) or not rel:
                errors.append(f"{stage_id}: {label} path is missing")
                continue
            path = self.repo_artifact_path(rel)
            if path is None or not path.exists():
                errors.append(f"{stage_id}: {label} artifact is missing or outside repo")
                continue
            expected = stage.get(sha_key)
            if not isinstance(expected, str) or not expected:
                errors.append(f"{stage_id}: {label} sha256 is missing")
            elif expected != file_sha256(path):
                errors.append(f"{stage_id}: {label} sha256 does not match manifest")
        return errors

    def swr_stage_response_artifacts_valid(self, payload: dict[str, Any], stage_id: str) -> bool:
        return not self.swr_stage_response_artifact_errors(payload, stage_id)

    def swr_stage_response_text(self, manifest_path: Path, stage_id: str) -> str:
        payload = read_json(manifest_path, {})
        if not isinstance(payload, dict):
            return ""
        stage = self.swr_stage_summary(payload, stage_id)
        if not stage:
            return ""
        markdown_rel = (
            stage.get("response_markdown_path")
            or stage.get("response_final_markdown_path")
            or (f"{stage.get('stage_dir')}/response.final.md" if stage.get("stage_dir") else "")
        )
        if isinstance(markdown_rel, str) and markdown_rel:
            markdown_path = self.repo_artifact_path(markdown_rel)
            if markdown_path is not None and markdown_path.exists():
                return markdown_path.read_text(encoding="utf-8")
        json_rel = stage.get("response_json_path") or stage.get("response_final_json_path")
        if isinstance(json_rel, str) and json_rel:
            json_path = self.repo_artifact_path(json_rel)
            if json_path is not None and json_path.exists():
                response_json = read_json(json_path, {})
                if isinstance(response_json, dict):
                    return self.extract_response_output_text(response_json)
        return ""

    def swr_review_bundle_for_stage(self, slice_: dict[str, Any], manifest_path: Path, source_stage_id: str) -> str | None:
        payload = read_json(manifest_path, {})
        if not isinstance(payload, dict):
            return None
        stage = self.swr_stage_summary(payload, source_stage_id)
        candidates: list[str] = []
        bundle_rel = stage.get("review_bundle_path") if isinstance(stage, dict) else None
        if isinstance(bundle_rel, str) and bundle_rel:
            candidates.append(bundle_rel)
        run_id = str(payload.get("run_id") or "")
        if run_id:
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{slice_['id']}-{run_id}-{source_stage_id}").strip("-")
            candidates.append(f".local/autokeel/swr/review_lane/{safe}/{source_stage_id}.review_bundle.json")
        for candidate in candidates:
            path = self.repo_artifact_path(candidate)
            if path is not None and path.exists():
                return str(path.relative_to(self.root))
        return None

    def contract_terms_from_validation_errors(self, slice_: dict[str, Any], playbook_errors: list[str]) -> list[str]:
        terms = {"required_verification_commands"}
        if str(slice_.get("risk", "")).lower() == "high" or self.swr_required(slice_):
            terms.add("autonomous_gate_review")
        for error in playbook_errors:
            for term in re.findall(r"\b[a-z][a-z0-9]+(?:_[a-z0-9]+)+\b", str(error).lower()):
                if term in {"required_verification_commands", "autonomous_gate_review"} or term.endswith("_review"):
                    terms.add(term)
        return sorted(terms)

    @staticmethod
    def contract_terms_missing(text: str, terms: list[str]) -> list[str]:
        lowered = text.lower()
        return [term for term in terms if term.lower() not in lowered]

    def diagnose_swr_validation_failure(self, slice_: dict[str, Any], manifest_path: Path, playbook_errors: list[str]) -> dict[str, Any]:
        terms = self.contract_terms_from_validation_errors(slice_, playbook_errors)
        stage4 = self.swr_stage_response_text(manifest_path, "gate_and_contract_review")
        stage5 = self.swr_stage_response_text(manifest_path, "final_markdown_playbook")
        missing_in_stage4 = self.contract_terms_missing(stage4, terms) if stage4 else []
        missing_in_stage5 = self.contract_terms_missing(stage5, terms) if stage5 else []

        if missing_in_stage4:
            return {
                "repair_stage_id": "gate_and_contract_review",
                "source_review_stage_id": "execution_row_draft",
                "stage4_missing_terms": missing_in_stage4,
                "stage5_missing_terms": missing_in_stage5,
                "status": "repairable",
                "rationale": (
                    "Stage 4 is missing validator-required contract terms; rerun Stage 4 "
                    "and then review it before any Stage 5 rerun."
                ),
            }
        if missing_in_stage5:
            return {
                "repair_stage_id": "final_markdown_playbook",
                "source_review_stage_id": "gate_and_contract_review",
                "stage4_missing_terms": missing_in_stage4,
                "stage5_missing_terms": missing_in_stage5,
                "status": "repairable",
                "rationale": "Stage 4 has the required contract terms but Stage 5 dropped them; rerun Stage 5 only.",
            }
        return {
            "repair_stage_id": None,
            "source_review_stage_id": None,
            "stage4_missing_terms": missing_in_stage4,
            "stage5_missing_terms": missing_in_stage5,
            "status": "blocked_pending_diagnosis",
            "rationale": "Validation failure did not map to a deterministic Stage 4 or Stage 5 contract diff.",
        }

    def plan_swr_validation_repair(
        self,
        slice_: dict[str, Any],
        validation: CommandResult,
        source: dict[str, str],
        playbook_archive: Path | None,
        evidence_archive: Path | None,
    ) -> dict[str, Any]:
        plan: dict[str, Any] = {
            "created_at": now_iso(),
            "status": "planned",
            "reason": "SWR-generated playbook failed autonomous validation before PO.",
            "validation_errors": self.validation_errors_from_result(validation),
            "validation_exit_code": validation.exit_code,
            "rejected_playbook_archive": str(playbook_archive.relative_to(self.root)) if playbook_archive else None,
            "rejected_evidence_archive": str(evidence_archive.relative_to(self.root)) if evidence_archive else None,
            "swr_source": source,
        }

        manifest_path = self.swr_manifest_from_materialization_source(source)
        if manifest_path is None:
            plan.update(
                {
                    "repair_stage_id": None,
                    "repair_action": "blocked_pending_source_manifest",
                    "rationale": "No source SWR run manifest was recorded for the rejected playbook.",
                }
            )
            return plan

        payload = read_json(manifest_path, {})
        if not isinstance(payload, dict):
            plan.update(
                {
                    "run_manifest": str(manifest_path.relative_to(self.root)),
                    "repair_stage_id": None,
                    "repair_action": "blocked_pending_valid_manifest",
                    "rationale": "The source SWR run manifest is not valid JSON object data.",
                }
            )
            return plan

        diagnosis = self.diagnose_swr_validation_failure(slice_, manifest_path, plan["validation_errors"])
        repair_stage_id = diagnosis.get("repair_stage_id")
        source_review_stage_id = diagnosis.get("source_review_stage_id")
        rationale = str(diagnosis.get("rationale") or "")

        source_review_bundle = (
            self.swr_review_bundle_for_stage(slice_, manifest_path, source_review_stage_id)
            if source_review_stage_id is not None
            else None
        )
        if repair_stage_id is not None and source_review_bundle is None:
            repair_action = "blocked_pending_review_bundle"
        elif repair_stage_id is not None:
            repair_action = "rerun_single_stage"
        else:
            repair_action = "blocked_pending_diagnosis"

        plan.update(
            {
                "repair_action": repair_action,
                "repair_stage_id": repair_stage_id,
                "source_review_stage_id": source_review_stage_id,
                "source_review_bundle": source_review_bundle,
                "run_id": payload.get("run_id"),
                "run_dir": payload.get("run_dir") or str(manifest_path.parent.relative_to(self.root)),
                "run_manifest": str(manifest_path.relative_to(self.root)),
                "stage4_missing_terms": diagnosis.get("stage4_missing_terms", []),
                "stage5_missing_terms": diagnosis.get("stage5_missing_terms", []),
                "diagnosis_status": diagnosis.get("status"),
                "rationale": rationale,
            }
        )
        return plan

    def write_swr_validation_failure_evidence(
        self,
        slice_: dict[str, Any],
        validation: CommandResult,
        repair_plan: dict[str, Any],
    ) -> Path:
        evidence = self.root / "docs" / "evidence" / f"{slice_['slug']}-swr-validation-repair-{slug_ts()}.md"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        validation_command = " ".join(shlex.quote(part) for part in validation.argv)
        text = f"""# {slice_['id']} SWR Validation Repair Plan

Status: blocked_compile_inputs

## Root Cause

The SWR-generated playbook failed `scripts/validate_playbook_autonomous.py`
before plan-orchestrator execution. AutoKeel rejected the playbook and planned
a stage-specific SWR repair instead of marking the slice `replan_required`.

## Validation Command

```bash
{validation_command}
```

Exit code: {validation.exit_code}

## Validation Errors

```json
{json.dumps(repair_plan.get("validation_errors", []), indent=2, sort_keys=True)}
```

## Repair Plan

```json
{json.dumps(repair_plan, indent=2, sort_keys=True)}
```

## Guardrail

AutoKeel must not start a fresh full SWR workflow for this failure. A future
policy-authorized continuation must satisfy `ops/autonomy/authorization_policy.yaml`
and use the recorded `run_dir` and `repair_stage_id` with
`keel-swr run --run-dir ... --stage ...`.
"""
        evidence.write_text(text, encoding="utf-8")
        self.log_event(
            "swr_validation_repair_evidence_recorded",
            {"evidence": str(evidence.relative_to(self.root)), "repair_stage_id": repair_plan.get("repair_stage_id")},
            slice_id=slice_["id"],
        )
        return evidence

    def clear_active_swr_run(self, slice_id: str, reason: str) -> None:
        state = self.load_state()
        active = state.get("active_swr_run")
        if not isinstance(active, dict) or active.get("slice") != slice_id:
            self.clear_swr_lease(slice_id, reason)
            return
        state["active_swr_run"] = None
        self.save_state(state)
        self.clear_swr_lease(slice_id, reason)
        self.log_event("active_swr_run_cleared", {"reason": reason}, slice_id=slice_id)

    def quarantine_swr_manifest(self, slice_id: str, manifest_path: Path, reason: str) -> None:
        payload = read_json(manifest_path, {})
        if not isinstance(payload, dict):
            return
        payload["autokeel_quarantined"] = True
        payload["quarantined_at"] = now_iso()
        payload["quarantined_reason"] = reason
        payload["status"] = "quarantined"
        write_json_atomic(manifest_path, payload)
        self.log_event(
            "swr_manifest_quarantined",
            {"run_manifest": str(manifest_path.relative_to(self.root)), "reason": reason},
            slice_id=slice_id,
        )

    def quarantine_related_swr_manifest(self, slice_: dict[str, Any], evidence_path: Path, reason: str) -> None:
        candidates: list[Path] = []
        if evidence_path.name == "run_manifest.json" and evidence_path.exists():
            candidates.append(evidence_path)
        state = self.load_state()
        active = state.get("active_swr_run")
        manifest_rel = active.get("run_manifest") if isinstance(active, dict) and active.get("slice") == slice_["id"] else None
        if isinstance(manifest_rel, str) and manifest_rel:
            candidates.append(self.root / manifest_rel)
        seen: set[Path] = set()
        for candidate in candidates:
            try:
                resolved = candidate.resolve()
                resolved.relative_to(self.root.resolve())
            except ValueError:
                continue
            if resolved in seen or not resolved.exists():
                continue
            seen.add(resolved)
            self.quarantine_swr_manifest(slice_["id"], resolved, reason)

    def clear_swr_validation_repair(self, slice_id: str) -> None:
        slices = self.load_slices()
        changed = False
        for item in slices:
            if item.get("id") == slice_id and "swr_validation_repair" in item:
                item.pop("swr_validation_repair", None)
                changed = True
                break
        if changed:
            self.save_slices(slices)
            self.log_event("swr_validation_repair_cleared", {}, slice_id=slice_id)

    def clear_swr_review_repair(self, slice_id: str) -> None:
        slices = self.load_slices()
        changed = False
        for item in slices:
            if item.get("id") == slice_id and "swr_review_repair" in item:
                item.pop("swr_review_repair", None)
                changed = True
                break
        if changed:
            self.save_slices(slices)
            self.log_event("swr_review_repair_cleared", {}, slice_id=slice_id)

    def swr_review_repair_authorized_by_policy(self, slice_: dict[str, Any], repair_plan: dict[str, Any]) -> tuple[bool, str]:
        policy = self.authorization_policy.get("swr_review_history_repair", {})
        if not isinstance(policy, dict) or not bool(policy.get("auto_authorized", False)):
            return False, "SWR review-history repair is not auto-authorized"
        manifest_rel = repair_plan.get("run_manifest")
        if bool(policy.get("require_existing_run_manifest", True)):
            if not isinstance(manifest_rel, str) or not manifest_rel or not (self.root / manifest_rel).exists():
                return False, "required SWR run manifest is missing"
        stage_id = str(repair_plan.get("repair_stage_id") or "")
        allowed = policy.get("require_repair_stage_id")
        if isinstance(allowed, list) and allowed and stage_id not in {str(item) for item in allowed}:
            return False, f"repair_stage_id is not policy-allowed: {stage_id}"
        if (
            repair_plan.get("repair_action") == "rerun_single_stage"
            and bool(policy.get("require_existing_review_bundle_for_stage_rerun", True))
            and repair_plan.get("source_review_stage_id") is not None
        ):
            bundle_rel = repair_plan.get("source_review_bundle")
            if not isinstance(bundle_rel, str) or not bundle_rel or not (self.root / bundle_rel).exists():
                return False, "required source SWR review bundle is missing"
        max_total = int(policy.get("max_repairs_per_slice", 4))
        max_stage = int(policy.get("max_repairs_per_stage", 2))
        # Safety invariant: failed repair attempts consume authorization budget
        # too — otherwise a non-converging repair retries unboundedly. Attempts
        # are scoped to the CURRENT plan (events at/after its created_at): each
        # plan gets a bounded number of attempts, and the number of plans is
        # itself bounded by the failure-ledger budgets.
        repair_events = {
            "swr_review_repair_lane_passed",
            "swr_review_repair_lane_failed",
            "swr_review_repair_stage_passed",
            "swr_review_repair_stage_failed",
        }
        plan_created_raw = str(repair_plan.get("created_at") or "")
        try:
            plan_created = datetime.fromisoformat(plan_created_raw)
        except ValueError:
            plan_created = None

        def belongs_to_current_plan(event: dict[str, Any]) -> bool:
            if plan_created is None:
                return True
            try:
                event_ts = datetime.fromisoformat(str(event.get("ts") or ""))
            except ValueError:
                return True
            return event_ts.astimezone() >= plan_created.astimezone()

        repairs = [
            event
            for event in iter_jsonl(self.events_path)
            if event.get("slice") == slice_["id"]
            and event.get("event") in repair_events
            and belongs_to_current_plan(event)
        ]
        if len(repairs) >= max_total:
            return False, "SWR review repair attempt count (including failed attempts) exceeds policy for this plan"
        same_stage = [
            event
            for event in repairs
            if isinstance(event.get("details"), dict) and event["details"].get("repair_stage_id") == stage_id
        ]
        if len(same_stage) >= max_stage:
            return False, "same-stage SWR review repair attempt count (including failed attempts) exceeds policy for this plan"
        return True, "policy-authorized same-run SWR review repair"

    def reset_swr_manifest_for_review_repair(self, manifest_path: Path, stage_id: str) -> None:
        payload = read_json(manifest_path, {})
        if not isinstance(payload, dict):
            raise AutoKeelError(f"invalid SWR manifest: {manifest_path}")
        stages = payload.get("stages")
        if not isinstance(stages, list):
            raise AutoKeelError(f"SWR manifest has no stages: {manifest_path}")
        target_index = self.swr_stage_index(payload, stage_id)
        if target_index is None:
            raise AutoKeelError(f"SWR stage not found in manifest: {stage_id}")
        stale_downstream_keys = {
            "approved_from_status",
            "checkpoint_path",
            "input_manifest_json_path",
            "input_manifest_markdown_path",
            "response_id",
            "response_status",
            "response_latest_json_path",
            "response_markdown_path",
            "response_markdown_sha256",
            "response_json_path",
            "response_json_sha256",
            "review_approved",
            "review_bundle_path",
            "sidecar_response_json_path",
            "sidecar_response_json_sha256",
            "sidecar_response_markdown_path",
            "sidecar_response_markdown_sha256",
            "structured_output_path",
            "structured_output_sha256",
            "token_preflight_path",
            "token_preflight_error_path",
        }
        for index, stage in enumerate(stages):
            if not isinstance(stage, dict):
                continue
            if index < target_index:
                continue
            if index == target_index:
                stage.pop("approved_from_status", None)
                stage.pop("review_approved", None)
                stage.pop("review_bundle_path", None)
                stage["status"] = "waiting_for_review"
                continue
            for key in stale_downstream_keys:
                stage.pop(key, None)
            stage["status"] = "prepared"
        payload.pop("autokeel_quarantined", None)
        payload.pop("autokeel_quarantine_reason", None)
        payload.pop("quarantined_at", None)
        payload.pop("quarantined_reason", None)
        payload["status"] = "waiting_for_review"
        payload["current_stage_id"] = stage_id
        write_json_atomic(manifest_path, payload)
        self.log_event(
            "swr_review_repair_manifest_reset",
            {"run_manifest": str(manifest_path.resolve().relative_to(self.root)), "stage_id": stage_id},
        )

    def handle_swr_playbook_validation_failure(self, slice_: dict[str, Any], validation: CommandResult) -> int:
        source = self.swr_materialization_source_for_slice(slice_)
        playbook_archive, evidence_archive = self.archive_swr_playbook_and_evidence_for_validation_repair(
            slice_,
            "SWR playbook validation failed; preserving the source run for stage-specific repair.",
        )
        repair_plan = self.plan_swr_validation_repair(slice_, validation, source, playbook_archive, evidence_archive)
        evidence = self.write_swr_validation_failure_evidence(slice_, validation, repair_plan)
        failure = self.record_failure(
            slice_["id"],
            "compile_failure",
            "high",
            "SWR-generated playbook failed autonomous validation before PO.",
            "Rejected the playbook, preserved the source SWR run manifest, and blocked with a minimal stage-repair plan instead of starting a fresh full SWR run.",
            evidence,
        )
        self.clear_active_swr_run(slice_["id"], "SWR validation failed and requires stage-specific repair")
        self.mark_slice_status(
            slice_["id"],
            "blocked_compile_inputs",
            failure_path=str(failure.relative_to(self.root)),
            reason="SWR playbook validation failed; minimal stage repair required",
            swr_validation_repair=repair_plan,
        )
        self.log_event("swr_validation_repair_planned", repair_plan, slice_id=slice_["id"])
        return 3

    def materialize_swr_playbook_from_manifest(self, slice_: dict[str, Any], manifest_path: Path) -> dict[str, str] | None:
        payload = read_json(manifest_path, {})
        if not isinstance(payload, dict) or payload.get("status") != "completed":
            return None
        stages = payload.get("stages")
        if not isinstance(stages, list):
            return None
        final_stage = next(
            (
                stage
                for stage in stages
                if isinstance(stage, dict)
                and stage.get("stage_id") == "final_markdown_playbook"
                and stage.get("status") == "completed"
            ),
            None,
        )
        if not isinstance(final_stage, dict):
            return None

        response_rel = final_stage.get("response_json_path")
        if not isinstance(response_rel, str) or not response_rel:
            return None
        response_path = Path(response_rel)
        if not response_path.is_absolute():
            response_path = self.root / response_path
        if not response_path.exists():
            return None

        response_json = read_json(response_path, {})
        if not isinstance(response_json, dict):
            return None
        playbook_text = self.extract_response_output_text(response_json)
        if not playbook_text:
            return None

        playbook = self.root / slice_["playbook"]
        playbook.parent.mkdir(parents=True, exist_ok=True)
        playbook.write_text(playbook_text.rstrip() + "\n", encoding="utf-8")
        self.log_event(
            "swr_playbook_materialized",
            {
                "manifest": str(manifest_path.relative_to(self.root)),
                "response_json": str(response_path.relative_to(self.root)),
                "playbook": str(playbook.relative_to(self.root)),
                "playbook_sha256": file_sha256(playbook),
            },
            slice_id=slice_["id"],
        )
        source = {
            "manifest": str(manifest_path.relative_to(self.root)),
            "run_id": str(payload.get("run_id") or ""),
            "run_dir": str(payload.get("run_dir") or manifest_path.parent.relative_to(self.root)),
            "stage_id": "final_markdown_playbook",
            "response_json": str(response_path.relative_to(self.root)),
        }
        response_markdown = final_stage.get("response_markdown_path") or final_stage.get("response_final_markdown_path")
        if isinstance(response_markdown, str) and response_markdown:
            source["response_markdown"] = response_markdown
        return source

    def record_swr_materialization(self, slice_: dict[str, Any], command: list[str], source: dict[str, str] | None = None) -> None:
        self._swr_materializations[slice_["id"]] = {
            "command": list(command),
            "swr_source": source or {},
        }
        self.clear_active_swr_run(slice_["id"], "SWR playbook materialized")

    def write_swr_playbook_evidence(self, slice_: dict[str, Any], validation: CommandResult) -> None:
        playbook = self.root / slice_["playbook"]
        if not playbook.exists():
            return
        materialization = self._swr_materializations.get(slice_["id"], {})
        evidence = self.swr_evidence_path(slice_)
        evidence.parent.mkdir(parents=True, exist_ok=True)
        validation_command = validation.argv or [
            "python",
            "-m",
            "scripts.validate_playbook_autonomous",
            str(playbook),
            "--risk",
            str(slice_.get("risk", "")),
            "--json",
        ]
        write_json_atomic(
            evidence,
            {
                "status": "ok",
                "tool": "keel-swr",
                "slice": slice_["id"],
                "command": materialization.get("command", []),
                "playbook": str(playbook.relative_to(self.root)),
                "playbook_sha256": file_sha256(playbook),
                "swr_source": materialization.get("swr_source", {}),
                "validation": {
                    "command": " ".join(shlex.quote(part) for part in validation_command),
                    "exit_code": validation.exit_code,
                    "stdout_tail": validation.stdout[-2000:],
                    "stderr_tail": validation.stderr[-2000:],
                },
                "recorded_at": now_iso(),
            },
        )
        self.log_event(
            "swr_playbook_evidence_recorded",
            {
                "evidence": str(evidence.relative_to(self.root)),
                "playbook": str(playbook.relative_to(self.root)),
                "playbook_sha256": file_sha256(playbook),
            },
            slice_id=slice_["id"],
        )

    def swr_provider_auth_failure(self, result: CommandResult) -> bool:
        text = f"{result.stdout}\n{result.stderr}".lower()
        return (
            "openai_api_key is not set" in text
            or ("openai_api_key" in text and "not found in .env" in text)
            or ("api key" in text and any(marker in text for marker in ("not set", "missing", "not found")))
        )

    def write_swr_provider_auth_evidence(self, slice_: dict[str, Any], command: list[str], result: CommandResult) -> Path:
        evidence = self.root / "docs" / "evidence" / f"{str(slice_['slug'])}-swr-provider-auth-failure-{slug_ts()}.json"
        evidence.parent.mkdir(parents=True, exist_ok=True)
        write_json_atomic(
            evidence,
            {
                "status": "blocked_external",
                "tool": "keel-swr",
                "slice": slice_["id"],
                "failure_class": "provider_auth_failure",
                "root_cause": "SWR playbook generation requires real OpenAI API credentials, but OPENAI_API_KEY was not available to the AutoKeel process and no repo-local .env was present.",
                "command": command,
                "exit_code": result.exit_code,
                "stdout_tail": result.stdout[-2000:],
                "stderr_tail": result.stderr[-2000:],
                "environment_probe": {
                    "OPENAI_API_KEY_set_in_process_env": env_present("OPENAI_API_KEY"),
                    "env_file_exists": (self.root / ".env").exists(),
                },
                "next_action": "Provide real local OpenAI API credentials via the process environment or an ignored repo-local .env, then rerun AutoKeel for S02.",
                "recorded_at": now_iso(),
            },
        )
        self.log_event(
            "swr_provider_auth_evidence_recorded",
            {"evidence": str(evidence.relative_to(self.root)), "exit_code": result.exit_code},
            slice_id=slice_["id"],
        )
        return evidence

    def require_swr_evidence_before_po(self, slice_: dict[str, Any], playbook: Path) -> CommandResult:
        if not self.swr_required(slice_) or self.swr_evidence_matches_playbook(slice_, playbook):
            return CommandResult([], 0, "SWR playbook evidence verified", "")
        failure = self.record_failure(
            slice_["id"],
            "swr_evidence_missing",
            "high",
            "SWR-required slice attempted PO start without matching SWR playbook evidence.",
            "Blocked PO start before execution.",
            playbook if playbook.exists() else None,
        )
        self.mark_slice_status(
            slice_["id"],
            "blocked_compile_inputs",
            failure_path=str(failure.relative_to(self.root)),
            reason="missing matching SWR playbook evidence",
        )
        return CommandResult([], 29, "", "missing matching SWR playbook evidence")

    def assert_slice_execution_lane(self, slice_: dict[str, Any], playbook: Path) -> CommandResult:
        if self.swr_required(slice_) and not self.swr_evidence_matches_playbook(slice_, playbook):
            return CommandResult(
                [],
                29,
                "",
                "SWR-required slice cannot enter PO without matching keel-swr evidence",
            )
        return CommandResult([], 0, "", "")

    def swr_kernel_pin(self) -> dict[str, Any]:
        """Identify the SWR execution kernel: nested-repo commit + dirty state.

        The kernel (staged-workflow-runner) lives in a nested git repo that the
        parent keel repo gitignores; without this pin no run can name the code
        that produced it.
        """
        keel_root = Path(self.policy.get("keel_root", "/Users/aeziz-local/keel"))
        kernel_root = keel_root / "tools" / "staged-workflow-runner"
        info: dict[str, Any] = {"kernel_root": str(kernel_root), "kernel_commit": None, "kernel_dirty": None}
        if not kernel_root.exists():
            return info
        head = subprocess.run(
            ["git", "-C", str(kernel_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        status = subprocess.run(
            ["git", "-C", str(kernel_root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=False,
        )
        if head.returncode == 0:
            info["kernel_commit"] = head.stdout.strip()
        if status.returncode == 0:
            dirty_paths = [line[3:].strip() for line in status.stdout.splitlines() if line.strip()]
            info["kernel_dirty"] = bool(dirty_paths)
            info["kernel_dirty_paths"] = dirty_paths[:20]
        return info

    def materialize_swr_task_pack(self) -> CommandResult:
        swr_policy = self.policy.get("swr", {})
        keel_root = Path(self.policy.get("keel_root", "/Users/aeziz-local/keel"))
        source_rel = str(swr_policy.get("task_pack_source") or "tools/staged-workflow-runner/automation/task_packs/gstack_design_to_po_playbook")
        workdir_rel = str(swr_policy.get("task_pack_workdir") or "automation/task_packs/gstack_design_to_po_playbook")
        source = keel_root / source_rel
        target = self.root / workdir_rel

        if not source.exists() or not source.is_dir():
            return CommandResult([], 27, "", f"SWR task pack missing: {source}")

        kernel = self.swr_kernel_pin()
        if bool(swr_policy.get("require_pinned_kernel", True)):
            if not kernel.get("kernel_commit"):
                return CommandResult([], 27, "", f"SWR kernel is not a git checkout; refusing unpinned launch: {kernel.get('kernel_root')}")
            if kernel.get("kernel_dirty"):
                return CommandResult(
                    [],
                    27,
                    "",
                    (
                        "SWR kernel checkout is dirty; commit or revert before launch: "
                        + ", ".join(kernel.get("kernel_dirty_paths") or [])
                    ),
                )

        if self.dry_run:
            return CommandResult(["test", "-d", str(source)], 0, "dry run SWR task-pack materialization planned", "")

        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source,
            target,
            ignore=shutil.ignore_patterns(".local", ".pytest_cache", "__pycache__", "*.pyc"),
        )
        self.write_swr_autonomous_contract_overlay(target)
        pack_files = sorted(
            str(item.relative_to(target)) + ":" + file_sha256(item)
            for item in target.rglob("*")
            if item.is_file()
        )
        kernel["task_pack_content_sha256"] = hashlib.sha256("\n".join(pack_files).encode("utf-8")).hexdigest()
        write_json_atomic(target / "kernel_pin.json", {"schema_version": "autokeel.swr_kernel_pin.v1", "pinned_at": now_iso(), **kernel})
        self.log_event("swr_kernel_pinned", kernel)
        return CommandResult(["cp", "-R", str(source), str(target)], 0, str(target.relative_to(self.root)), "")

    def write_swr_autonomous_contract_overlay(self, task_pack_root: Path) -> None:
        marker = "<!-- autokeel-autonomous-validation-overlay-v1 -->"
        overlay = f"""

{marker}

## AutoKeel Autonomous Validation Overlay

This repository runs the SWR output through
`scripts/validate_playbook_autonomous.py` before plan-orchestrator execution.
For AutoKeel autonomous runs, this overlay supersedes any older table-column
summary above when the stricter validator requires additional columns.

The execution table must include these columns:

| step_id | phase | action | why_now | owner_type | prerequisites | repo_surfaces | deliverable | exit_criteria | allowed_write_roots | requires_red_green | required_verification_commands |

Additional validator requirements:

- Include the literal term `autonomous_gate_review` in the playbook when the
  slice is high risk or uses autonomous gate substitution.
- `prerequisites` must be `none`, a comma-separated list of exact step ids
  such as `03,05`, or a numeric range such as `01-04`. Do not use natural
  language forms such as `03 and 05`; plan-orchestrator normalization rejects
  those strings before execution.
- `allowed_write_roots` must use semicolon-separated repo-relative roots such
  as `src/api/app.py; tests/test_api_security.py`. Do not use comma-separated
  roots; plan-orchestrator treats comma text as one root.
- `repo_surfaces` are inputs, not outputs. They must name tracked repo paths
  that already exist before the row runs, or exact deliverable paths from
  earlier rows. Do not list same-row or future deliverables as repo surfaces;
  plan-orchestrator refuses to materialize missing inputs into its worktree.
- Every executable code, script, or test deliverable must have a non-empty
  `required_verification_commands` cell.
- Rows with `requires_red_green=true` must have a non-empty
  `required_verification_commands` cell.
- Docs-only rows with `requires_red_green=false` still need concrete reviewable
  validation commands or content checks in `required_verification_commands`.
- Do not emit active manual gates, human approval claims, or
  `keel-run mark-manual-gate`.
""".rstrip()

        for rel in (
            "corpus/markdown_playbook_v1_contract.md",
            "prompts/stage3_execution_row_draft.md",
            "prompts/stage4_gate_and_contract_review.md",
            "prompts/stage5_final_markdown_playbook.md",
            "shared_instructions.md",
        ):
            path = task_pack_root / rel
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8")
            if marker in text:
                continue
            path.write_text(text.rstrip() + "\n" + overlay + "\n", encoding="utf-8")

    def materialize_swr_supervisor_task_pack(self) -> CommandResult:
        swr_policy = self.policy.get("swr", {})
        keel_root = Path(self.policy.get("keel_root", "/Users/aeziz-local/keel"))
        source_rel = str(
            swr_policy.get("supervisor_task_pack_source")
            or "tools/staged-workflow-runner/automation/task_packs/responses_runner_v2_supervisor_internal"
        )
        workdir_rel = str(
            swr_policy.get("supervisor_task_pack_workdir")
            or "automation/task_packs/responses_runner_v2_supervisor_internal"
        )
        source = keel_root / source_rel
        target = self.root / workdir_rel
        if not source.exists() or not source.is_dir():
            return CommandResult([], 33, "", f"SWR supervisor task pack missing: {source}")
        if self.dry_run:
            return CommandResult(["test", "-d", str(source)], 0, "dry run SWR supervisor task-pack materialization planned", "")
        if target.exists():
            shutil.rmtree(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source,
            target,
            ignore=shutil.ignore_patterns(".local", ".pytest_cache", "__pycache__", "*.pyc"),
        )
        return CommandResult(["cp", "-R", str(source), str(target)], 0, str(target.relative_to(self.root)), "")

    def build_swr_command(self, slice_: dict[str, Any]) -> list[str]:
        swr_policy = self.policy.get("swr", {})
        keel_root = Path(self.policy.get("keel_root", "/Users/aeziz-local/keel"))
        keel_swr = keel_root / "bin" / "keel-swr"
        workflow_rel = str(swr_policy.get("workflow_file") or "automation/task_packs/gstack_design_to_po_playbook/workflows/gstack_design_to_po_playbook.workflow.json")
        output_root_rel = str(swr_policy.get("output_root") or ".local/autokeel/swr/runs")
        max_wait_seconds = str(swr_policy.get("max_wait_seconds", 5))

        cmd = [
            str(keel_swr),
            "run",
            "--root",
            str(self.root),
            "--workflow-file",
            workflow_rel,
            "--run-name",
            f"autokeel-{str(slice_['id']).lower()}-{slug_ts()}",
            "--output-root",
            output_root_rel,
            "--wait",
            "--max-wait-seconds",
            max_wait_seconds,
        ]
        if bool(swr_policy.get("skip_token_count", False)):
            cmd.append("--skip-token-count")
        for candidate in (self.design_doc_path(slice_), self.root / str(slice_.get("autoplan", "")), self.root / str(slice_.get("brief", ""))):
            if candidate.exists():
                cmd.extend(["--primary-job-input", str(candidate.relative_to(self.root))])
        return cmd

    def keel_swr_path(self) -> Path:
        return Path(self.policy.get("keel_root", "/Users/aeziz-local/keel")) / "bin" / "keel-swr"

    def swr_workflow_file(self) -> str:
        return str(
            self.policy.get("swr", {}).get("workflow_file")
            or "automation/task_packs/gstack_design_to_po_playbook/workflows/gstack_design_to_po_playbook.workflow.json"
        )

    def swr_wait_seconds(self) -> str:
        return str(self.policy.get("swr", {}).get("max_wait_seconds", 5))

    def build_swr_resume_command(self, manifest_path: Path) -> list[str]:
        payload = read_json(manifest_path, {})
        stage_id = str(payload.get("current_stage_id") or "")
        run_dir = str(payload.get("run_dir") or manifest_path.parent.relative_to(self.root))
        return [
            str(self.keel_swr_path()),
            "resume",
            "--root",
            str(self.root),
            "--run-dir",
            run_dir,
            "--stage",
            stage_id,
            "--wait",
            "--max-wait-seconds",
            self.swr_wait_seconds(),
        ]

    def build_swr_continue_command(self, manifest_path: Path, review_bundle: str) -> list[str]:
        payload = read_json(manifest_path, {})
        run_dir = str(payload.get("run_dir") or manifest_path.parent.relative_to(self.root))
        cmd = [
            str(self.keel_swr_path()),
            "run",
            "--root",
            str(self.root),
            "--workflow-file",
            self.swr_workflow_file(),
            "--run-dir",
            run_dir,
            "--review-bundle",
            review_bundle,
            "--wait",
            "--max-wait-seconds",
            self.swr_wait_seconds(),
        ]
        overrides = payload.get("operator_overrides")
        if isinstance(overrides, dict):
            for candidate in overrides.get("primary_job_inputs", []) if isinstance(overrides.get("primary_job_inputs"), list) else []:
                if isinstance(candidate, str) and candidate:
                    cmd.extend(["--primary-job-input", candidate])
            for candidate in overrides.get("reference_context", []) if isinstance(overrides.get("reference_context"), list) else []:
                if isinstance(candidate, str) and candidate:
                    cmd.extend(["--reference-context", candidate])
        if bool(self.policy.get("swr", {}).get("skip_token_count", False)):
            cmd.append("--skip-token-count")
        return cmd

    def build_swr_stage_rerun_command(self, repair_plan: dict[str, Any]) -> list[str]:
        run_dir = str(repair_plan.get("run_dir") or "")
        stage_id = str(repair_plan.get("repair_stage_id") or "")
        if not run_dir or not stage_id:
            raise AutoKeelError("SWR validation repair plan is missing run_dir or repair_stage_id")
        cmd = [
            str(self.keel_swr_path()),
            "run",
            "--root",
            str(self.root),
            "--workflow-file",
            self.swr_workflow_file(),
            "--run-dir",
            run_dir,
            "--stage",
            stage_id,
        ]
        review_bundle = repair_plan.get("source_review_bundle")
        if isinstance(review_bundle, str) and review_bundle:
            cmd.extend(["--review-bundle", review_bundle])
        cmd.extend(["--wait", "--max-wait-seconds", self.swr_wait_seconds()])

        manifest_rel = repair_plan.get("run_manifest")
        manifest_path = self.root / str(manifest_rel) if isinstance(manifest_rel, str) and manifest_rel else None
        payload = read_json(manifest_path, {}) if manifest_path is not None else {}
        overrides = payload.get("operator_overrides") if isinstance(payload, dict) else {}
        if isinstance(overrides, dict):
            for candidate in overrides.get("primary_job_inputs", []) if isinstance(overrides.get("primary_job_inputs"), list) else []:
                if isinstance(candidate, str) and candidate:
                    cmd.extend(["--primary-job-input", candidate])
            for candidate in overrides.get("reference_context", []) if isinstance(overrides.get("reference_context"), list) else []:
                if isinstance(candidate, str) and candidate:
                    cmd.extend(["--reference-context", candidate])
        if bool(self.policy.get("swr", {}).get("skip_token_count", False)):
            cmd.append("--skip-token-count")
        return cmd

    def reset_swr_manifest_for_stage_rerun(self, manifest_path: Path, stage_id: str) -> None:
        payload = read_json(manifest_path, {})
        if not isinstance(payload, dict):
            raise AutoKeelError(f"invalid SWR manifest: {manifest_path}")
        stages = payload.get("stages")
        if not isinstance(stages, list):
            raise AutoKeelError(f"SWR manifest has no stages: {manifest_path}")
        stage_order = payload.get("stage_order")
        if isinstance(stage_order, list) and stage_id in stage_order:
            target_index = stage_order.index(stage_id)
        else:
            stage_numbers = {
                str(stage.get("stage_id")): int(stage.get("stage_number") or 0)
                for stage in stages
                if isinstance(stage, dict) and stage.get("stage_id") is not None
            }
            if stage_id not in stage_numbers:
                raise AutoKeelError(f"SWR stage not found in manifest: {stage_id}")
            target_index = stage_numbers[stage_id] - 1

        stale_keys = {
            "approved_from_status",
            "checkpoint_path",
            "input_manifest_json_path",
            "input_manifest_markdown_path",
            "response_id",
            "response_status",
            "response_latest_json_path",
            "response_markdown_path",
            "response_markdown_sha256",
            "response_json_path",
            "response_json_sha256",
            "review_approved",
            "review_bundle_path",
            "sidecar_response_json_path",
            "sidecar_response_json_sha256",
            "sidecar_response_markdown_path",
            "sidecar_response_markdown_sha256",
            "structured_output_path",
            "structured_output_sha256",
            "token_preflight_path",
            "token_preflight_error_path",
        }
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            current_id = str(stage.get("stage_id") or "")
            if isinstance(stage_order, list) and current_id in stage_order:
                current_index = stage_order.index(current_id)
            else:
                current_index = int(stage.get("stage_number") or 0) - 1
            if current_index < target_index:
                continue
            for key in stale_keys:
                stage.pop(key, None)
            stage["status"] = "prepared"
        payload.pop("autokeel_quarantined", None)
        payload.pop("autokeel_quarantine_reason", None)
        payload.pop("quarantined_at", None)
        payload.pop("quarantined_reason", None)
        payload["status"] = "created"
        payload["current_stage_id"] = stage_id
        write_json_atomic(manifest_path, payload)
        self.log_event(
            "swr_stage_rerun_manifest_reset",
            {"run_manifest": str(manifest_path.resolve().relative_to(self.root)), "stage_id": stage_id},
        )

    def swr_waiting_for_review(self, payload: dict[str, Any]) -> bool:
        if str(payload.get("status", "")) != "waiting_for_review":
            return False
        current_stage_id = payload.get("current_stage_id")
        for stage in payload.get("stages", []) if isinstance(payload.get("stages"), list) else []:
            if isinstance(stage, dict) and stage.get("stage_id") == current_stage_id:
                return stage.get("gate") == "review_required" and stage.get("status") == "waiting_for_review"
        return False

    def swr_final_stage_completed(self, payload: dict[str, Any]) -> bool:
        if str(payload.get("status", "")) != "completed":
            return False
        current_stage_id = payload.get("current_stage_id")
        stage_order = payload.get("stage_order")
        return isinstance(stage_order, list) and bool(stage_order) and current_stage_id == stage_order[-1]

    def swr_remote_check_due(self, slice_: dict[str, Any]) -> bool:
        state = self.load_state()
        active = state.get("active_swr_run")
        if not isinstance(active, dict) or active.get("slice") != slice_["id"]:
            return True
        checked_at = active.get("last_remote_check_at")
        if not checked_at:
            return True
        try:
            checked = datetime.fromisoformat(str(checked_at))
        except ValueError:
            return True
        interval = int(self.policy.get("swr", {}).get("monitor_min_interval_seconds", 300))
        return (datetime.now().astimezone() - checked.astimezone()).total_seconds() >= interval

    def update_active_swr_remote_check(self, slice_id: str, result: CommandResult) -> None:
        state = self.load_state()
        active = state.get("active_swr_run")
        if not isinstance(active, dict) or active.get("slice") != slice_id:
            return
        checked_at = now_iso()
        active["last_remote_check_at"] = checked_at
        active["last_remote_check_exit_code"] = result.exit_code
        active["last_remote_check_stderr"] = result.stderr[-1000:]
        state["active_swr_run"] = active
        self.save_state(state)
        manifest_rel = active.get("run_manifest")
        if isinstance(manifest_rel, str) and manifest_rel:
            manifest = self.root / manifest_rel
            payload = read_json(manifest, {})
            slice_ = self.find_slice(slice_id)
            if isinstance(payload, dict) and slice_ is not None:
                self.write_swr_lease(slice_, manifest, payload, observed_at=checked_at)

    def parse_json_stdout(self, result: CommandResult, default: Any = None) -> Any:
        text = result.stdout.strip()
        if not text:
            return default
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return default

    def swr_review_dir(self, slice_: dict[str, Any], payload: dict[str, Any], review_cycle_id: str | None = None) -> Path:
        run_id = str(payload.get("run_id") or "unknown_run")
        stage_id = str(payload.get("current_stage_id") or "unknown_stage")
        default_cycle = f"{stage_id}_stage_review"
        suffix = f"-{review_cycle_id}" if review_cycle_id and review_cycle_id != default_cycle else ""
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{slice_['id']}-{run_id}-{stage_id}{suffix}").strip("-")
        return self.root / ".local" / "autokeel" / "swr" / "review_lane" / safe

    def swr_review_repair_cycle_id(self, repair_plan: dict[str, Any], stage_id: str) -> str:
        created = str(repair_plan.get("created_at") or now_iso())
        stamp = re.sub(r"[^a-z0-9]+", "-", created.lower()).strip("-") or "repair"
        return f"{stage_id}_stage_review_repair_{stamp}"

    def swr_repair_cycle_id(self, slice_: dict[str, Any], stage_id: str) -> str:
        repair = slice_.get("swr_review_repair")
        created_at = ""
        if isinstance(repair, dict):
            created_at = str(repair.get("created_at") or "")
        safe_created = re.sub(r"[^0-9a-z]+", "", created_at.lower())[:32] or now_slug().lower()
        return f"{stage_id}_stage_review_repair_{safe_created}"

    def swr_review_lane_dir(
        self,
        *,
        slice_id: str,
        run_id: str,
        stage_id: str,
        review_cycle_id: str,
    ) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{slice_id}-{run_id}-{stage_id}-{review_cycle_id}")
        return self.root / ".local/autokeel/swr/review_lane" / safe

    def assert_fresh_review_repair_dir(self, review_dir: Path, review_cycle_id: str) -> CommandResult:
        # Safety invariant: a matching cycle-id marker is NOT sufficient — a
        # prior attempt of this same cycle may have died mid-review and left
        # decision sidecars behind. Reviewing on top of those replays the
        # stale-sidecar contamination failure. Only a sidecar-free directory
        # is acceptable; retries must mint a new attempt-suffixed cycle id.
        if review_dir.exists() and any(review_dir.rglob("*.json")):
            return CommandResult(
                [],
                32,
                "",
                f"review repair directory is not fresh and contains prior sidecars: {review_dir}",
            )
        review_dir.mkdir(parents=True, exist_ok=True)
        marker = review_dir / ".autokeel_review_cycle_id"
        marker.write_text(review_cycle_id + "\n", encoding="utf-8")
        return CommandResult([], 0, "fresh review repair directory prepared", "")

    def fresh_swr_repair_cycle(self, slice_: dict[str, Any], repair_plan: dict[str, Any], stage_id: str) -> tuple[str, Path]:
        """Choose an attempt-unique review cycle id whose directory is pristine.

        The base id derives from the repair plan's created_at; if that
        directory already holds sidecars from a failed attempt, suffix the id
        (_r2, _r3, ...) until a pristine directory is found, so no attempt can
        ever consume another attempt's reviewer output.
        """
        base_cycle_id = self.swr_repair_cycle_id(slice_, stage_id)
        run_id = str(repair_plan.get("run_id") or "")
        for attempt in range(1, 10):
            cycle_id = base_cycle_id if attempt == 1 else f"{base_cycle_id}_r{attempt}"
            review_dir = self.swr_review_lane_dir(
                slice_id=slice_["id"],
                run_id=run_id,
                stage_id=stage_id,
                review_cycle_id=cycle_id,
            )
            if not review_dir.exists() or not any(review_dir.rglob("*.json")):
                return cycle_id, review_dir
        # Ten contaminated attempt dirs means something is systematically
        # wrong; surface the base dir and let the fail-closed assert stop us.
        return base_cycle_id, self.swr_review_lane_dir(
            slice_id=slice_["id"], run_id=run_id, stage_id=stage_id, review_cycle_id=base_cycle_id
        )

    def repo_artifact_path(self, rel_or_abs: str) -> Path | None:
        if not rel_or_abs:
            return None
        path = Path(rel_or_abs)
        if not path.is_absolute():
            path = self.root / path
        try:
            path.resolve().relative_to(self.root)
        except ValueError:
            return None
        return path

    def swr_decision_semantic_rejection(self, path_value: str | Path) -> bool:
        """True when a decision record is a genuine reviewer NO.

        A semantic rejection has transport status succeeded but a
        non-approving verdict or substantive blocking issues — as opposed to
        record-shape drift or transport failures.
        """
        if not path_value:
            return False
        path = self.repo_artifact_path(str(path_value))
        if path is None or not path.exists():
            return False
        decision = read_json(path, {})
        if not isinstance(decision, dict) or decision.get("status") != "succeeded":
            return False
        approval = str(decision.get("approval_decision") or "")
        return approval in {"do_not_approve", "blocked"} or bool(decision.get("blocking_issues"))

    def swr_review_failure(
        self,
        slice_: dict[str, Any],
        evidence_path: Path,
        reason: str,
        *,
        semantic_rejection: bool = False,
    ) -> CommandResult:
        manifest_path = evidence_path if evidence_path.name == "run_manifest.json" else self.active_swr_manifest_from_state(slice_) or self.latest_swr_manifest_for_slice(slice_)
        repair_plan = (
            self.plan_swr_review_repair(slice_, manifest_path, reason, semantic_rejection=semantic_rejection)
            if manifest_path is not None
            else None
        )
        failure = self.record_failure(
            slice_["id"],
            "audit_failure",
            "high",
            "SWR supervisor review did not satisfy the fail-closed review-bundle contract.",
            (
                "Stopped before creating or reusing a review bundle and planned a same-run SWR review repair."
                if isinstance(repair_plan, dict) and repair_plan.get("repair_action") in {"rerun_review_lane", "rerun_single_stage"}
                else "Stopped before creating or reusing a review bundle and before launching the next SWR stage."
            ),
            evidence_path,
        )
        self.mark_slice_status(
            slice_["id"],
            "blocked_compile_inputs",
            failure_path=str(failure.relative_to(self.root)),
            reason=reason,
            **({"swr_review_repair": repair_plan} if isinstance(repair_plan, dict) else {}),
        )
        if isinstance(repair_plan, dict) and repair_plan.get("repair_action") in {"rerun_review_lane", "rerun_single_stage"}:
            self.log_event("swr_review_repair_planned", repair_plan, slice_id=slice_["id"])
        else:
            self.quarantine_related_swr_manifest(slice_, evidence_path, reason)
        self.clear_active_swr_run(slice_["id"], reason)
        return CommandResult([], 32, "", reason)

    def plan_swr_review_repair(
        self,
        slice_: dict[str, Any],
        manifest_path: Path | None,
        reason: str,
        *,
        semantic_rejection: bool = False,
    ) -> dict[str, Any] | None:
        if manifest_path is not None and not manifest_path.is_absolute():
            manifest_path = self.root / manifest_path
        if manifest_path is None or not manifest_path.exists():
            return None
        payload = read_json(manifest_path, {})
        if not isinstance(payload, dict):
            return None

        findings = self.swr_review_history_findings(slice_, payload)
        target_stage_id = str(findings[0].get("stage_id") or "") if findings else str(payload.get("current_stage_id") or "")
        if not target_stage_id:
            return None
        target_index = self.swr_stage_index(payload, target_stage_id)
        if target_index is None:
            return None
        stages = payload.get("stages") if isinstance(payload.get("stages"), list) else []
        downstream = [
            str(stage.get("stage_id"))
            for index, stage in enumerate(stages)
            if index > target_index and isinstance(stage, dict) and stage.get("stage_id")
        ]
        artifact_errors = self.swr_stage_response_artifact_errors(payload, target_stage_id)
        source_review_stage_id = str(stages[target_index - 1].get("stage_id")) if target_index > 0 and isinstance(stages[target_index - 1], dict) else None
        source_review_bundle = (
            self.swr_review_bundle_for_stage(slice_, manifest_path, source_review_stage_id)
            if source_review_stage_id is not None
            else None
        )
        if not artifact_errors and semantic_rejection and (target_index == 0 or source_review_bundle):
            # Convergence rule: a genuine reviewer NO on a hash-stable artifact
            # can never be cured by re-reviewing identical content. Regenerate
            # the stage from its current inputs and reset downstream stages.
            repair_action = "rerun_single_stage"
            rationale = (
                "Reviewers semantically rejected the hash-stable stage artifact; re-reviewing identical "
                "content cannot converge. Regenerate the stage from current inputs and reset downstream stages."
            )
        elif not artifact_errors:
            repair_action = "rerun_review_lane"
            rationale = "The stage response artifacts are complete and hash-stable; rerun only the supervisor review lane and reset downstream stages that consumed the tainted handoff."
        elif target_index == 0 or source_review_bundle:
            repair_action = "rerun_single_stage"
            rationale = "The stage output is not safely reviewable; rerun this stage and reset downstream stages only."
        else:
            repair_action = "blocked_pending_review_bundle"
            rationale = "The stage output is not safely reviewable and the prior approved review bundle needed for a same-run stage rerun is missing."
        return {
            "created_at": now_iso(),
            "status": "planned",
            "reason": reason,
            "repair_action": repair_action,
            "repair_stage_id": target_stage_id,
            "source_review_stage_id": source_review_stage_id,
            "source_review_bundle": source_review_bundle,
            "run_id": payload.get("run_id"),
            "run_dir": payload.get("run_dir") or str(manifest_path.parent.relative_to(self.root)),
            "run_manifest": str(manifest_path.relative_to(self.root)),
            "review_history_findings": findings,
            "stage_artifact_errors": artifact_errors,
            "stale_downstream_stage_ids": downstream,
            "rationale": rationale,
        }

    def swr_review_failure_reason_is_repairable(self, reason: str) -> bool:
        normalized = reason.lower()
        return "swr review history failed closed" in normalized or "swr independent review failed closed" in normalized

    def plan_stored_swr_review_repair(self, slice_: dict[str, Any]) -> CommandResult | None:
        manifest_rel = slice_.get("swr_run_manifest")
        if not isinstance(manifest_rel, str) or not manifest_rel:
            return None
        manifest_path = self.repo_artifact_path(manifest_rel)
        if manifest_path is None or not manifest_path.exists() or manifest_path.name != "run_manifest.json":
            return None
        payload = read_json(manifest_path, {})
        if not isinstance(payload, dict) or payload.get("status") != "quarantined":
            return None
        reason = str(
            slice_.get("reason")
            or payload.get("quarantined_reason")
            or payload.get("autokeel_quarantine_reason")
            or "SWR review history failed closed for stored quarantined run"
        )
        if not self.swr_review_failure_reason_is_repairable(reason):
            return None
        repair_plan = self.plan_swr_review_repair(slice_, manifest_path, reason)
        if not isinstance(repair_plan, dict):
            return None
        if repair_plan.get("repair_action") not in {"rerun_review_lane", "rerun_single_stage", "blocked_pending_review_bundle"}:
            return None
        self.mark_slice_status(
            slice_["id"],
            "blocked_compile_inputs",
            failure_path=slice_.get("failure_path"),
            reason=reason,
            swr_review_repair=repair_plan,
        )
        self.log_event("swr_review_repair_planned_from_stored_manifest", repair_plan, slice_id=slice_["id"])
        return CommandResult(
            [],
            32,
            "",
            "SWR review repair planned from stored quarantined run; rerun AutoKeel to execute the repair without a fresh full SWR launch.",
        )

    def current_swr_stage(self, payload: dict[str, Any]) -> dict[str, Any]:
        current_stage_id = payload.get("current_stage_id")
        for stage in payload.get("stages", []) if isinstance(payload.get("stages"), list) else []:
            if isinstance(stage, dict) and stage.get("stage_id") == current_stage_id:
                return stage
        return {}

    def swr_decision_transport_error(self, path_value: str | Path, label: str) -> str | None:
        """Detect a transport-level failure recorded in a decision sidecar.

        Returns a precise human-readable description when the sidecar's status
        names a subprocess/schema/plumbing failure rather than a semantic
        reviewer verdict; returns None for genuine reviewer decisions.
        """
        if not path_value:
            return None
        path = self.repo_artifact_path(str(path_value))
        if path is None or not path.exists():
            return None
        decision = read_json(path, {})
        if not isinstance(decision, dict):
            return f"{label} sidecar is not parseable JSON (transport defect)"
        status = str(decision.get("status") or "")
        if status not in TRANSPORT_DECISION_STATUSES:
            return None
        validation_errors = decision.get("validation_errors")
        first_error = ""
        if isinstance(validation_errors, list) and validation_errors:
            first_error = str(validation_errors[0])[:300]
        return (
            f"{label} transport failure: status={status}"
            + (f"; first_error={first_error}" if first_error else "")
        )

    def swr_review_transport_failure(self, slice_: dict[str, Any], evidence_path: Path, reason: str) -> CommandResult:
        """Record a review TRANSPORT failure as a control-plane defect.

        The stage artifact is untouched and no semantic verdict exists, so the
        bounded same-run review-lane repair is planned exactly as for a review
        failure — but the ledger row is typed so it consumes the control-plane
        budget, never the SWR review-lane budget, and the recorded reason never
        claims the reviewers rejected the work.
        """
        manifest_path = (
            evidence_path
            if evidence_path.name == "run_manifest.json"
            else self.active_swr_manifest_from_state(slice_) or self.latest_swr_manifest_for_slice(slice_)
        )
        repair_plan = self.plan_swr_review_repair(slice_, manifest_path, reason) if manifest_path is not None else None
        failure = self.record_failure(
            slice_["id"],
            "swr_review_transport_failure",
            "high",
            "SWR review transport failed before any semantic reviewer verdict was produced.",
            (
                "Stopped before creating or reusing a review bundle and planned a same-run SWR review repair."
                if isinstance(repair_plan, dict) and repair_plan.get("repair_action") in {"rerun_review_lane", "rerun_single_stage"}
                else "Stopped before creating or reusing a review bundle and before launching the next SWR stage."
            ),
            evidence_path,
            repair_scope="autokeel_control_plane",
        )
        self.mark_slice_status(
            slice_["id"],
            "blocked_compile_inputs",
            failure_path=str(failure.relative_to(self.root)),
            reason=reason,
            **({"swr_review_repair": repair_plan} if isinstance(repair_plan, dict) else {}),
        )
        if isinstance(repair_plan, dict) and repair_plan.get("repair_action") in {"rerun_review_lane", "rerun_single_stage"}:
            self.log_event("swr_review_repair_planned", repair_plan, slice_id=slice_["id"])
        self.clear_active_swr_run(slice_["id"], reason)
        return CommandResult([], 33, "", reason)

    def swr_decision_record_errors(
        self,
        path_value: str | Path,
        *,
        label: str,
        expected_actor: str,
        expected_review_kind: str,
        allowed_approvals: set[str],
        payload: dict[str, Any],
        required_next_action: str | None = None,
    ) -> list[str]:
        path = self.repo_artifact_path(str(path_value))
        if path is None:
            return [f"{label} path is outside the repository: {path_value}"]
        decision = read_json(path, {})
        if not isinstance(decision, dict):
            return [f"{label} is not a JSON object: {path.relative_to(self.root)}"]

        errors: list[str] = []
        if decision.get("schema_version") != "responses_runner_v2.review_decision.v1":
            errors.append(f"{label} has unexpected schema_version")
        if decision.get("actor_role") != expected_actor:
            errors.append(f"{label} actor_role must be {expected_actor}")
        if decision.get("review_kind") != expected_review_kind:
            errors.append(f"{label} review_kind must be {expected_review_kind}")
        if decision.get("status") != "succeeded":
            errors.append(f"{label} status must be succeeded")
        approval = str(decision.get("approval_decision") or "")
        if approval not in allowed_approvals:
            errors.append(f"{label} approval_decision must be one of {sorted(allowed_approvals)}")
        if required_next_action is not None and decision.get("next_action") != required_next_action:
            errors.append(f"{label} next_action must be {required_next_action}")
        if decision.get("validation_errors"):
            errors.append(f"{label} contains validation_errors")
        if decision.get("blocking_issues"):
            errors.append(f"{label} contains blocking_issues")

        run_id = str(payload.get("run_id") or "")
        stage_id = str(payload.get("current_stage_id") or "")
        if run_id and decision.get("run_id") not in {run_id, None, ""}:
            errors.append(f"{label} run_id does not match current SWR run")
        if stage_id and decision.get("stage_id") not in {stage_id, None, ""}:
            errors.append(f"{label} stage_id does not match current SWR stage")
        return errors

    def swr_stage_review_record_errors(
        self,
        *,
        operator_review: str,
        codex_review: str,
        claude_review: str,
        consolidated_review: str,
        acceptance_record: str,
        payload: dict[str, Any],
    ) -> list[str]:
        checks = [
            (
                "operator provisional review",
                operator_review,
                "operator_codex",
                "stage_output",
                {"approve", "approve_with_conditions"},
                None,
            ),
            (
                "Codex reviewer decision",
                codex_review,
                "codex_review_agent",
                "stage_output",
                {"approve", "approve_with_conditions"},
                None,
            ),
            (
                "Claude reviewer decision",
                claude_review,
                "claude_review_agent",
                "stage_output",
                {"approve", "approve_with_conditions"},
                None,
            ),
            (
                "consolidated review",
                consolidated_review,
                "consolidation_pass",
                "consolidation",
                {"approve", "approve_with_conditions"},
                "proceed_to_operator_acceptance",
            ),
            (
                "operator acceptance",
                acceptance_record,
                "operator_codex",
                "operator_acceptance",
                {"approve"},
                "create_review_bundle",
            ),
        ]
        errors: list[str] = []
        for label, record, actor, kind, approvals, next_action in checks:
            if not record:
                errors.append(f"{label} record is missing")
                continue
            errors.extend(
                self.swr_decision_record_errors(
                    record,
                    label=label,
                    expected_actor=actor,
                    expected_review_kind=kind,
                    allowed_approvals=approvals,
                    payload=payload,
                    required_next_action=next_action,
                )
            )
        return errors

    def parse_swr_reviewer_notes_records(self, reviewer_notes: Path) -> dict[str, str]:
        if not reviewer_notes.exists():
            return {}
        records: dict[str, str] = {}
        for line in reviewer_notes.read_text(encoding="utf-8").splitlines():
            match = re.match(r"-\s+([A-Za-z0-9_]+):\s+`([^`]+)`", line.strip())
            if match:
                records[match.group(1)] = match.group(2)
        return records

    def swr_review_bundle_record_errors(self, bundle_path: Path, bundle: dict[str, Any], payload: dict[str, Any]) -> list[str]:
        records = bundle.get("review_decision_records")
        records = records if isinstance(records, dict) else {}
        reviewer_notes_rel = str(bundle.get("reviewer_notes") or "")
        reviewer_notes = self.repo_artifact_path(reviewer_notes_rel) if reviewer_notes_rel else None
        parsed = self.parse_swr_reviewer_notes_records(reviewer_notes) if reviewer_notes is not None else {}

        def record_for(key: str) -> str:
            value = records.get(key)
            if isinstance(value, str) and value:
                return value
            parsed_value = parsed.get(key)
            if isinstance(parsed_value, str) and parsed_value:
                return parsed_value
            if key == "operator_acceptance":
                acceptance = bundle.get("acceptance_record")
                if isinstance(acceptance, str) and acceptance:
                    return acceptance
            if key == "consolidated_review":
                candidate = bundle_path.parent / "consolidated_review.json"
                if candidate.exists():
                    return str(candidate.relative_to(self.root))
            return ""

        return self.swr_stage_review_record_errors(
            operator_review=record_for("operator_review"),
            codex_review=record_for("codex_review"),
            claude_review=record_for("claude_review"),
            consolidated_review=record_for("consolidated_review"),
            acceptance_record=record_for("operator_acceptance"),
            payload=payload,
        )

    def swr_review_bundle_validation_errors(self, bundle_path: Path, payload: dict[str, Any]) -> list[str]:
        bundle = read_json(bundle_path, {})
        if not isinstance(bundle, dict):
            return [f"{bundle_path.relative_to(self.root)} is not a JSON object"]
        stage_id = str(payload.get("current_stage_id") or "")
        run_id = str(payload.get("run_id") or "")
        stage = self.current_swr_stage(payload)
        errors: list[str] = []
        required_fields = {
            "run_id",
            "stage_id",
            "response_id",
            "input_sha256",
            "output_sha256",
            "reviewer_results",
            "consolidated_verdict",
            "accepted_at",
        }
        missing = sorted(required_fields - set(bundle.keys()))
        if missing:
            errors.append(f"review bundle missing required fields: {', '.join(missing)}")
        if str(bundle.get("run_id") or "") != run_id:
            errors.append("review bundle run_id does not match current SWR run")
        if str(bundle.get("stage_id") or "") != stage_id:
            errors.append("review bundle stage_id does not match current SWR stage")
        if str(bundle.get("response_id") or "") != str(stage.get("response_id") or ""):
            errors.append("review bundle response_id does not match current SWR stage")
        if bundle.get("consolidated_verdict") != "pass":
            errors.append("review bundle consolidated_verdict must be pass")
        reviewers = bundle.get("reviewer_results")
        if not isinstance(reviewers, list):
            errors.append("review bundle reviewer_results must be a list")
        else:
            required_reviewers = {"operator_codex", "codex_review_agent", "claude_review_agent"}
            reviewer_map = {str(item.get("reviewer")): str(item.get("verdict")) for item in reviewers if isinstance(item, dict)}
            for reviewer in sorted(required_reviewers):
                if reviewer_map.get(reviewer) != "pass":
                    errors.append(f"review bundle reviewer {reviewer} verdict must be pass")
        if bundle.get("review_status") != "approved":
            errors.append("review bundle review_status must be approved")
        if str(bundle.get("source_stage_id") or "") != stage_id:
            errors.append("review bundle source_stage_id does not match current SWR stage")
        if str(bundle.get("source_run_id") or "") != run_id:
            errors.append("review bundle source_run_id does not match current SWR run")

        artifacts = self.swr_stage_artifacts(payload)
        checks = [
            ("primary_artifact_markdown", artifacts.get("response_markdown"), "primary_artifact_markdown_sha256"),
            ("response_artifact_json", artifacts.get("response_json"), "response_artifact_json_sha256"),
            ("reviewer_notes", bundle.get("reviewer_notes"), "reviewer_notes_sha256"),
        ]
        hashes = bundle.get("artifact_hashes")
        hashes = hashes if isinstance(hashes, dict) else {}
        for bundle_key, expected_rel, hash_key in checks:
            rel = str(bundle.get(bundle_key) or "")
            if not rel:
                errors.append(f"review bundle {bundle_key} is missing")
                continue
            if expected_rel is not None and str(expected_rel) and bundle_key != "reviewer_notes" and rel != str(expected_rel):
                errors.append(f"review bundle {bundle_key} does not match current SWR stage artifact")
                continue
            artifact_path = self.repo_artifact_path(rel)
            if artifact_path is None or not artifact_path.exists():
                errors.append(f"review bundle {bundle_key} artifact is missing or outside repo")
                continue
            expected_hash = hashes.get(hash_key)
            if isinstance(expected_hash, str) and expected_hash and expected_hash != file_sha256(artifact_path):
                errors.append(f"review bundle {hash_key} does not match artifact")
        input_artifact = self.repo_artifact_path(str(bundle.get("input_artifact") or bundle.get("input_manifest_json") or ""))
        output_artifact = self.repo_artifact_path(str(bundle.get("response_artifact_json") or bundle.get("output_artifact") or ""))
        if input_artifact is None or output_artifact is None or not input_artifact.exists() or not output_artifact.exists():
            errors.append("review bundle input/output artifacts are missing or outside repo")
        else:
            if bundle.get("input_sha256") != file_sha256(input_artifact):
                errors.append("review bundle input_sha256 does not match artifact")
            if bundle.get("output_sha256") != file_sha256(output_artifact):
                errors.append("review bundle output_sha256 does not match artifact")
        # Safety invariant: a review bundle cannot be treated as approved based
        # on bundle self-claims alone. The accountable operator provisional,
        # both independent reviewers, deterministic consolidation, and final
        # operator acceptance must all be valid non-blocking decision records.
        errors.extend(self.swr_review_bundle_record_errors(bundle_path, bundle, payload))
        return errors

    def swr_review_bundle_is_valid(self, bundle_path: Path, payload: dict[str, Any]) -> bool:
        return not self.swr_review_bundle_validation_errors(bundle_path, payload)

    def existing_swr_review_bundle(self, slice_: dict[str, Any], payload: dict[str, Any]) -> Path | None:
        stage = self.current_swr_stage(payload)
        stage_id = str(payload.get("current_stage_id") or "")
        candidates: list[Path] = []
        stage_bundle = stage.get("review_bundle_path") if isinstance(stage, dict) else None
        if isinstance(stage_bundle, str) and stage_bundle:
            candidate = self.repo_artifact_path(stage_bundle)
            if candidate is not None:
                candidates.append(candidate)
        if stage_id:
            candidates.append(self.swr_review_dir(slice_, payload) / f"{stage_id}.review_bundle.json")

        seen: set[Path] = set()
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            if candidate.exists() and self.swr_review_bundle_is_valid(candidate, payload):
                return candidate
        return None

    def swr_bundle_matches_stage_identity(self, bundle_path: Path, payload: dict[str, Any], stage_id: str) -> bool:
        bundle = read_json(bundle_path, {})
        if not isinstance(bundle, dict):
            return False
        run_id = str(payload.get("run_id") or "")
        return (
            str(bundle.get("source_stage_id") or bundle.get("stage_id") or "") == stage_id
            and str(bundle.get("source_run_id") or bundle.get("run_id") or "") == run_id
        )

    def swr_review_bundle_candidates_for_stage(
        self,
        slice_: dict[str, Any],
        payload: dict[str, Any],
        stage: dict[str, Any],
    ) -> list[Path]:
        stage_id = str(stage.get("stage_id") or "")
        run_id = str(payload.get("run_id") or "")
        candidates: list[Path] = []
        stage_bundle = stage.get("review_bundle_path")
        if isinstance(stage_bundle, str) and stage_bundle:
            path = self.repo_artifact_path(stage_bundle)
            if path is not None and (
                bool(stage.get("review_approved")) or self.swr_bundle_matches_stage_identity(path, payload, stage_id)
            ):
                candidates.append(path)
        if run_id and stage_id:
            safe = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{slice_['id']}-{run_id}-{stage_id}").strip("-")
            candidates.append(self.root / ".local" / "autokeel" / "swr" / "review_lane" / safe / f"{stage_id}.review_bundle.json")

        seen: set[Path] = set()
        unique: list[Path] = []
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            unique.append(candidate)
        return unique

    def swr_review_history_findings(self, slice_: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        findings: list[dict[str, Any]] = []
        stages = payload.get("stages") if isinstance(payload.get("stages"), list) else []
        for stage in stages:
            if not isinstance(stage, dict):
                continue
            stage_id = str(stage.get("stage_id") or "")
            if not stage_id:
                continue
            # A stage that is generating or reset has no current review history
            # to validate: its prior-cycle bundles are superseded by design (a
            # regeneration replaces the artifacts they hashed), and validating
            # them here falsely fails the run mid-regeneration.
            if str(stage.get("status") or "") in {"prepared", "queued", "submitted", "in_progress"} and not stage.get("review_approved"):
                continue
            candidates = self.swr_review_bundle_candidates_for_stage(slice_, payload, stage)
            existing = [candidate for candidate in candidates if candidate.exists()]
            if not existing:
                if stage.get("review_approved"):
                    findings.append(
                        {
                            "stage_id": stage_id,
                            "bundle": str(candidates[0].relative_to(self.root)) if candidates else "",
                            "errors": [f"{stage_id}: approved review bundle is missing"],
                        }
                    )
                continue
            bundle_path = existing[0]
            bundle = read_json(bundle_path, {})
            bundle_stage = str(bundle.get("stage_id") or bundle.get("source_stage_id") or "") if isinstance(bundle, dict) else ""
            if bundle_stage and bundle_stage != stage_id:
                # Consumed handoff bundle from the prior stage recorded on this
                # stage's entry; it is not this stage's review history. The
                # script verifier (verify_swr_review_history.py) skips these
                # identically.
                continue
            stage_payload = dict(payload)
            stage_payload["current_stage_id"] = stage_id
            errors = self.swr_review_bundle_validation_errors(bundle_path, stage_payload)
            if errors:
                findings.append(
                    {
                        "stage_id": stage_id,
                        "bundle": str(bundle_path.relative_to(self.root)),
                        "errors": [f"{stage_id}: {error}" for error in errors],
                    }
                )
        return findings

    def swr_review_history_errors(self, payload: dict[str, Any]) -> list[str]:
        slice_id = str(payload.get("slice") or "")
        slice_ = self.find_slice(slice_id) if slice_id else None
        if slice_ is None:
            slice_ = {"id": slice_id or "SWR"}
        errors: list[str] = []
        for finding in self.swr_review_history_findings(slice_, payload):
            bundle = str(finding.get("bundle") or "")
            stage_id = str(finding.get("stage_id") or "")
            if bundle:
                errors.append(f"{stage_id}: review bundle failed decision-record/hash validation: {bundle}")
            else:
                errors.append(f"{stage_id}: review bundle failed decision-record/hash validation")
        return errors

    def swr_supervisor_session_id(self, slice_: dict[str, Any], payload: dict[str, Any]) -> str:
        raw = f"autokeel-{slice_['id']}-{payload.get('run_id') or 'run'}"
        return re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-").lower()

    def ensure_swr_supervisor_session(self, slice_: dict[str, Any], manifest_path: Path) -> tuple[str, CommandResult]:
        payload = read_json(manifest_path, {})
        if not isinstance(payload, dict):
            payload = {}
        task_pack = self.materialize_swr_supervisor_task_pack()
        if not task_pack.ok:
            self.log_event(
                "swr_supervisor_task_pack_missing",
                {"exit_code": task_pack.exit_code, "stderr": task_pack.stderr[-2000:]},
                slice_id=slice_["id"],
            )
            return "", task_pack
        session_id = self.swr_supervisor_session_id(slice_, payload)
        session_manifest = self.root / ".local" / "automation" / "responses_runner_v2" / "supervisor_sessions" / session_id / "supervisor_session.json"
        if session_manifest.exists():
            return session_id, CommandResult(["test", "-f", str(session_manifest)], 0, str(session_manifest.relative_to(self.root)), "")
        cmd = [
            str(self.keel_swr_path()),
            "supervisor",
            "init-session",
            "--root",
            str(self.root),
            "--clarified-task-brief",
            str((self.root / str(slice_["brief"])).relative_to(self.root)),
            "--summary",
            f"AutoKeel SWR review lane for {slice_['id']} {slice_.get('name', '')}".strip(),
            "--session-id",
            session_id,
        ]
        result = self.runner.run(cmd, cwd=self.root, timeout=self.po_timeout_seconds())
        if result.ok:
            state = self.load_state()
            active = state.get("active_swr_run")
            if isinstance(active, dict) and active.get("slice") == slice_["id"]:
                active["supervisor_session_id"] = session_id
                state["active_swr_run"] = active
                self.save_state(state)
        self.log_event(
            "swr_supervisor_session_ready" if result.ok else "swr_supervisor_session_failed",
            {"session_id": session_id, "exit_code": result.exit_code, "stdout": result.stdout[-2000:], "stderr": result.stderr[-2000:]},
            slice_id=slice_["id"],
        )
        return session_id, result

    def swr_stage_artifacts(self, payload: dict[str, Any]) -> dict[str, str]:
        stage_summary = self.current_swr_stage(payload)
        stage_dir = str(stage_summary.get("stage_dir") or "")
        return {
            "run_manifest": str(payload.get("run_dir", "")).rstrip("/") + "/run_manifest.json",
            "stage_checkpoint": str(stage_summary.get("checkpoint_path") or (f"{stage_dir}/stage_checkpoint.json" if stage_dir else "")),
            "input_manifest_json": str(stage_summary.get("input_manifest_json_path") or (f"{stage_dir}/input_manifest.json" if stage_dir else "")),
            "input_manifest_markdown": str(stage_summary.get("input_manifest_markdown_path") or (f"{stage_dir}/input_manifest.md" if stage_dir else "")),
            "request_payload": str(stage_summary.get("request_payload_path") or (f"{stage_dir}/request_payload.json" if stage_dir else "")),
            "response_markdown": str(stage_summary.get("response_markdown_path") or stage_summary.get("response_final_markdown_path") or (f"{stage_dir}/response.final.md" if stage_dir else "")),
            "response_json": str(stage_summary.get("response_json_path") or stage_summary.get("response_final_json_path") or (f"{stage_dir}/response.final.json" if stage_dir else "")),
        }

    def swr_review_embedded_sources(self, slice_: dict[str, Any], payload: dict[str, Any]) -> list[dict[str, Any]]:
        """Embed the source text of cited authority files into the review job.

        Read-only reviewers blocked a healthy cycle because the job referenced
        the primary/contract files by path only. Embedding bounded content plus
        the full-file sha256 removes that objection deterministically. Only
        tracked repo docs are embedded — never provider payloads or health data.
        """
        max_bytes = int(self.policy.get("swr", {}).get("embedded_source_max_bytes", 65536))
        artifacts = self.swr_stage_artifacts(payload)
        workdir_rel = str(self.policy.get("swr", {}).get("task_pack_workdir") or "automation/task_packs/gstack_design_to_po_playbook")
        candidates = [
            ("primary_artifact_markdown", artifacts.get("response_markdown")),
            ("input_manifest_markdown", artifacts.get("input_manifest_markdown")),
            ("playbook_contract", f"{workdir_rel}/corpus/markdown_playbook_v1_contract.md"),
            ("autoplan", slice_.get("autoplan")),
            ("brief", slice_.get("brief")),
        ]
        sources: list[dict[str, Any]] = []
        for label, rel in candidates:
            if not rel:
                continue
            path = self.repo_artifact_path(str(rel))
            if path is None or not path.is_file():
                continue
            rel_str = str(path.relative_to(self.root))
            if rel_str.startswith(("data/", "private/")) or path.name.startswith(".env"):
                continue
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            truncated = len(content.encode("utf-8")) > max_bytes
            if truncated:
                content = content.encode("utf-8")[:max_bytes].decode("utf-8", errors="ignore")
            sources.append(
                {
                    "label": label,
                    "path": rel_str,
                    "sha256": file_sha256(path),
                    "truncated": truncated,
                    "content": content,
                }
            )
        return sources

    def write_swr_stage_review_job(self, slice_: dict[str, Any], manifest_path: Path, review_dir: Path, outcome_path: Path) -> Path:
        payload = read_json(manifest_path, {})
        if not isinstance(payload, dict):
            payload = {}
        review_dir.mkdir(parents=True, exist_ok=True)
        artifacts = self.swr_stage_artifacts(payload)
        job = {
            "job_id": f"autokeel_{slice_['id']}_{payload.get('run_id')}_{payload.get('current_stage_id')}_stage_review",
            "objective": "Review this completed SWR stage output before any next-stage launch.",
            "review_kind": "stage_output",
            "slice": slice_["id"],
            "workflow_id": payload.get("workflow_id"),
            "run_id": payload.get("run_id"),
            "stage_id": payload.get("current_stage_id"),
            "run_manifest": str(manifest_path.relative_to(self.root)),
            "stage_outcome": str(outcome_path.relative_to(self.root)),
            "reviewed_artifacts": artifacts,
            "allowed_write_paths": [
                str(review_dir.relative_to(self.root)),
                str((review_dir / "reviewer_notes.md").relative_to(self.root)),
                str((review_dir / "approved_handoff.md").relative_to(self.root)),
            ],
            "required_checks": [
                "Do not advance output-limit incomplete artifacts.",
                "Do not advance failed-no-artifact attempts.",
                "Verify Stage 1 through Stage 4 outputs receive approved review bundles before the next stage.",
                "Reject unsupported claims and any bundle path/hash mismatch.",
            ],
            "next_stage_rule": "If approved, AutoKeel may create an approved review bundle and continue the same SWR run with --review-bundle.",
            "embedded_sources": self.swr_review_embedded_sources(slice_, payload),
            "embedded_sources_note": (
                "Source text of the primary and contract authority files is embedded above "
                "with full-file sha256 digests; grounding checks must use these instead of "
                "blocking on missing source text."
            ),
        }
        path = review_dir / "stage_review_job.json"
        write_json_atomic(path, job)
        return path

    def write_swr_reviewer_notes(self, review_dir: Path, payload: dict[str, Any], files: dict[str, str]) -> Path:
        notes = review_dir / "reviewer_notes.md"
        lines = [
            "# SWR Stage Review Notes",
            "",
            f"- workflow_id: {payload.get('workflow_id')}",
            f"- run_id: {payload.get('run_id')}",
            f"- stage_id: {payload.get('current_stage_id')}",
            "",
            "## Review Artifacts",
            "",
        ]
        for label, rel in files.items():
            lines.append(f"- {label}: `{rel}`")
        lines.extend(
            [
                "",
                "## Decision",
                "",
                "AutoKeel created this note after the SWR supervisor operator, reviewer, consolidation, and acceptance artifacts were produced. It is used only as the hash-stable reviewer-notes input to the approved review bundle.",
            ]
        )
        notes.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
        return notes

    def harden_swr_review_bundle(
        self,
        slice_: dict[str, Any],
        bundle_path: Path,
        payload: dict[str, Any],
        acceptance_path: Path,
        decision_records: dict[str, str],
        reviewer_notes_path: Path | None = None,
    ) -> None:
        bundle = read_json(bundle_path, {})
        if not isinstance(bundle, dict):
            bundle = {}
        artifacts = self.swr_stage_artifacts(payload)
        input_rel = artifacts.get("input_manifest_json") or artifacts.get("request_payload") or artifacts.get("stage_checkpoint")
        output_rel = artifacts.get("response_json")
        input_path = self.repo_artifact_path(str(input_rel or ""))
        output_path = self.repo_artifact_path(str(output_rel or ""))
        if input_path is None or output_path is None or not input_path.exists() or not output_path.exists():
            return
        stage = self.current_swr_stage(payload)
        bundle.update(
            {
                "slice": slice_["id"],
                "run_id": str(payload.get("run_id") or ""),
                "stage_id": str(payload.get("current_stage_id") or ""),
                "response_id": str(stage.get("response_id") or ""),
                "review_status": "approved",
                "source_stage_id": str(payload.get("current_stage_id") or ""),
                "source_run_id": str(payload.get("run_id") or ""),
                "input_artifact": str(input_path.relative_to(self.root)),
                "input_sha256": file_sha256(input_path),
                "output_artifact": str(output_path.relative_to(self.root)),
                "output_sha256": file_sha256(output_path),
                "primary_artifact_markdown": artifacts.get("response_markdown"),
                "response_artifact_json": artifacts.get("response_json"),
                "reviewer_results": [
                    {"reviewer": "operator_codex", "verdict": "pass"},
                    {"reviewer": "codex_review_agent", "verdict": "pass"},
                    {"reviewer": "claude_review_agent", "verdict": "pass"},
                ],
                "consolidated_verdict": "pass",
                "accepted_at": now_iso(),
                "acceptance_record": str(acceptance_path.relative_to(self.root)),
                "review_decision_records": decision_records,
            }
        )
        if reviewer_notes_path is not None:
            bundle.setdefault("reviewer_notes", str(reviewer_notes_path.relative_to(self.root)))
        write_json_atomic(bundle_path, bundle)

    def handle_swr_continue_result(self, slice_: dict[str, Any], manifest_path: Path, result: CommandResult) -> CommandResult:
        self.log_event(
            "swr_stage_continue_passed" if result.ok else "swr_stage_continue_failed",
            {"exit_code": result.exit_code, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]},
            slice_id=slice_["id"],
        )
        if not result.ok:
            if self.swr_wait_timeout_in_progress(result):
                self.record_active_swr_run(slice_, manifest_path, "SWR background run in progress", observed_result=result)
                return CommandResult(result.argv, 31, result.stdout, "SWR background run is still in progress; do not relaunch")
            return result
        payload = read_json(manifest_path, {})
        if isinstance(payload, dict) and self.swr_final_stage_completed(payload):
            source = self.materialize_swr_playbook_from_manifest(slice_, manifest_path)
            playbook = self.root / slice_["playbook"]
            if playbook.exists():
                self.record_swr_materialization(slice_, result.argv, source)
                return result
        self.record_active_swr_run(slice_, manifest_path, "SWR background run in progress")
        return CommandResult(result.argv, 31, result.stdout, "SWR background run is still in progress; do not relaunch")

    def swr_repair_authorized_by_policy(self, slice_: dict[str, Any], repair_plan: dict[str, Any]) -> tuple[bool, str]:
        policy = self.authorization_policy.get("swr_single_stage_repair", {})
        if not bool(policy.get("auto_authorized", False)):
            return False, "single-stage SWR repair is not auto-authorized by policy"
        allowed = set(policy.get("require_repair_stage_id", []) or [])
        stage_id = str(repair_plan.get("repair_stage_id") or "")
        if stage_id not in allowed:
            return False, f"repair_stage_id is not policy-allowed: {stage_id}"
        manifest_rel = repair_plan.get("run_manifest")
        if bool(policy.get("require_existing_run_manifest", True)):
            if not isinstance(manifest_rel, str) or not manifest_rel or not (self.root / manifest_rel).exists():
                return False, "required SWR run manifest is missing"
        bundle_rel = repair_plan.get("source_review_bundle")
        if bool(policy.get("require_existing_review_bundle", True)):
            if not isinstance(bundle_rel, str) or not bundle_rel or not (self.root / bundle_rel).exists():
                return False, "required SWR review bundle is missing"
        if bool(policy.get("require_no_open_high_or_critical_failures", True)) and self.high_or_critical_open_failures(slice_["id"]):
            return False, "open high/critical failures block SWR repair"
        max_total = int(policy.get("max_repairs_per_slice", 2))
        max_stage = int(policy.get("max_repairs_per_stage", 1))
        repairs = [
            event
            for event in iter_jsonl(self.events_path)
            if event.get("slice") == slice_["id"] and event.get("event") == "swr_validation_repair_stage_passed"
        ]
        if len(repairs) >= max_total:
            return False, "SWR repair count exceeds policy"
        same_stage = [
            event
            for event in iter_jsonl(self.events_path)
            if event.get("slice") == slice_["id"]
            and event.get("event") == "swr_validation_repair_stage_passed"
            and isinstance(event.get("details"), dict)
            and event["details"].get("repair_stage_id") == stage_id
        ]
        if len(same_stage) >= max_stage:
            return False, "same-stage SWR repair count exceeds policy"
        return True, "policy-authorized single-stage SWR repair"

    def run_swr_validation_repair(self, slice_: dict[str, Any], repair_plan: dict[str, Any]) -> CommandResult:
        recovered = self.recover_revalidated_swr_playbook(slice_, repair_plan)
        if recovered is not None:
            return recovered

        authorized, auth_reason = self.swr_repair_authorized_by_policy(slice_, repair_plan)
        if repair_plan.get("status") == "authorized":
            authorized = True
            auth_reason = "repair plan already marked authorized"
        if not authorized:
            command: list[str] = []
            try:
                command = self.build_swr_stage_rerun_command(repair_plan)
            except AutoKeelError:
                command = []
            self.log_event(
                "swr_validation_repair_waiting_for_authorization",
                {
                    "repair_stage_id": repair_plan.get("repair_stage_id"),
                    "run_manifest": repair_plan.get("run_manifest"),
                    "authorization_reason": auth_reason,
                    "planned_command": " ".join(shlex.quote(part) for part in command) if command else None,
                },
                slice_id=slice_["id"],
            )
            return CommandResult(command, 34, "", f"SWR validation repair is blocked: {auth_reason}")
        self.log_event(
            "swr_validation_repair_policy_authorized",
            {"repair_stage_id": repair_plan.get("repair_stage_id"), "authorization_reason": auth_reason},
            slice_id=slice_["id"],
        )

        if repair_plan.get("repair_action") != "rerun_single_stage":
            self.log_event(
                "swr_validation_repair_not_runnable",
                {"repair_action": repair_plan.get("repair_action"), "rationale": repair_plan.get("rationale")},
                slice_id=slice_["id"],
            )
            return CommandResult([], 34, "", "SWR validation repair plan is not runnable")

        manifest_rel = repair_plan.get("run_manifest")
        stage_id = str(repair_plan.get("repair_stage_id") or "")
        if not isinstance(manifest_rel, str) or not manifest_rel or not stage_id:
            return CommandResult([], 34, "", "SWR validation repair plan is missing run manifest or stage id")
        manifest_path = self.root / manifest_rel
        if not manifest_path.exists():
            return CommandResult([], 34, "", f"SWR validation repair manifest missing: {manifest_rel}")

        cmd = self.build_swr_stage_rerun_command(repair_plan)
        if self.dry_run:
            self.log_event(
                "dry_run_swr_validation_repair_planned",
                {"command": " ".join(shlex.quote(part) for part in cmd)},
                slice_id=slice_["id"],
            )
            return CommandResult(cmd, 0, "dry run SWR validation repair planned", "")

        self.reset_swr_manifest_for_stage_rerun(manifest_path, stage_id)
        result = self.runner.run(cmd, cwd=self.root, timeout=self.po_timeout_seconds())
        self.log_event(
            "swr_validation_repair_stage_passed" if result.ok else "swr_validation_repair_stage_failed",
            {
                "repair_stage_id": stage_id,
                "exit_code": result.exit_code,
                "stdout": result.stdout[-4000:],
                "stderr": result.stderr[-4000:],
            },
            slice_id=slice_["id"],
        )
        if not result.ok:
            if self.swr_wait_timeout_in_progress(result):
                self.record_active_swr_run(slice_, manifest_path, "SWR validation repair in progress", observed_result=result)
                return CommandResult(result.argv, 31, result.stdout, "SWR validation repair is still in progress; do not relaunch")
            return result

        payload = read_json(manifest_path, {})
        if isinstance(payload, dict) and self.swr_waiting_for_review(payload):
            return self.run_swr_review_lane(slice_, manifest_path)
        if isinstance(payload, dict) and self.swr_final_stage_completed(payload):
            source = self.materialize_swr_playbook_from_manifest(slice_, manifest_path)
            playbook = self.root / slice_["playbook"]
            if playbook.exists():
                self.clear_swr_validation_repair(slice_["id"])
                self.record_swr_materialization(slice_, result.argv, source)
                return result

        self.record_active_swr_run(slice_, manifest_path, "SWR validation repair in progress")
        return CommandResult(result.argv, 31, result.stdout, "SWR validation repair is still in progress; do not relaunch")

    def write_swr_corrective_findings(self, slice_: dict[str, Any], repair_plan: dict[str, Any]) -> Path | None:
        """Collect the rejecting reviewers' blocking issues for a stage rerun.

        Scans the newest review-lane cycle directories for the repaired stage,
        extracts every blocking issue from semantic (status=succeeded)
        reviewer/operator decisions, and writes a bounded corrective-findings
        document inside the run directory for --reference-context injection.
        Returns None when no blocking findings exist.
        """
        stage_id = str(repair_plan.get("repair_stage_id") or "")
        run_id = str(repair_plan.get("run_id") or "")
        run_dir_rel = str(repair_plan.get("run_dir") or "")
        if not stage_id or not run_id or not run_dir_rel:
            return None
        lane_root = self.root / ".local/autokeel/swr/review_lane"
        if not lane_root.exists():
            return None
        prefix = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{slice_['id']}-{run_id}-{stage_id}")
        cycle_dirs = sorted(
            (d for d in lane_root.iterdir() if d.is_dir() and d.name.startswith(prefix)),
            key=lambda d: d.stat().st_mtime,
            reverse=True,
        )
        findings: list[str] = []
        seen: set[str] = set()
        # Findings carried explicitly on the plan come first — they may
        # originate from another stage's review cycle (e.g. a downstream gate
        # stage rejecting this stage's rows) that the sidecar scan below
        # cannot reach.
        for carried in repair_plan.get("corrective_findings") or []:
            text = str(carried).strip()
            if text and text not in seen:
                seen.add(text)
                findings.append(f"- {text}")
        for cycle_dir in cycle_dirs[:3]:
            sidecars = sorted(cycle_dir.glob("agents/cmd_*_review_agent_*.json")) + sorted(
                cycle_dir.glob("operator/cmd_operator_codex_*.json")
            )
            for sidecar in sidecars:
                decision = read_json(sidecar, None)
                if not isinstance(decision, dict) or decision.get("status") != "succeeded":
                    continue
                actor = str(decision.get("actor_role") or "reviewer")
                for issue in decision.get("blocking_issues") or []:
                    if not isinstance(issue, dict):
                        continue
                    description = str(issue.get("description") or "").strip()
                    if not description or description in seen:
                        continue
                    seen.add(description)
                    affected = ", ".join(str(a) for a in (issue.get("affected_artifacts") or [])[:4])
                    findings.append(f"- [{actor}] {description}" + (f" (affected: {affected})" if affected else ""))
            if findings:
                break
        if not findings:
            return None
        run_dir = self.root / run_dir_rel
        if not run_dir.exists():
            return None
        path = run_dir / f"corrective_findings_{stage_id}.md"
        text = (
            f"# Corrective Findings for {stage_id} Regeneration\n\n"
            "A prior draft of this stage was REJECTED by independent fail-closed review.\n"
            "The regenerated output MUST resolve every finding below; reproducing any of\n"
            "them will fail review again.\n\n"
            + "\n".join(findings[:20])
            + "\n"
        )
        path.write_text(text, encoding="utf-8")
        return path

    def run_swr_review_repair(self, slice_: dict[str, Any], repair_plan: dict[str, Any]) -> CommandResult:
        authorized, auth_reason = self.swr_review_repair_authorized_by_policy(slice_, repair_plan)
        if repair_plan.get("status") == "authorized":
            authorized = True
            auth_reason = "repair plan already marked authorized"
        if not authorized:
            self.log_event(
                "swr_review_repair_waiting_for_authorization",
                {
                    "repair_stage_id": repair_plan.get("repair_stage_id"),
                    "run_manifest": repair_plan.get("run_manifest"),
                    "authorization_reason": auth_reason,
                },
                slice_id=slice_["id"],
            )
            return CommandResult([], 34, "", f"SWR review repair is blocked: {auth_reason}")

        manifest_rel = repair_plan.get("run_manifest")
        stage_id = str(repair_plan.get("repair_stage_id") or "")
        if not isinstance(manifest_rel, str) or not manifest_rel or not stage_id:
            return CommandResult([], 34, "", "SWR review repair plan is missing run manifest or stage id")
        manifest_path = self.root / manifest_rel
        if not manifest_path.exists():
            return CommandResult([], 34, "", f"SWR review repair manifest missing: {manifest_rel}")

        self.log_event(
            "swr_review_repair_policy_authorized",
            {"repair_stage_id": stage_id, "authorization_reason": auth_reason, "repair_action": repair_plan.get("repair_action")},
            slice_id=slice_["id"],
        )

        if repair_plan.get("repair_action") == "rerun_review_lane":
            if self.dry_run:
                self.log_event(
                    "dry_run_swr_review_repair_planned",
                    {"repair_stage_id": stage_id, "run_manifest": manifest_rel, "repair_action": "rerun_review_lane"},
                    slice_id=slice_["id"],
                )
                return CommandResult([], 0, "dry run SWR review repair planned", "")
            review_cycle_id, review_dir = self.fresh_swr_repair_cycle(slice_, repair_plan, stage_id)
            fresh = self.assert_fresh_review_repair_dir(review_dir, review_cycle_id)
            if not fresh.ok:
                self.record_failure(
                    slice_["id"],
                    "audit_failure",
                    "high",
                    "SWR review repair directory was contaminated by stale sidecars.",
                    fresh.stderr,
                    review_dir,
                )
                self.mark_slice_status(slice_["id"], "blocked_compile_inputs", reason="SWR review repair directory contaminated")
                return fresh
            self.reset_swr_manifest_for_review_repair(manifest_path, stage_id)
            result = self.run_swr_review_lane(
                slice_,
                manifest_path,
                review_cycle_id=review_cycle_id,
                allow_existing_bundle=False,
            )
            self.log_event(
                "swr_review_repair_lane_passed" if result.ok or result.exit_code == 31 else "swr_review_repair_lane_failed",
                {
                    "repair_stage_id": stage_id,
                    "review_cycle_id": review_cycle_id,
                    "exit_code": result.exit_code,
                    "stderr": result.stderr[-2000:],
                },
                slice_id=slice_["id"],
            )
            if result.ok or result.exit_code == 31:
                self.clear_swr_review_repair(slice_["id"])
            return result

        if repair_plan.get("repair_action") != "rerun_single_stage":
            self.log_event(
                "swr_review_repair_not_runnable",
                {"repair_action": repair_plan.get("repair_action"), "rationale": repair_plan.get("rationale")},
                slice_id=slice_["id"],
            )
            return CommandResult([], 34, "", "SWR review repair plan is not runnable")

        cmd = self.build_swr_stage_rerun_command(repair_plan)
        findings_file = None
        if not self.dry_run:
            findings_file = self.write_swr_corrective_findings(slice_, repair_plan)
        if findings_file is not None:
            # Convergence: the regenerated stage must see why its predecessor
            # was rejected, or nondeterminism can reproduce the same defect.
            cmd.extend(["--reference-context", str(findings_file.relative_to(self.root))])
            self.log_event(
                "swr_stage_rerun_corrective_findings_attached",
                {"findings": str(findings_file.relative_to(self.root)), "repair_stage_id": stage_id},
                slice_id=slice_["id"],
            )
        if self.dry_run:
            self.log_event(
                "dry_run_swr_review_stage_rerun_planned",
                {"command": " ".join(shlex.quote(part) for part in cmd)},
                slice_id=slice_["id"],
            )
            return CommandResult(cmd, 0, "dry run SWR review stage rerun planned", "")
        self.reset_swr_manifest_for_stage_rerun(manifest_path, stage_id)
        result = self.runner.run(cmd, cwd=self.root, timeout=self.po_timeout_seconds())
        self.log_event(
            "swr_review_repair_stage_passed" if result.ok else "swr_review_repair_stage_failed",
            {"repair_stage_id": stage_id, "exit_code": result.exit_code, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]},
            slice_id=slice_["id"],
        )
        if not result.ok:
            if self.swr_wait_timeout_in_progress(result):
                self.record_active_swr_run(slice_, manifest_path, "SWR review repair stage rerun in progress", observed_result=result)
                self.clear_swr_review_repair(slice_["id"])
                return CommandResult(result.argv, 31, result.stdout, "SWR review repair is still in progress; do not relaunch")
            return result
        payload = read_json(manifest_path, {})
        if isinstance(payload, dict) and self.swr_waiting_for_review(payload):
            review_result = self.run_swr_review_lane(
                slice_,
                manifest_path,
                review_cycle_id=self.swr_review_repair_cycle_id(repair_plan, stage_id),
                allow_existing_bundle=False,
            )
            if review_result.ok or review_result.exit_code == 31:
                self.clear_swr_review_repair(slice_["id"])
            return review_result
        self.record_active_swr_run(slice_, manifest_path, "SWR review repair stage rerun in progress")
        self.clear_swr_review_repair(slice_["id"])
        return CommandResult(result.argv, 31, result.stdout, "SWR review repair is still in progress; do not relaunch")

    def recover_revalidated_swr_playbook(
        self, slice_: dict[str, Any], repair_plan: dict[str, Any]
    ) -> CommandResult | None:
        archive_rel = repair_plan.get("rejected_playbook_archive")
        if not isinstance(archive_rel, str) or not archive_rel:
            return None
        archive = self.repo_artifact_path(archive_rel)
        if archive is None or not archive.exists():
            return None

        validation = self.runner.run(
            [
                "python",
                "-m",
                "scripts.validate_playbook_autonomous",
                str(archive),
                "--risk",
                str(slice_.get("risk", "")),
                "--json",
            ],
            cwd=self.root,
            execute_in_dry_run=True,
        )
        self.log_event(
            "swr_validation_repair_archived_playbook_revalidated"
            if validation.ok
            else "swr_validation_repair_archived_playbook_still_rejected",
            {
                "archive": str(archive.relative_to(self.root)),
                "exit_code": validation.exit_code,
                "stdout": validation.stdout[-4000:],
                "stderr": validation.stderr[-4000:],
            },
            slice_id=slice_["id"],
        )
        if not validation.ok:
            return None

        restore_command = [
            "restore-revalidated-swr-playbook",
            str(archive.relative_to(self.root)),
            slice_["playbook"],
        ]
        if self.dry_run:
            return CommandResult(restore_command, 0, "dry run SWR validation repair recovery planned", "")

        playbook = self.root / slice_["playbook"]
        playbook.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(archive, playbook)
        source = repair_plan.get("swr_source") if isinstance(repair_plan.get("swr_source"), dict) else {}
        self.clear_swr_validation_repair(slice_["id"])
        self.record_swr_materialization(slice_, restore_command, source)
        self.mark_slice_status(
            slice_["id"],
            "pending",
            reason="SWR playbook revalidated after validator false-positive fix",
        )
        self.log_event(
            "swr_validation_repair_recovered_without_rerun",
            {
                "archive": str(archive.relative_to(self.root)),
                "playbook": str(playbook.relative_to(self.root)),
                "playbook_sha256": file_sha256(playbook),
                "repair_stage_id": repair_plan.get("repair_stage_id"),
            },
            slice_id=slice_["id"],
        )
        return CommandResult(restore_command, 0, str(playbook.relative_to(self.root)), "")

    def run_swr_review_lane(
        self,
        slice_: dict[str, Any],
        manifest_path: Path,
        *,
        review_cycle_id: str | None = None,
        allow_existing_bundle: bool = True,
    ) -> CommandResult:
        payload = read_json(manifest_path, {})
        if not isinstance(payload, dict):
            return CommandResult([], 32, "", f"invalid SWR manifest: {manifest_path}")
        stage_id = str(payload.get("current_stage_id") or "")
        existing_bundle = self.existing_swr_review_bundle(slice_, payload) if allow_existing_bundle else None
        if existing_bundle is not None:
            bundle_rel = str(existing_bundle.relative_to(self.root))
            self.log_event(
                "swr_stage_review_bundle_reused",
                {"stage_id": stage_id, "review_bundle": bundle_rel},
                slice_id=slice_["id"],
            )
            continue_cmd = self.build_swr_continue_command(manifest_path, bundle_rel)
            continued = self.runner.run(continue_cmd, cwd=self.root, timeout=self.po_timeout_seconds())
            return self.handle_swr_continue_result(slice_, manifest_path, continued)

        session_id, session_result = self.ensure_swr_supervisor_session(slice_, manifest_path)
        if not session_result.ok:
            return session_result
        cycle = review_cycle_id or f"{stage_id}_stage_review"
        review_dir = self.swr_review_dir(slice_, payload, review_cycle_id=cycle)
        if review_cycle_id is None and review_dir.exists() and any(review_dir.rglob("*.json")):
            # A fresh review of a (re)generated artifact must never share a
            # directory with a previous cycle's sidecars and bundle — reviewers
            # see the stale bundle (hashed against the replaced artifact) and
            # correctly block on it. Mint an attempt-suffixed cycle instead.
            for attempt in range(2, 10):
                candidate_cycle = f"{stage_id}_stage_review_c{attempt}"
                candidate_dir = self.swr_review_dir(slice_, payload, review_cycle_id=candidate_cycle)
                if not candidate_dir.exists() or not any(candidate_dir.rglob("*.json")):
                    cycle = candidate_cycle
                    review_dir = candidate_dir
                    self.log_event(
                        "swr_review_cycle_rolled_for_freshness",
                        {"stage_id": stage_id, "review_cycle_id": cycle},
                        slice_id=slice_["id"],
                    )
                    break
        review_dir.mkdir(parents=True, exist_ok=True)
        classify_output = review_dir / "stage_outcome.json"
        classify_cmd = [
            str(self.keel_swr_path()),
            "supervisor",
            "classify",
            "--root",
            str(self.root),
            "--session",
            session_id,
            "--run-dir",
            str(payload.get("run_dir") or manifest_path.parent.relative_to(self.root)),
            "--stage",
            stage_id,
            "--output",
            str(classify_output.relative_to(self.root)),
        ]
        classify = self.runner.run(classify_cmd, cwd=self.root, timeout=self.po_timeout_seconds())
        self.log_event(
            "swr_stage_classified" if classify.ok else "swr_stage_classification_failed",
            {"stage_id": stage_id, "exit_code": classify.exit_code, "stdout": classify.stdout[-2000:], "stderr": classify.stderr[-2000:]},
            slice_id=slice_["id"],
        )
        if not classify.ok:
            return self.swr_review_transport_failure(
                slice_,
                classify_output if classify_output.exists() else manifest_path,
                f"SWR review transport failed: supervisor classify command exited {classify.exit_code}: {classify.stderr[-400:]}",
            )
        outcome = read_json(classify_output, self.parse_json_stdout(classify, {}))
        if not isinstance(outcome, dict) or not outcome.get("reviewable") or not outcome.get("review_bundle_allowed", True):
            failure = self.record_failure(
                slice_["id"],
                "audit_failure",
                "high",
                "SWR stage output was not reviewable for next-stage progression.",
                "Stopped before creating a review bundle or launching the next SWR stage.",
                classify_output if classify_output.exists() else manifest_path,
            )
            self.mark_slice_status(slice_["id"], "blocked_compile_inputs", failure_path=str(failure.relative_to(self.root)), reason="SWR stage review blocked")
            return CommandResult(classify_cmd, 32, "", "SWR stage review blocked")

        job = self.write_swr_stage_review_job(slice_, manifest_path, review_dir, classify_output)
        operator_cmd = [
            str(self.keel_swr_path()),
            "supervisor",
            "invoke-operator",
            "--root",
            str(self.root),
            "--session",
            session_id,
            "--review-cycle",
            cycle,
            "--review-kind",
            "stage_output",
            "--job-json",
            str(job.relative_to(self.root)),
            "--output-dir",
            str((review_dir / "operator").relative_to(self.root)),
        ]
        operator = self.runner.run(operator_cmd, cwd=self.root, timeout=self.po_timeout_seconds())
        if not operator.ok:
            return self.swr_review_transport_failure(
                slice_,
                review_dir / "operator",
                f"SWR review transport failed: supervisor invoke-operator command exited {operator.exit_code}: {operator.stderr[-400:]}",
            )
        operator_payload = self.parse_json_stdout(operator, {})
        operator_review = str(operator_payload.get("operator_review")) if isinstance(operator_payload, dict) else ""
        operator_transport = self.swr_decision_transport_error(operator_review, "operator provisional review")
        if operator_transport:
            evidence = self.repo_artifact_path(operator_review) or (review_dir / "operator")
            return self.swr_review_transport_failure(slice_, evidence, "SWR review transport failed: " + operator_transport)
        operator_errors = self.swr_decision_record_errors(
            operator_review,
            label="operator provisional review",
            expected_actor="operator_codex",
            expected_review_kind="stage_output",
            allowed_approvals={"approve", "approve_with_conditions"},
            payload=payload,
        )
        if operator_errors:
            evidence = self.repo_artifact_path(operator_review) or (review_dir / "operator")
            return self.swr_review_failure(
                slice_,
                evidence,
                "SWR operator review failed closed: " + "; ".join(operator_errors),
                semantic_rejection=self.swr_decision_semantic_rejection(operator_review),
            )

        reviewers_cmd = [
            str(self.keel_swr_path()),
            "supervisor",
            "invoke-reviewers",
            "--root",
            str(self.root),
            "--session",
            session_id,
            "--review-cycle",
            cycle,
            "--review-kind",
            "stage_output",
            "--job-json",
            str(job.relative_to(self.root)),
            "--output-dir",
            str((review_dir / "agents").relative_to(self.root)),
        ]
        reviewers = self.runner.run(reviewers_cmd, cwd=self.root, timeout=self.po_timeout_seconds())
        if not reviewers.ok:
            return self.swr_review_transport_failure(
                slice_,
                review_dir / "agents",
                f"SWR review transport failed: supervisor invoke-reviewers command exited {reviewers.exit_code}: {reviewers.stderr[-400:]}",
            )
        reviewers_payload = self.parse_json_stdout(reviewers, {})
        codex_review = str(reviewers_payload.get("codex_review")) if isinstance(reviewers_payload, dict) else ""
        claude_review = str(reviewers_payload.get("claude_review")) if isinstance(reviewers_payload, dict) else ""
        transport_errors = [
            error
            for error in (
                self.swr_decision_transport_error(codex_review, "Codex reviewer decision"),
                self.swr_decision_transport_error(claude_review, "Claude reviewer decision"),
            )
            if error
        ]
        if transport_errors:
            evidence = self.repo_artifact_path(codex_review) or self.repo_artifact_path(claude_review) or (review_dir / "agents")
            return self.swr_review_transport_failure(
                slice_,
                evidence,
                "SWR review transport failed: " + "; ".join(transport_errors),
            )
        reviewer_errors: list[str] = []
        reviewer_errors.extend(
            self.swr_decision_record_errors(
                codex_review,
                label="Codex reviewer decision",
                expected_actor="codex_review_agent",
                expected_review_kind="stage_output",
                allowed_approvals={"approve", "approve_with_conditions"},
                payload=payload,
            )
        )
        reviewer_errors.extend(
            self.swr_decision_record_errors(
                claude_review,
                label="Claude reviewer decision",
                expected_actor="claude_review_agent",
                expected_review_kind="stage_output",
                allowed_approvals={"approve", "approve_with_conditions"},
                payload=payload,
            )
        )
        if reviewer_errors:
            evidence = self.repo_artifact_path(codex_review) or self.repo_artifact_path(claude_review) or (review_dir / "agents")
            return self.swr_review_failure(
                slice_,
                evidence,
                "SWR independent review failed closed: " + "; ".join(reviewer_errors),
                semantic_rejection=(
                    self.swr_decision_semantic_rejection(codex_review)
                    or self.swr_decision_semantic_rejection(claude_review)
                ),
            )

        consolidated = review_dir / "consolidated_review.json"
        consolidate_cmd = [
            str(self.keel_swr_path()),
            "supervisor",
            "consolidate",
            "--root",
            str(self.root),
            "--session",
            session_id,
            "--review-cycle",
            cycle,
            "--codex-review",
            codex_review,
            "--claude-review",
            claude_review,
            "--operator-review",
            operator_review,
            "--output",
            str(consolidated.relative_to(self.root)),
        ]
        consolidate = self.runner.run(consolidate_cmd, cwd=self.root, timeout=self.po_timeout_seconds())
        if not consolidate.ok:
            return self.swr_review_transport_failure(
                slice_,
                consolidated if consolidated.exists() else review_dir,
                f"SWR review transport failed: supervisor consolidate command exited {consolidate.exit_code}: {consolidate.stderr[-400:]}",
            )
        consolidation_transport = self.swr_decision_transport_error(
            str(consolidated.relative_to(self.root)), "consolidated review"
        )
        if consolidation_transport:
            return self.swr_review_transport_failure(slice_, consolidated, "SWR review transport failed: " + consolidation_transport)
        consolidation_errors = self.swr_decision_record_errors(
            str(consolidated.relative_to(self.root)),
            label="consolidated review",
            expected_actor="consolidation_pass",
            expected_review_kind="consolidation",
            allowed_approvals={"approve", "approve_with_conditions"},
            payload=payload,
            required_next_action="proceed_to_operator_acceptance",
        )
        if consolidation_errors:
            return self.swr_review_failure(
                slice_,
                consolidated,
                "SWR consolidation failed closed: " + "; ".join(consolidation_errors),
                semantic_rejection=self.swr_decision_semantic_rejection(str(consolidated.relative_to(self.root))),
            )

        acceptance = review_dir / "operator_acceptance.json"
        accept_cmd = [
            str(self.keel_swr_path()),
            "supervisor",
            "accept",
            "--root",
            str(self.root),
            "--session",
            session_id,
            "--review-cycle",
            cycle,
            "--consolidated-review",
            str(consolidated.relative_to(self.root)),
            "--output",
            str(acceptance.relative_to(self.root)),
        ]
        accept = self.runner.run(accept_cmd, cwd=self.root, timeout=self.po_timeout_seconds())
        if not accept.ok:
            return self.swr_review_transport_failure(
                slice_,
                acceptance if acceptance.exists() else consolidated,
                f"SWR review transport failed: supervisor accept command exited {accept.exit_code}: {accept.stderr[-400:]}",
            )
        acceptance_transport = self.swr_decision_transport_error(
            str(acceptance.relative_to(self.root)), "operator acceptance"
        )
        if acceptance_transport:
            return self.swr_review_transport_failure(
                slice_, acceptance if acceptance.exists() else consolidated, "SWR review transport failed: " + acceptance_transport
            )
        acceptance_errors = self.swr_decision_record_errors(
            str(acceptance.relative_to(self.root)),
            label="operator acceptance",
            expected_actor="operator_codex",
            expected_review_kind="operator_acceptance",
            allowed_approvals={"approve"},
            payload=payload,
            required_next_action="create_review_bundle",
        )
        if acceptance_errors:
            return self.swr_review_failure(
                slice_,
                acceptance if acceptance.exists() else consolidated,
                "SWR acceptance failed closed: " + "; ".join(acceptance_errors),
                semantic_rejection=self.swr_decision_semantic_rejection(
                    str(acceptance.relative_to(self.root)) if acceptance.exists() else str(consolidated.relative_to(self.root))
                ),
            )

        decision_records = {
            "operator_review": operator_review,
            "codex_review": codex_review,
            "claude_review": claude_review,
            "consolidated_review": str(consolidated.relative_to(self.root)),
            "operator_acceptance": str(acceptance.relative_to(self.root)),
        }
        record_errors = self.swr_stage_review_record_errors(
            operator_review=operator_review,
            codex_review=codex_review,
            claude_review=claude_review,
            consolidated_review=str(consolidated.relative_to(self.root)),
            acceptance_record=str(acceptance.relative_to(self.root)),
            payload=payload,
        )
        if record_errors:
            return self.swr_review_failure(slice_, acceptance, "SWR review records failed closed: " + "; ".join(record_errors))

        files = {
            "operator_review": operator_review,
            "codex_review": codex_review,
            "claude_review": claude_review,
            "consolidated_review": str(consolidated.relative_to(self.root)),
            "operator_acceptance": str(acceptance.relative_to(self.root)),
        }
        reviewer_notes = self.write_swr_reviewer_notes(review_dir, payload, files)
        artifacts = self.swr_stage_artifacts(payload)
        bundle_path = review_dir / f"{stage_id}.review_bundle.json"
        bundle_cmd = [
            str(self.keel_swr_path()),
            "supervisor",
            "create-bundle",
            "--root",
            str(self.root),
            "--session",
            session_id,
            "--output",
            str(bundle_path.relative_to(self.root)),
            "--workflow-id",
            str(payload.get("workflow_id") or ""),
            "--source-stage-id",
            stage_id,
            "--source-run-id",
            str(payload.get("run_id") or ""),
            "--primary-artifact-markdown",
            artifacts["response_markdown"],
            "--response-artifact-json",
            artifacts["response_json"],
            "--reviewer-notes",
            str(reviewer_notes.relative_to(self.root)),
            "--acceptance-record",
            str(acceptance.relative_to(self.root)),
        ]
        bundle = self.runner.run(bundle_cmd, cwd=self.root, timeout=self.po_timeout_seconds())
        self.log_event(
            "swr_stage_review_bundle_created" if bundle.ok else "swr_stage_review_bundle_failed",
            {"stage_id": stage_id, "exit_code": bundle.exit_code, "stdout": bundle.stdout[-2000:], "stderr": bundle.stderr[-2000:]},
            slice_id=slice_["id"],
        )
        if not bundle.ok:
            return bundle
        bundle_payload = self.parse_json_stdout(bundle, {})
        bundle_rel = str(bundle_payload.get("bundle_path") or bundle_path.relative_to(self.root)) if isinstance(bundle_payload, dict) else str(bundle_path.relative_to(self.root))
        hardened_bundle = self.repo_artifact_path(bundle_rel)
        if hardened_bundle is not None and hardened_bundle.exists():
            self.harden_swr_review_bundle(slice_, hardened_bundle, payload, acceptance, decision_records, reviewer_notes)
            if not self.swr_review_bundle_is_valid(hardened_bundle, payload):
                return CommandResult(bundle.argv, 32, "", f"SWR review bundle failed schema/hash validation: {bundle_rel}")
        continue_cmd = self.build_swr_continue_command(manifest_path, bundle_rel)
        continued = self.runner.run(continue_cmd, cwd=self.root, timeout=self.po_timeout_seconds())
        return self.handle_swr_continue_result(slice_, manifest_path, continued)

    def swr_run_abandonment_artifacts(self, slice_id: str) -> list[Path]:
        evidence_dir = self.root / "docs" / "evidence"
        if not evidence_dir.exists():
            return []
        return sorted(evidence_dir.glob(f"{slice_id.lower()}-swr-run-abandonment-*.json"))

    def assert_fresh_swr_launch_sanctioned(self, slice_: dict[str, Any]) -> CommandResult:
        """A fresh SWR launch after prior SWR history requires a recorded decision.

        The June-1 incident escaped a dead-end repair state via an unrecorded
        hand edit that silently converted a bounded same-run repair into an
        unguarded fresh launch. Any fresh launch that follows earlier SWR
        activity for the slice must now cite an explicit abandonment decision
        artifact under docs/evidence/.
        """
        if not bool(self.policy.get("swr", {}).get("require_abandonment_decision_for_relaunch", True)):
            return CommandResult([], 0, "fresh-launch guard disabled by policy", "")
        prior_swr_events = [
            event
            for event in iter_jsonl(self.events_path)
            if event.get("slice") == slice_["id"]
            and str(event.get("event") or "").startswith("swr_")
            and str(event.get("event") or "") not in {"swr_kernel_pinned"}
        ]
        if not prior_swr_events:
            return CommandResult([], 0, "no prior SWR history; fresh launch allowed", "")
        artifacts = self.swr_run_abandonment_artifacts(slice_["id"])
        for artifact in reversed(artifacts):
            payload = read_json(artifact, {})
            if not isinstance(payload, dict):
                continue
            if payload.get("schema_version") != "autokeel.swr_run_abandonment.v1":
                continue
            if payload.get("slice") != slice_["id"]:
                continue
            if payload.get("authorizes_fresh_launch") is True and not payload.get("consumed_at"):
                if not self.dry_run:
                    payload["consumed_at"] = now_iso()
                    write_json_atomic(artifact, payload)
                self.log_event(
                    "swr_fresh_launch_sanctioned_by_abandonment",
                    {"artifact": str(artifact.relative_to(self.root)), "abandoned_run_id": payload.get("abandoned_run_id")},
                    slice_id=slice_["id"],
                )
                return CommandResult([], 0, "fresh launch sanctioned by recorded abandonment decision", "")
        return CommandResult(
            [],
            39,
            "",
            (
                f"fresh SWR launch for {slice_['id']} is not sanctioned: prior SWR history exists "
                "(events) but no unconsumed abandonment decision artifact was found under "
                f"docs/evidence/{slice_['id'].lower()}-swr-run-abandonment-*.json; "
                "record one via 'python scripts/record_intervention.py abandon-swr-run ...' "
                "or restore the prior run and execute its planned repair"
            ),
        )

    def ensure_swr_playbook(self, slice_: dict[str, Any]) -> CommandResult:
        playbook = self.root / slice_["playbook"]
        if self.swr_evidence_matches_playbook(slice_, playbook):
            return CommandResult(["test", "-f", str(self.swr_evidence_path(slice_))], 0, "SWR playbook evidence exists", "")

        leased_manifest, lease_error = self.active_swr_manifest_from_lease(slice_)
        if lease_error is not None:
            return lease_error

        review_repair_plan = slice_.get("swr_review_repair")
        if isinstance(review_repair_plan, dict):
            return self.run_swr_review_repair(slice_, review_repair_plan)

        validation_repair_plan = slice_.get("swr_validation_repair")
        if isinstance(validation_repair_plan, dict):
            return self.run_swr_validation_repair(slice_, validation_repair_plan)

        stored_review_repair = self.plan_stored_swr_review_repair(slice_)
        if stored_review_repair is not None:
            return stored_review_repair

        active_manifest = leased_manifest or self.active_swr_manifest_from_state(slice_) or self.latest_swr_manifest_for_slice(slice_)
        if active_manifest is not None:
            active_payload = read_json(active_manifest, {})
            if isinstance(active_payload, dict):
                history_findings = self.swr_review_history_findings(slice_, active_payload)
                if history_findings:
                    history_errors = [
                        f"{finding.get('stage_id')}: review bundle failed decision-record/hash validation: {finding.get('bundle')}"
                        for finding in history_findings
                    ]
                    return self.swr_review_failure(
                        slice_,
                        active_manifest,
                        "SWR review history failed closed: " + "; ".join(history_errors),
                    )
            if isinstance(active_payload, dict) and self.swr_waiting_for_review(active_payload):
                return self.run_swr_review_lane(slice_, active_manifest)
            if not self.dry_run and self.swr_remote_check_due(slice_):
                resume_cmd = self.build_swr_resume_command(active_manifest)
                result = self.runner.run(resume_cmd, cwd=self.root, timeout=self.po_timeout_seconds())
                self.update_active_swr_remote_check(slice_["id"], result)
                self.log_event(
                    "swr_active_stage_resumed" if result.ok else "swr_active_stage_resume_waiting",
                    {"exit_code": result.exit_code, "stdout": result.stdout[-2000:], "stderr": result.stderr[-2000:]},
                    slice_id=slice_["id"],
                )
                refreshed_payload = read_json(active_manifest, {})
                if isinstance(refreshed_payload, dict) and self.swr_waiting_for_review(refreshed_payload):
                    return self.run_swr_review_lane(slice_, active_manifest)
                if isinstance(refreshed_payload, dict) and self.swr_final_stage_completed(refreshed_payload):
                    swr_source = self.materialize_swr_playbook_from_manifest(slice_, active_manifest)
                    if playbook.exists():
                        self.record_swr_materialization(slice_, result.argv, swr_source)
                        return result
                if not result.ok and not self.swr_wait_timeout_in_progress(result):
                    return result
            self.record_active_swr_run(slice_, active_manifest, "SWR background run in progress")
            return CommandResult(
                ["test", "-f", str(active_manifest)],
                31,
                str(active_manifest.relative_to(self.root)),
                "SWR background run is still in progress; do not relaunch",
            )

        fresh_guard = self.assert_fresh_swr_launch_sanctioned(slice_)
        if not fresh_guard.ok:
            return fresh_guard

        if playbook.exists():
            if self.dry_run:
                self.log_event(
                    "dry_run_non_swr_playbook_archive_skipped",
                    {"playbook": str(playbook.relative_to(self.root))},
                    slice_id=slice_["id"],
                )
            else:
                self.archive_swr_playbook_and_evidence(slice_, "existing playbook lacks matching SWR evidence")

        autoplan_result = self.ensure_autoplan(slice_)
        if not autoplan_result.ok:
            return autoplan_result
        autoplan_head = self.assert_autoplan_tracked_at_head(slice_)
        if not autoplan_head.ok:
            self.log_event(
                "autoplan_head_invariant_failed",
                {"autoplan": slice_.get("autoplan"), "stderr": autoplan_head.stderr},
                slice_id=slice_["id"],
            )
            return autoplan_head

        task_pack = self.materialize_swr_task_pack()
        if not task_pack.ok:
            failure = self.record_failure(
                slice_["id"],
                "compile_failure",
                "high",
                "SWR task pack required for swr_preferred lane is missing.",
                "Blocked S02 before compiler fallback; do not run PO until SWR can generate the playbook.",
                None,
            )
            self.mark_slice_status(slice_["id"], "blocked_compile_inputs", failure_path=str(failure.relative_to(self.root)), reason=task_pack.stderr)
            return task_pack

        provider_preflight = self.strict_swr_provider_preflight(slice_)
        if not provider_preflight.ok:
            evidence = self.write_swr_provider_preflight_evidence(slice_, provider_preflight)
            failure = self.record_failure(
                slice_["id"],
                "provider_auth_failure",
                "high",
                "SWR provider authentication preflight failed before launch.",
                "Blocked the slice before SWR launch. Do not fall back to compiler or fabricate credentials.",
                evidence,
            )
            self.mark_slice_status(
                slice_["id"],
                str(self.policy.get("swr", {}).get("provider_auth_failure_status", "blocked_external")),
                failure_path=str(failure.relative_to(self.root)),
                reason="missing required SWR provider environment",
            )
            return provider_preflight

        cmd = self.build_swr_command(slice_)
        if self.dry_run:
            self.log_event("swr_playbook_generation_planned", {"command": " ".join(shlex.quote(part) for part in cmd)}, slice_id=slice_["id"])
            return CommandResult(cmd, 0, "dry run SWR playbook generation planned", "")

        result = self.runner.run(cmd, cwd=self.root, timeout=self.po_timeout_seconds())
        self.log_event(
            "swr_playbook_generation_passed" if result.ok else "swr_playbook_generation_failed",
            {"exit_code": result.exit_code, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]},
            slice_id=slice_["id"],
        )
        if not result.ok:
            if self.swr_provider_auth_failure(result):
                evidence = self.write_swr_provider_auth_evidence(slice_, cmd, result)
                failure = self.record_failure(
                    slice_["id"],
                    "provider_auth_failure",
                    "high",
                    "SWR playbook generation could not start because OpenAI API credentials were missing.",
                    "Blocked the slice before PO. Do not fall back to compiler or fabricate credentials; provide real local credentials and rerun.",
                    evidence,
                )
                self.mark_slice_status(
                    slice_["id"],
                    "blocked_external",
                    failure_path=str(failure.relative_to(self.root)),
                    reason="missing OPENAI_API_KEY for keel-swr",
                )
                self.log_event(
                    "swr_provider_auth_failed",
                    {
                        "evidence": str(evidence.relative_to(self.root)),
                        "exit_code": result.exit_code,
                        "stderr": result.stderr[-2000:],
                    },
                    slice_id=slice_["id"],
                )
                return CommandResult(cmd, 26, result.stdout, "missing OPENAI_API_KEY for keel-swr")
            if self.swr_wait_timeout_in_progress(result):
                manifest_path = self.extract_swr_manifest_path(result) or self.latest_swr_manifest_for_slice(slice_)
                if manifest_path is not None:
                    self.record_active_swr_run(slice_, manifest_path, "SWR background run in progress", observed_result=result)
                    return CommandResult(
                        cmd,
                        31,
                        str(manifest_path.relative_to(self.root)),
                        "SWR background run is still in progress; do not relaunch",
                    )
            return result

        swr_source: dict[str, str] | None = None
        manifest_path = self.extract_swr_manifest_path(result)
        if manifest_path is not None:
            result_payload = read_json(manifest_path, {})
            if isinstance(result_payload, dict) and self.swr_waiting_for_review(result_payload):
                return self.run_swr_review_lane(slice_, manifest_path)
        if manifest_path is not None and not playbook.exists():
            swr_source = self.materialize_swr_playbook_from_manifest(slice_, manifest_path)

        if not playbook.exists():
            failure = self.record_failure(
                slice_["id"],
                "compile_failure",
                "high",
                "SWR run did not materialize the canonical markdown_playbook_v1 artifact.",
                "Stopped before PO. Continue the SWR review-bundle flow and materialize the Stage 5 output as the canonical playbook.",
                manifest_path or self.root / str(self.policy.get("swr", {}).get("output_root", ".local/autokeel/swr/runs")),
            )
            self.mark_slice_status(slice_["id"], "blocked_compile_inputs", failure_path=str(failure.relative_to(self.root)), reason="SWR playbook not materialized")
            return CommandResult(cmd, 28, result.stdout, "SWR completed without materializing the canonical playbook")

        self.record_swr_materialization(slice_, cmd, swr_source)
        return result

    def ensure_playbook(self, slice_: dict[str, Any]) -> CommandResult:
        playbook = self.root / slice_["playbook"]

        if slice_.get("status") == "replan_required":
            if self.dry_run:
                self.log_event(
                    "dry_run_playbook_archive_skipped",
                    {"playbook": str(playbook.relative_to(self.root))},
                    slice_id=slice_["id"],
                )
            else:
                self.archive_playbook_for_replan(slice_)

        if self.swr_required(slice_):
            return self.ensure_swr_playbook(slice_)

        if playbook.exists():
            return CommandResult(["test", "-f", str(playbook)], 0, "playbook exists", "")

        autoplan_result = self.ensure_autoplan(slice_)
        if not autoplan_result.ok:
            return autoplan_result
        autoplan_head = self.assert_autoplan_tracked_at_head(slice_)
        if not autoplan_head.ok:
            self.log_event(
                "autoplan_head_invariant_failed",
                {"autoplan": slice_.get("autoplan"), "stderr": autoplan_head.stderr},
                slice_id=slice_["id"],
            )
            return autoplan_head

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

    def provider_decisions_exist(self, slice_id: str) -> bool:
        decisions = self.root / "ops/autonomy/decisions"
        if not decisions.exists():
            return False
        for path in decisions.glob("*.json"):
            payload = read_json(path, {})
            if (
                isinstance(payload, dict)
                and payload.get("slice") == slice_id
                and payload.get("schema_version") == "autokeel.provider_evidence_decision.v1"
            ):
                return True
        return False

    def validate_provider_decisions(self, slice_id: str) -> CommandResult:
        if not self.provider_decisions_exist(slice_id):
            return CommandResult([], 0, "no provider decisions for slice", "")
        result = self.runner.run(
            ["python", "scripts/validate_provider_decisions.py", slice_id, "--json"],
            cwd=self.root,
            execute_in_dry_run=True,
        )
        self.log_event(
            "provider_decisions_validated" if result.ok else "provider_decisions_rejected",
            {"exit_code": result.exit_code, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]},
            slice_id=slice_id,
        )
        return result

    def run_slice_phase_commands(self, slice_: dict[str, Any], phase: str) -> CommandResult:
        from scripts.acceptance_policy import command_allowed

        key = f"{phase}_commands"
        commands = slice_.get(key, [])
        if not commands:
            return CommandResult([], 0, f"no {key}", "")

        for command in commands:
            command_text = str(command)
            if not command_allowed(command_text, self.root):
                self.log_event(
                    f"{phase}_command_rejected",
                    {"command": command_text, "reason": "command is not allowlisted"},
                    slice_id=slice_["id"],
                )
                return CommandResult([], 98, "", f"{phase} command is not allowlisted: {command_text}")

            result = self.runner.run(shlex.split(command_text), cwd=self.root, execute_in_dry_run=True)
            self.log_event(
                f"{phase}_command_passed" if result.ok else f"{phase}_command_failed",
                {
                    "command": command_text,
                    "exit_code": result.exit_code,
                    "stdout": result.stdout[-4000:],
                    "stderr": result.stderr[-4000:],
                },
                slice_id=slice_["id"],
            )
            if not result.ok:
                return result

        return CommandResult([], 0, f"{key} passed", "")

    def validate_po_contract_before_start(self, playbook: Path) -> CommandResult:
        contract_env = {"PLAN_ORCHESTRATOR_CLEAN_ENV_CONFIRMED": "1"}
        list_items = self.runner.run(
            self.plan_orchestrator_command(
                "list-items",
                "--playbook",
                str(playbook.relative_to(self.root)),
                "--format",
                "json",
            ),
            cwd=self.root,
            env=contract_env,
            execute_in_dry_run=True,
        )
        if not list_items.ok:
            return list_items
        doctor = self.runner.run(
            self.plan_orchestrator_command(
                "doctor",
                "--playbook",
                str(playbook.relative_to(self.root)),
                "--format",
                "json",
            ),
            cwd=self.root,
            env=contract_env,
            execute_in_dry_run=True,
        )
        if not doctor.ok:
            return doctor
        return CommandResult(list_items.argv + ["&&", *doctor.argv], 0, list_items.stdout, "")

    def run_verify_v1(self) -> CommandResult:
        result = self.runner.run(["python", "-m", "scripts.verify_v1", "--json"], cwd=self.root, execute_in_dry_run=True)
        self.log_event("verify_v1_passed" if result.ok else "verify_v1_failed", {"exit_code": result.exit_code, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]})
        if result.ok:
            state = self.load_state()
            state["v1_complete"] = True
            self.save_state(state)
        return result

    def run_slice_readiness(self, slice_id: str) -> CommandResult:
        command_by_slice = {
            "S04": ["python", "scripts/verify_s04_readiness.py", "--json"],
            "S05": ["python", "scripts/verify_s05_readiness.py", "--json"],
        }
        command = command_by_slice.get(slice_id)
        if command is None:
            return CommandResult([], 0, "no slice-specific readiness gate", "")
        result = self.runner.run(command, cwd=self.root, execute_in_dry_run=True)
        self.log_event(
            "slice_readiness_passed" if result.ok else "slice_readiness_failed",
            {"command": " ".join(command), "exit_code": result.exit_code, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]},
            slice_id=slice_id,
        )
        return result

    def evaluate_tripwires(self) -> CommandResult:
        result = self.runner.run(["python", "-m", "scripts.evaluate_tripwires", "--json"], cwd=self.root, execute_in_dry_run=True)
        self.log_event(
            "tripwires_ok" if result.ok else "tripwires_fired",
            {"exit_code": result.exit_code, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]},
        )
        return result

    def run_autokeel_invariants(self, phase: str) -> CommandResult:
        result = self.runner.run(
            ["python", "-m", "scripts.verify_autokeel_invariants", "--json"],
            cwd=self.root,
            execute_in_dry_run=True,
        )
        self.log_event(
            "autokeel_invariants_ok" if result.ok else "autokeel_invariants_failed",
            {"phase": phase, "exit_code": result.exit_code, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]},
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

    def verify_slice_acceptance(self, slice_id: str, cwd: Path | None = None) -> CommandResult:
        run_cwd = cwd or self.root
        result = self.runner.run(
            ["python", "-m", "scripts.verify_slice", slice_id, "--json"],
            cwd=run_cwd,
            execute_in_dry_run=True,
        )
        self.log_event(
            "slice_acceptance_passed" if result.ok else "slice_acceptance_failed",
            {
                "cwd": str(run_cwd),
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

    def ensure_review_artifacts(self, slice_id: str, run_id: str, cwd: Path | None = None) -> CommandResult:
        slice_ = self.find_slice(slice_id)
        if not slice_:
            return CommandResult([], 60, "", f"unknown slice: {slice_id}")

        artifacts = [str(item) for item in slice_.get("review_artifacts", [])]
        if not artifacts:
            return CommandResult([], 0, "no review artifacts required", "")

        run_cwd = cwd or self.root
        if run_cwd != self.root:
            check = self.runner.run(
                ["python", "-m", "scripts.check_autonomous_review_exists", slice_id, "--json"],
                cwd=run_cwd,
                execute_in_dry_run=True,
            )
            self.log_event(
                "review_artifacts_validated" if check.ok else "review_artifacts_rejected",
                {
                    "cwd": str(run_cwd),
                    "exit_code": check.exit_code,
                    "stdout": check.stdout[-4000:],
                    "stderr": check.stderr[-4000:],
                },
                slice_id=slice_id,
            )
            return check

        missing = [artifact for artifact in artifacts if not (run_cwd / artifact).exists()]
        reviews_policy = self.policy.get("reviews", {})
        if missing and not bool(reviews_policy.get("auto_generate_missing", True)):
            return self.runner.run(
                ["python", "-m", "scripts.check_autonomous_review_exists", slice_id, "--json"],
                cwd=run_cwd,
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
            cwd=run_cwd,
            execute_in_dry_run=True,
        )
        self.log_event(
            "review_artifacts_validated" if check.ok else "review_artifacts_rejected",
            {
                "cwd": str(run_cwd),
                "exit_code": check.exit_code,
                "stdout": check.stdout[-4000:],
                "stderr": check.stderr[-4000:],
            },
            slice_id=slice_id,
        )
        return check

    def load_po_run_state(self, run_id: str) -> dict[str, Any]:
        run_state_path = self.root / ".local/automation/plan_orchestrator/runs" / run_id / "run_state.json"
        if not run_state_path.exists():
            return {}
        try:
            payload = json.loads(run_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}

    def resolve_po_run_branch(self, run_id: str) -> tuple[str, CommandResult]:
        run_state = self.load_po_run_state(run_id)
        candidates = [
            str(run_state.get("run_branch_name") or ""),
            f"orchestrator/run/{run_id}",
        ]
        seen: set[str] = set()
        failures: list[str] = []
        for candidate in candidates:
            if not candidate or candidate in seen:
                continue
            seen.add(candidate)
            verify = self.runner.run(
                ["git", "rev-parse", "--verify", f"{candidate}^{{commit}}"],
                cwd=self.root,
            )
            if verify.ok:
                return candidate, verify
            failures.append(f"{candidate}: {verify.stderr.strip() or verify.stdout.strip()}")
        return "", CommandResult(
            ["git", "rev-parse", "--verify", f"orchestrator/run/{run_id}^{{commit}}"],
            30,
            "",
            "could not resolve PO run branch; " + "; ".join(failures),
        )

    @staticmethod
    def ship_branch_name(slice_id: str) -> str:
        return f"ship/{slice_id.lower()}"

    def with_detached_worktree(self, ref: str, prefix: str, fn: Callable[[Path], CommandResult]) -> CommandResult:
        worktree_root = self.root / ".local" / "autokeel" / "ship-checkouts"
        worktree_root.mkdir(parents=True, exist_ok=True)
        worktree_path = Path(tempfile.mkdtemp(prefix=prefix, dir=worktree_root))
        add = self.runner.run(["git", "worktree", "add", "--detach", str(worktree_path), ref], cwd=self.root)
        if not add.ok:
            shutil.rmtree(worktree_path, ignore_errors=True)
            return add
        try:
            return fn(worktree_path)
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(worktree_path)],
                cwd=self.root,
                text=True,
                capture_output=True,
                check=False,
            )
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)

    def validate_shipped_slice(self, slice_id: str, run_id: str, ship_branch: str) -> tuple[str, CommandResult]:
        safe_prefix = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"{slice_id.lower()}-{run_id}-")
        failed_phase = "worktree"

        def validate(worktree: Path) -> CommandResult:
            nonlocal failed_phase
            failed_phase = "review_artifacts"
            reviews = self.ensure_review_artifacts(slice_id, run_id, cwd=worktree)
            if not reviews.ok:
                return reviews
            failed_phase = "slice_acceptance"
            return self.verify_slice_acceptance(slice_id, cwd=worktree)

        result = self.with_detached_worktree(ship_branch, safe_prefix, validate)
        return failed_phase, result

    def active_run_is_stale(self, active: dict[str, Any]) -> bool:
        observed_at = active.get("last_seen_at") or active.get("started_at")
        if not observed_at:
            return False
        try:
            started = datetime.fromisoformat(str(observed_at))
        except ValueError:
            return False
        stale_minutes = int(self.policy.get("loop", {}).get("stale_run_minutes", 60))
        age_seconds = (datetime.now().astimezone() - started.astimezone()).total_seconds()
        return age_seconds > stale_minutes * 60

    def active_run_playbook_mismatch(self, slice_: dict[str, Any], run_id: str) -> tuple[bool, Path | None, str]:
        run_state_path = self.root / ".local/automation/plan_orchestrator/runs" / run_id / "run_state.json"
        playbook_path = self.root / str(slice_.get("playbook", ""))
        if not run_state_path.exists() or not playbook_path.exists():
            return False, run_state_path if run_state_path.exists() else None, ""
        try:
            run_state = json.loads(run_state_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return False, run_state_path, ""
        snapshot_sha = str(run_state.get("playbook_source_sha256") or "")
        current_sha = file_sha256(playbook_path)
        if snapshot_sha and snapshot_sha != current_sha:
            return True, run_state_path, f"{snapshot_sha} != {current_sha}"
        return False, run_state_path, ""

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
                "--max-wait-seconds",
                str(self.po_supervisor_wait_seconds()),
                "--max-auto-resume-attempts",
                str(self.po_max_auto_resume_attempts()),
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
        self.update_active_run_seen(slice_["id"], str(run_id), result.ok)
        return result

    def update_active_run_seen(self, slice_id: str, run_id: str, resumed: bool) -> None:
        state = self.load_state()
        active = state.get("active_run") or {}
        if active.get("slice") != slice_id or active.get("run_id") != run_id:
            return
        active["last_seen_at"] = now_iso()
        if resumed:
            state["current_slice"] = slice_id
        state["active_run"] = active
        self.save_state(state)

    def resume_active_po(
        self,
        slice_: dict[str, Any],
        active: dict[str, Any],
        *,
        prechecked_status: dict[str, Any] | None = None,
        skip_checkpoint: bool = False,
    ) -> CommandResult:
        run_id = active.get("run_id")
        if not run_id:
            return CommandResult([], 50, "", "cannot resume active PO: missing run_id")

        if not skip_checkpoint:
            checkpoint = self.checkpoint_allowed_pre_po_changes(slice_["id"])
            if not checkpoint.ok:
                self.log_event(
                    "po_resume_precheck_failed",
                    {
                        "run_id": run_id,
                        "exit_code": checkpoint.exit_code,
                        "stdout": checkpoint.stdout[-2000:],
                        "stderr": checkpoint.stderr[-2000:],
                    },
                    slice_id=slice_["id"],
                )
                return checkpoint

        max_auto_resume_attempts = self.po_max_auto_resume_attempts()
        status = prechecked_status or self.inspect_po_status(str(run_id))
        terminal = str(status.get("terminal_state") or status.get("state") or "unknown")
        if terminal == "escalated":
            open_audit_failures = self.open_failures(slice_["id"], "audit_failure", str(run_id))
            if open_audit_failures:
                self.log_event(
                    "po_escalated_resume_blocked_open_failure",
                    {
                        "run_id": run_id,
                        "open_failures": len(open_audit_failures),
                        "required_action": "close audit_failure with local root-cause evidence before one bounded resume",
                    },
                    slice_id=slice_["id"],
                )
                return CommandResult(
                    [],
                    54,
                    "",
                    "cannot resume escalated PO while audit_failure remains open; close it with local evidence after root-cause repair",
                )
            max_auto_resume_attempts = self.po_repaired_escalation_resume_attempts()
            self.log_event(
                "po_escalated_resume_after_repair_authorized",
                {"run_id": run_id, "max_auto_resume_attempts": max_auto_resume_attempts},
                slice_id=slice_["id"],
            )

        checkpoint = self.checkpoint_allowed_pre_po_changes(slice_["id"])
        if not checkpoint.ok:
            self.log_event(
                "po_resume_precheck_failed",
                {
                    "run_id": run_id,
                    "exit_code": checkpoint.exit_code,
                    "stdout": checkpoint.stdout[-2000:],
                    "stderr": checkpoint.stderr[-2000:],
                },
                slice_id=slice_["id"],
            )
            return checkpoint

        result = self.runner.run(
            self.plan_orchestrator_command(
                "supervise",
                "resume",
                "--run-id",
                str(run_id),
                "--max-wait-seconds",
                str(self.po_supervisor_wait_seconds()),
                "--max-auto-resume-attempts",
                str(max_auto_resume_attempts),
            ),
            cwd=self.root,
            env={"PLAN_ORCHESTRATOR_CLEAN_ENV_CONFIRMED": "1"},
            timeout=self.po_timeout_seconds(),
        )
        self.update_active_run_seen(slice_["id"], str(run_id), result.ok)
        self.log_event(
            "po_resumed" if result.ok else "po_resume_failed",
            {
                "run_id": run_id,
                "exit_code": result.exit_code,
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-2000:],
            },
            slice_id=slice_["id"],
        )
        return result

    def recover_passed_slice_run(self, slice_: dict[str, Any]) -> CommandResult | None:
        run_id = str(slice_.get("run_id") or "")
        if not run_id:
            return None
        if self.open_failures(slice_["id"], run_id=run_id):
            self.log_event(
                "po_terminal_recovery_blocked_open_failure",
                {"run_id": run_id, "required_action": "close open failures with local evidence before terminal recovery"},
                slice_id=slice_["id"],
            )
            return None

        status = self.inspect_po_status(run_id)
        terminal = str(status.get("terminal_state") or status.get("state") or "unknown")
        if terminal != "passed":
            return None

        state = self.load_state()
        state["active_run"] = {
            "slice": slice_["id"],
            "run_id": run_id,
            "started_at": now_iso(),
            "recovered_terminal_state": terminal,
        }
        state["current_slice"] = slice_["id"]
        self.save_state(state)
        self.log_event(
            "po_passed_run_recovered",
            {"run_id": run_id, "terminal_state": terminal},
            slice_id=slice_["id"],
        )
        return CommandResult([], 0, json.dumps({"run_id": run_id, "terminal_state": terminal}), "")

    def restore_repaired_escalated_slice_run(self, slice_: dict[str, Any]) -> bool:
        run_id = str(slice_.get("run_id") or "")
        if not run_id:
            return False

        state = self.load_state()
        active = state.get("active_run") or {}
        if active.get("slice") or active.get("run_id"):
            return False

        open_for_run = self.open_failures(slice_["id"], run_id=run_id)
        if open_for_run:
            self.log_event(
                "po_escalated_recovery_blocked_open_failure",
                {"run_id": run_id, "open_failures": len(open_for_run)},
                slice_id=slice_["id"],
            )
            return False

        closed_audit = self.evidence_closed_failures(slice_["id"], "audit_failure", run_id)
        if not closed_audit:
            return False

        status = self.inspect_po_status(run_id)
        terminal = str(status.get("terminal_state") or status.get("state") or "unknown")
        if terminal != "escalated":
            return False

        state["active_run"] = {
            "slice": slice_["id"],
            "run_id": run_id,
            "started_at": now_iso(),
            "last_seen_at": now_iso(),
            "restored_after_failure": "evidence_closed_escalation",
        }
        state["current_slice"] = slice_["id"]
        self.save_state(state)
        self.log_event(
            "po_escalated_run_recovered_for_repaired_resume",
            {"run_id": run_id, "closed_audit_failures": len(closed_audit), "terminal_state": terminal},
            slice_id=slice_["id"],
        )
        return True

    def start_or_resume_po(self, slice_: dict[str, Any]) -> CommandResult:
        state = self.load_state()
        active = state.get("active_run") or {}
        playbook = self.root / slice_["playbook"]
        lane_guard = self.assert_slice_execution_lane(slice_, playbook)
        if not lane_guard.ok:
            self.log_event(
                "po_start_lane_invariant_failed",
                {"exit_code": lane_guard.exit_code, "stderr": lane_guard.stderr},
                slice_id=slice_["id"],
            )
            failure = self.record_failure(
                slice_["id"],
                "state_divergence",
                "high",
                "SWR-required slice reached PO start/resume without matching SWR evidence.",
                "Blocked PO before execution; restore matching keel-swr evidence or re-run SWR.",
                playbook if playbook.exists() else None,
            )
            self.mark_slice_status(slice_["id"], "blocked_compile_inputs", failure_path=str(failure.relative_to(self.root)), reason=lane_guard.stderr)
            return lane_guard
        if active.get("slice") == slice_["id"] and active.get("run_id") and slice_.get("status") == "evidence_ready":
            return self.resume_po_with_evidence(slice_, active)
        if active.get("slice") == slice_["id"] and active.get("run_id"):
            checkpoint = self.checkpoint_allowed_pre_po_changes(slice_["id"])
            if not checkpoint.ok:
                self.log_event(
                    "po_resume_precheck_failed",
                    {
                        "run_id": active.get("run_id"),
                        "exit_code": checkpoint.exit_code,
                        "stdout": checkpoint.stdout[-2000:],
                        "stderr": checkpoint.stderr[-2000:],
                    },
                    slice_id=slice_["id"],
                )
                return checkpoint

            status = self.inspect_po_status(str(active.get("run_id")))
            terminal = str(status.get("terminal_state") or status.get("state") or "unknown")
            if terminal == "passed":
                self.log_event(
                    "po_active_passed_run_detected",
                    {"run_id": active.get("run_id"), "terminal_state": terminal},
                    slice_id=slice_["id"],
                )
                return CommandResult([], 0, json.dumps({"run_id": active.get("run_id"), "terminal_state": terminal}), "")

            mismatch, run_state_path, mismatch_detail = self.active_run_playbook_mismatch(slice_, str(active.get("run_id")))
            if mismatch:
                failure = self.record_failure(
                    slice_["id"],
                    "state_divergence",
                    "medium",
                    "Active PO run was based on a superseded playbook snapshot.",
                    "Cleared active_run so the next launch starts from the current canonical playbook.",
                    run_state_path,
                    run_id=active.get("run_id"),
                )
                self.clear_active_run()
                self.mark_slice_status(
                    slice_["id"],
                    "pending",
                    run_id=active.get("run_id"),
                    failure_path=str(failure.relative_to(self.root)),
                    reason=f"active run playbook snapshot superseded: {mismatch_detail}",
                )
                state = self.load_state()
                active = {}
            else:
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
                return self.resume_active_po(slice_, active, prechecked_status=status, skip_checkpoint=True)

        recovered = self.recover_passed_slice_run(slice_)
        if recovered:
            return recovered

        if not playbook.exists():
            self.log_event("playbook_missing", {"playbook": str(playbook.relative_to(self.root))}, slice_id=slice_["id"])
            return CommandResult([], 3, "", f"missing playbook: {playbook}")
        swr_guard = self.require_swr_evidence_before_po(slice_, playbook)
        if not swr_guard.ok:
            return swr_guard
        failure_guard = self.assert_no_open_high_failures(slice_["id"], "PO start")
        if not failure_guard.ok:
            return failure_guard

        # PO doctor enforces a clean tracked checkout. Commit only the allowed
        # AutoKeel launch artifacts before asking the real PO parser/doctor to
        # accept the playbook contract.
        checkpoint = self.checkpoint_allowed_pre_po_changes(slice_["id"])
        if not checkpoint.ok:
            return checkpoint

        po_contract = self.validate_po_contract_before_start(playbook)
        self.log_event(
            "po_contract_validated" if po_contract.ok else "po_contract_rejected",
            {
                "playbook": str(playbook.relative_to(self.root)),
                "exit_code": po_contract.exit_code,
                "stdout": po_contract.stdout[-4000:],
                "stderr": po_contract.stderr[-4000:],
            },
            slice_id=slice_["id"],
        )
        if not po_contract.ok:
            failure = self.record_failure(
                slice_["id"],
                "compile_failure",
                "high",
                "Plan-orchestrator contract validation failed before PO start.",
                "Blocked PO start; the real PO parser/doctor must pass before execution.",
                playbook,
            )
            self.mark_slice_status(slice_["id"], "blocked_compile_inputs", failure_path=str(failure.relative_to(self.root)), reason="PO contract validation failed")
            return po_contract

        # The contract-validation event itself mutates the AutoKeel log; checkpoint
        # that audit trail before handing control to supervised PO execution.
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
                "--max-wait-seconds",
                str(self.po_supervisor_wait_seconds()),
                "--max-auto-resume-attempts",
                str(self.po_max_auto_resume_attempts()),
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

    def ensure_lane_decision(self, slice_: dict[str, Any]) -> CommandResult:
        lane = slice_.get("lane")
        if lane != "swr_preferred":
            return CommandResult([], 0, "lane decision not required", "")
        policy = self.policy.get("lanes", {})
        mode = policy.get("swr_preferred", "compile_with_decision")
        if mode not in {"compile_with_decision", "use_swr"}:
            return CommandResult([], 0, "lane decision mode not required", "")
        existing = slice_.get("lane_decision")
        if existing or slice_.get("risk") == "high":
            from scripts.lane_decision_policy import validate_lane_decision

            errors = validate_lane_decision(self.root, slice_)
            if not errors:
                return CommandResult(["test", "-f", str(existing)], 0, "lane decision exists", "")

            failure_class = "lane_decision_invalid"
            if not existing or any("missing lane_decision artifact" in err or "artifact missing" in err for err in errors):
                failure_class = "lane_decision_missing"
            description = "; ".join(errors)
            failure = self.record_failure(
                slice_["id"],
                failure_class,
                "high",
                description,
                "Blocked compilation until a reviewed, schema-valid lane_decision artifact exists.",
                self.root / str(existing) if existing else None,
            )
            self.mark_slice_status(
                slice_["id"],
                "blocked_compile_inputs",
                failure_path=str(failure.relative_to(self.root)),
                reason=description[:500],
            )
            return CommandResult([], 25, "", description)
        decision_value = "use_swr" if mode == "use_swr" else "compile_with_keel_compile"
        reason = (
            "SWR-preferred lane is enforced by policy; the staged-workflow-runner must generate the playbook."
            if decision_value == "use_swr"
            else "SWR-preferred lane is preserved as policy metadata; current AutoKeel milestone uses compiler path with explicit downgrade decision."
        )
        decision = self.write_decision(
            f"{slice_['id']}-swr-use" if decision_value == "use_swr" else f"{slice_['id']}-swr-downgrade",
            {
                "status": "accepted",
                "slice": slice_["id"],
                "lane": lane,
                "decision": decision_value,
                "risk": slice_.get("risk", "medium"),
                "review_artifacts": slice_.get("review_artifacts", []),
                "commands": [
                    {
                        "command": "AutoKeel non-high swr_preferred downgrade decision",
                        "exit_code": 0,
                        "stdout_tail": "policy permits non-high swr_preferred compiler-path decision",
                        "stderr_tail": "",
                    }
                ],
                "verdict": "pass",
                "reason": reason,
            },
        )
        self.mark_slice_status(slice_["id"], slice_.get("status", "pending"), lane_decision=str(decision.relative_to(self.root)))
        return CommandResult(["test", "-f", str(decision.relative_to(self.root))], 0, "lane decision created", "")

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

    def required_external_evidence_ready(self, slice_: dict[str, Any]) -> CommandResult:
        if slice_.get("id") != "S03":
            return CommandResult([], 0, "no required external evidence preflight", "")

        result = self.runner.run(
            ["python", "-m", "scripts.verify_s03_readiness", "--json"],
            cwd=self.root,
            execute_in_dry_run=True,
        )
        self.log_event(
            "required_external_evidence_ready" if result.ok else "required_external_evidence_blocked",
            {
                "exit_code": result.exit_code,
                "stdout": result.stdout[-2000:],
                "stderr": result.stderr[-2000:],
            },
            slice_id="S03",
        )
        if result.ok:
            return result

        failure = self.record_failure(
            "S03",
            "blocked_external_missing_evidence",
            "high",
            "Required Oura smoke evidence is missing or blocked.",
            "Blocked S03 before PO execution; did not fabricate provider evidence.",
            None,
        )
        self.mark_slice_status(
            "S03",
            "blocked_external",
            failure_path=str(failure.relative_to(self.root)),
            reason="required Oura evidence unavailable",
        )
        return result

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

            ship_branch = self.ship_branch_name(slice_id)
            failed_phase, shipped_validation = self.validate_shipped_slice(slice_id, run_id, ship_branch)
            if not shipped_validation.ok:
                failure_class = "review_artifact_invalid" if failed_phase != "slice_acceptance" else "agent_false_done"
                description = (
                    "PO passed but required autonomous review artifacts were missing or invalid on the shipped branch."
                    if failure_class == "review_artifact_invalid"
                    else "PO passed but shipped branch slice acceptance verification failed."
                )
                action_taken = (
                    "Rejected completion; shipped branch reviewer artifacts must pass validation before slice acceptance."
                    if failure_class == "review_artifact_invalid"
                    else "Rejected completion; shipped branch requires remediation."
                )
                failure = self.record_failure(
                    slice_id,
                    failure_class,
                    "high",
                    description,
                    action_taken,
                    None,
                    run_id=run_id,
                )
                self.mark_slice_status(slice_id, "replan_required", run_id=run_id, failure_path=str(failure.relative_to(self.root)))
                return "review_failed" if failure_class == "review_artifact_invalid" else "acceptance_failed"

            ship_commit_result = self.runner.run(["git", "rev-parse", f"{ship_branch}^{{commit}}"], cwd=self.root)
            ship_commit = ship_commit_result.stdout.strip() if ship_commit_result.ok else ""

            self.mark_slice_status(
                slice_id,
                "complete",
                run_id=run_id,
                completed_at=now_iso(),
                ship_branch=ship_branch,
                ship_commit=ship_commit,
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
                "Recorded escalation for root-cause diagnosis; keep the active PO run for supervised resume after the fix.",
                None,
                run_id=run_id,
            )
            self.mark_slice_status(slice_id, "pending", run_id=run_id, failure_path=str(failure.relative_to(self.root)))
            return "escalated"

        if terminal == "unknown" and bool(self.policy.get("loop", {}).get("stop_on_unknown_terminal_state", True)):
            failure = self.record_failure(
                slice_id,
                "unknown_terminal_state",
                "high",
                f"PO reported unknown or non-terminal state: {terminal}.",
                "Stopped AutoKeel instead of converting an unknown terminal state into a generic replan.",
                None,
                run_id=run_id,
            )
            self.mark_slice_status(slice_id, "blocked", run_id=run_id, failure_path=str(failure.relative_to(self.root)), reason=f"unknown terminal state: {terminal}")
            return "unknown_terminal_state"

        return "live"

    def create_external_evidence_request(self, slice_id: str, run_id: str, status: dict[str, Any]) -> Path:
        evidence_dir = self.root / "private" / "evidence" / slice_id / f"{slug_ts()}-{run_id}"
        evidence_dir.mkdir(parents=True, exist_ok=True)
        for path in (
            self.root / "private",
            self.root / "private" / "evidence",
            self.root / "private" / "evidence" / slice_id,
            evidence_dir,
        ):
            if path.exists():
                os.chmod(path, 0o700)
        readme = evidence_dir / "README.md"
        readme.write_text(
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
        os.chmod(readme, 0o600)
        self.log_event("evidence_request_created", {"path": str(evidence_dir.relative_to(self.root))}, slice_id=slice_id)
        return evidence_dir

    def failure_origin_for_class(self, failure_class: str) -> str:
        mapping = {
            "provider_auth_failure": "external_provider",
            "blocked_external_missing_evidence": "external_provider",
            "audit_failure": "po_kernel",
            "manual_gate_leak": "po_kernel",
            "state_divergence": "autokeel_wrapper",
            "ship_failure": "autokeel_wrapper",
            "review_artifact_invalid": "validator",
            "compile_failure": "validator",
            "unsafe_write_root": "validator",
            "swr_evidence_missing": "autokeel_wrapper",
            "lane_decision_missing": "autokeel_wrapper",
            "lane_decision_invalid": "autokeel_wrapper",
        }
        return mapping.get(failure_class, "autokeel_wrapper")

    def record_failure(
        self,
        slice_id: str,
        failure_class: str,
        severity: str,
        description: str,
        action_taken: str,
        evidence_path: Path | None,
        run_id: str | None = None,
        repair_scope: str | None = None,
    ) -> Path:
        failure_dir = self.root / "ops" / "autonomy" / "failures"
        failure_dir.mkdir(parents=True, exist_ok=True)
        failure_file = failure_dir / f"{slice_id}-{failure_class}-{slug_ts()}-{uuid.uuid4().hex[:8]}.md"
        evidence_text = str(evidence_path.relative_to(self.root)) if evidence_path and evidence_path.is_absolute() else str(evidence_path or "")
        root_cause_id = f"{slice_id}-{failure_class}".upper().replace("_", "-")
        failure_origin = self.failure_origin_for_class(failure_class)
        failure_file.write_text(
            f"# {slice_id} {failure_class}\n\n"
            f"- Timestamp: {now_iso()}\n"
            f"- Severity: {severity}\n"
            f"- Run ID: {run_id or ''}\n"
            f"- Evidence: {evidence_text}\n\n"
            f"- Root Cause ID: {root_cause_id}\n"
            f"- Failure Origin: {failure_origin}\n\n"
            f"## Description\n\n{description}\n\n"
            f"## Action Taken\n\n{action_taken}\n",
            encoding="utf-8",
        )
        payload = {
            "schema_version": "autokeel.failure_ledger.v2",
            "ts": now_iso(),
            "slice": slice_id,
            "run_id": run_id,
            "failure_class": failure_class,
            "severity": severity,
            "description": description,
            "action_taken": action_taken,
            "evidence_path": evidence_text or str(failure_file.relative_to(self.root)),
            "evidence_sha256": file_sha256(evidence_path) if evidence_path is not None and evidence_path.is_file() else None,
            "root_cause_id": root_cause_id,
            "failure_origin": failure_origin,
            "supersedes": [],
            "superseded_by": None,
            "false_positive": False,
            "closure_validation_command": "",
            "open": True,
        }
        if repair_scope:
            payload["repair_scope"] = repair_scope
        append_jsonl(self.failure_path, redact(payload))
        self.log_event("failure_recorded", payload, slice_id=slice_id)
        return failure_file

    def ship_slice(self, slice_id: str, run_id: str) -> CommandResult:
        branch = self.ship_branch_name(slice_id)
        slice_ = self.find_slice(slice_id)
        if slice_ is None:
            return CommandResult([], 60, "", f"unknown slice: {slice_id}")
        lane_guard = self.assert_slice_execution_lane(slice_, self.root / str(slice_.get("playbook", "")))
        if not lane_guard.ok:
            self.log_event(
                "ship_lane_invariant_failed",
                {"run_id": run_id, "exit_code": lane_guard.exit_code, "stderr": lane_guard.stderr},
                slice_id=slice_id,
            )
            return lane_guard
        provider_guard = self.validate_provider_decisions(slice_id)
        if not provider_guard.ok:
            return provider_guard
        pre_ship = self.run_slice_phase_commands(slice_, "pre_ship")
        if not pre_ship.ok:
            self.log_event(
                "slice_ship_failed",
                {
                    "run_id": run_id,
                    "ship_branch": branch,
                    "reason": "pre_ship_policy_command_failed",
                    "exit_code": pre_ship.exit_code,
                    "stdout": pre_ship.stdout[-2000:],
                    "stderr": pre_ship.stderr[-2000:],
                },
                slice_id=slice_id,
            )
            return pre_ship
        retarget_guard = self.verify_run_retarget_evidence(slice_id)
        if not retarget_guard.ok:
            return retarget_guard
        failure_guard = self.assert_no_open_high_failures(slice_id, "ship")
        if not failure_guard.ok:
            return failure_guard
        run_branch, verify = self.resolve_po_run_branch(run_id)

        if not verify.ok:
            self.log_event(
                "slice_ship_failed",
                {"run_id": run_id, "run_branch": run_branch, "stderr": verify.stderr[-2000:]},
                slice_id=slice_id,
            )
            return verify

        checkpoint = self.checkpoint_allowed_pre_po_changes(slice_id)
        if not checkpoint.ok:
            self.log_event(
                "slice_ship_failed",
                {
                    "run_id": run_id,
                    "run_branch": run_branch,
                    "ship_branch": branch,
                    "reason": "pre_ship_checkpoint_failed",
                    "exit_code": checkpoint.exit_code,
                    "stdout": checkpoint.stdout[-2000:],
                    "stderr": checkpoint.stderr[-2000:],
                },
                slice_id=slice_id,
            )
            return checkpoint

        before_branch = self.runner.run(["git", "branch", "--show-current"], cwd=self.root)
        checkout = self.runner.run(["git", "branch", "-f", branch, run_branch], cwd=self.root)
        after_branch = self.runner.run(["git", "branch", "--show-current"], cwd=self.root) if checkout.ok else before_branch
        ship_commit = ""
        if checkout.ok:
            head = self.runner.run(["git", "rev-parse", f"{branch}^{{commit}}"], cwd=self.root)
            ship_commit = head.stdout.strip() if head.ok else ""
        self.log_event(
            "slice_ship_branch_created" if checkout.ok else "slice_ship_failed",
            {
                "run_id": run_id,
                "run_branch": run_branch,
                "ship_branch": branch,
                "ship_commit": ship_commit,
                "operator_branch_before": before_branch.stdout.strip() if before_branch.ok else "",
                "operator_branch_after": after_branch.stdout.strip() if after_branch.ok else "",
                "exit_code": checkout.exit_code,
                "stdout": checkout.stdout[-2000:],
                "stderr": checkout.stderr[-2000:],
            },
            slice_id=slice_id,
        )
        return checkout

    def run_once(self, requested_slice: str | None = None, force_slice: bool = False) -> int:
        if not self.dry_run:
            return self._run_once_with_end_invariants(requested_slice=requested_slice, force_slice=force_slice)

        snapshots = self.snapshot_dry_run_state()
        try:
            return self._run_once_with_end_invariants(requested_slice=requested_slice, force_slice=force_slice)
        finally:
            self.restore_dry_run_state(snapshots)

    def _run_once_with_end_invariants(self, requested_slice: str | None = None, force_slice: bool = False) -> int:
        code = self._run_once_impl(requested_slice=requested_slice, force_slice=force_slice)
        end_invariants = self.run_autokeel_invariants("end")
        if code == 0 and not end_invariants.ok:
            return end_invariants.exit_code or 38
        return code

    def run_po_and_handle_status(self, slice_: dict[str, Any], run: CommandResult) -> int:
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
        end_invariants = self.run_autokeel_invariants("end")
        if not end_invariants.ok:
            return end_invariants.exit_code or 38
        return 0

    def _run_once_impl(self, requested_slice: str | None = None, force_slice: bool = False) -> int:
        self.log_heartbeat()

        digest_check = self.verify_state_digest()
        if not digest_check.ok:
            failure = self.record_failure(
                "GLOBAL",
                "state_divergence",
                "high",
                "Control-plane state was modified outside sanctioned AutoKeel writers.",
                "Stopped the tick before any slice work; the out-of-band change must be ratified with recorded evidence.",
                self.autonomy_dir / "state_digest.json",
            )
            self.log_event(
                "state_digest_divergence",
                {"stderr": digest_check.stderr, "failure_path": str(failure.relative_to(self.root))},
            )
            return digest_check.exit_code or 39

        invariants = self.run_autokeel_invariants("start")
        if not invariants.ok:
            return invariants.exit_code or 38

        verify = self.run_verify_v1()
        if verify.ok:
            self.log_event("v1_complete", {})
            end_invariants = self.run_autokeel_invariants("end")
            if not end_invariants.ok:
                return end_invariants.exit_code or 38
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
        phase = self.swr_execution_phase(slice_)
        self.log_event(
            "slice_execution_phase_determined",
            {"phase": phase, "status": slice_.get("status")},
            slice_id=slice_["id"],
        )

        if phase == "swr_repair_malformed":
            # Sanctioned exit from the dead end: if a recorded abandonment
            # decision covers this repair's run, archive the broken plan and
            # requeue the slice instead of blocking forever.
            repair = slice_.get("swr_review_repair") if isinstance(slice_.get("swr_review_repair"), dict) else {}
            abandonment = next(
                (
                    artifact
                    for artifact in reversed(self.swr_run_abandonment_artifacts(slice_["id"]))
                    if isinstance(read_json(artifact, None), dict)
                    and read_json(artifact, {}).get("schema_version") == "autokeel.swr_run_abandonment.v1"
                    and str(read_json(artifact, {}).get("abandoned_run_id") or "") == str(repair.get("run_id") or "")
                ),
                None,
            )
            if abandonment is not None:
                slices = self.load_slices()
                for item in slices:
                    if item.get("id") != slice_["id"]:
                        continue
                    abandoned = item.setdefault("swr_review_repair_abandoned", [])
                    if isinstance(abandoned, list):
                        abandoned.append({**repair, "abandoned_at": now_iso(), "abandonment_evidence": str(abandonment.relative_to(self.root))})
                    for stale_key in ("swr_review_repair", "swr_run_id", "swr_run_manifest"):
                        item.pop(stale_key, None)
                    item["updated_at"] = now_iso()
                    break
                self.save_slices(slices)
                self.log_event(
                    "swr_review_repair_abandoned_by_decision",
                    {"abandonment_evidence": str(abandonment.relative_to(self.root)), "abandoned_run_id": repair.get("run_id")},
                    slice_id=slice_["id"],
                )
                self.mark_slice_status(slice_["id"], "pending")
                return 0
            self.record_failure(
                slice_["id"],
                "state_divergence",
                "high",
                "SWR review repair plan is malformed or references a missing manifest.",
                (
                    "Stopped before launch/resume. Sanctioned exits: restore the run directory the plan references, "
                    "or record an abandonment decision via 'python scripts/record_intervention.py abandon-swr-run' "
                    "and rerun; never hand-edit slices.json."
                ),
                self.root / "ops/autonomy/slices.json",
            )
            self.mark_slice_status(slice_["id"], "blocked", reason="malformed SWR review repair plan")
            return 36

        if phase == "swr_quarantined" and not isinstance(slice_.get("swr_review_repair"), dict):
            self.record_failure(
                slice_["id"],
                "audit_failure",
                "high",
                "SWR run is quarantined without a planned repair.",
                "Stopped before launch/resume; create a bounded repair plan or abandon with evidence.",
                self.root / "ops/autonomy/slices.json",
            )
            self.mark_slice_status(slice_["id"], "blocked_compile_inputs", reason="SWR run quarantined without repair plan")
            return 32

        budgets = self.failure_budget_exceeded(slice_)
        if not budgets.ok:
            failure = self.record_failure(
                slice_["id"],
                "failure_budget_exceeded",
                "high",
                budgets.stderr,
                "Stopped the AutoKeel tick instead of converting repeated failures into another generic replan.",
                None,
            )
            self.mark_slice_status(slice_["id"], "blocked", failure_path=str(failure.relative_to(self.root)), reason=budgets.stderr)
            return budgets.exit_code or 37
        recovered = self.recover_passed_slice_run(slice_)
        if recovered:
            run_id = self._extract_run_id(recovered.stdout)
            if not run_id:
                self.log_event("po_run_id_missing", {"stdout": recovered.stdout, "stderr": recovered.stderr}, slice_id=slice_["id"])
                return 5
            status = self.inspect_po_status(run_id)
            self.handle_po_status(slice_["id"], run_id, status)
            return 0

        self.restore_repaired_escalated_slice_run(slice_)

        state = self.load_state()
        active = state.get("active_run") or {}
        if active.get("slice") == slice_["id"] and active.get("run_id"):
            # Active PO runs are bound to the playbook snapshot captured at
            # launch. Resume them before lane/evidence/compiler steps can
            # rewrite the canonical playbook and create false divergence.
            run = self.start_or_resume_po(slice_)
            return self.run_po_and_handle_status(slice_, run)

        if self.should_run_slice_readiness(slice_):
            readiness = self.run_slice_readiness(slice_["id"])
            if not readiness.ok:
                self.record_failure(
                    slice_["id"],
                    "audit_failure",
                    "high",
                    "Slice readiness gate failed before launch.",
                    "Stopped before lane/evidence/compiler work; readiness must pass before this slice can start.",
                    self.root / "docs/briefs",
                )
                self.mark_slice_status(slice_["id"], "blocked", reason="slice readiness failed")
                return readiness.exit_code or 35
        else:
            self.log_event(
                "slice_readiness_skipped_existing_swr_phase",
                {"phase": phase},
                slice_id=slice_["id"],
            )

        if phase in {"active_swr_resume", "swr_review_repair"}:
            manifest_rel = str(
                (slice_.get("swr_review_repair") or {}).get("run_manifest")
                if isinstance(slice_.get("swr_review_repair"), dict)
                else slice_.get("swr_run_manifest")
            )
            if manifest_rel:
                result = self.runner.run(
                    ["python", "scripts/verify_swr_review_history.py", manifest_rel, "--json"],
                    cwd=self.root,
                    execute_in_dry_run=True,
                )
                if not result.ok:
                    self.record_failure(
                        slice_["id"],
                        "audit_failure",
                        "high",
                        "SWR review history failed closed before resume.",
                        result.stderr[-2000:] or result.stdout[-2000:],
                        self.root / manifest_rel,
                    )
                    self.mark_slice_status(slice_["id"], "blocked_compile_inputs", reason="SWR review history failed closed")
                    return 32

        lane_decision = self.ensure_lane_decision(slice_)
        if not lane_decision.ok:
            self.log_event(
                "waiting_for_lane_decision",
                {"exit_code": lane_decision.exit_code, "stderr": lane_decision.stderr[-2000:]},
                slice_id=slice_["id"],
            )
            return lane_decision.exit_code or 2
        required_evidence = self.required_external_evidence_ready(slice_)
        if not required_evidence.ok:
            return required_evidence.exit_code or 4
        if slice_.get("lane") == "compiler_external_evidence":
            self.run_optional_evidence(slice_)

        compiled = self.ensure_playbook(slice_)
        if not compiled.ok:
            self.log_event(
                "waiting_for_playbook_or_compile_inputs",
                {"exit_code": compiled.exit_code, "stderr": compiled.stderr[-2000:]},
                slice_id=slice_["id"],
            )
            if compiled.exit_code == 31:
                return 0
            # 33 = typed SWR review transport failure: already recorded with its
            # own class, repair plan, and control-plane scope by
            # swr_review_transport_failure; never double-record it as a generic
            # compile failure.
            if compiled.exit_code not in {20, 21, 22, 24, 26, 27, 28, 31, 32, 33, 34}:
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
            if self.swr_required(slice_):
                return self.handle_swr_playbook_validation_failure(slice_, validation)
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

        if (
            self.swr_required(slice_)
            and (
                slice_["id"] in self._swr_materializations
                or not self.swr_evidence_matches_playbook(slice_, self.root / slice_["playbook"])
            )
        ):
            self.write_swr_playbook_evidence(slice_, validation)

        provider_decisions = self.validate_provider_decisions(slice_["id"])
        if not provider_decisions.ok:
            self.record_failure(
                slice_["id"],
                "audit_failure",
                "high",
                "Provider decision validation failed before PO start.",
                "Stopped before PO; provider decisions must be schema-valid and non-conflicting.",
                self.root / "ops/autonomy/decisions",
            )
            self.mark_slice_status(slice_["id"], "replan_required", reason="provider decision validation failed")
            return provider_decisions.exit_code or 35

        pre_po = self.run_slice_phase_commands(slice_, "pre_po")
        if not pre_po.ok:
            self.record_failure(
                slice_["id"],
                "audit_failure",
                "high",
                "Slice pre-PO policy command failed.",
                "Stopped before PO execution; pre-PO policy commands must pass.",
                self.root / "ops/autonomy/slices.json",
            )
            self.mark_slice_status(slice_["id"], "replan_required", reason="pre-PO policy command failed")
            return pre_po.exit_code or 35

        if self.dry_run:
            self.log_event("dry_run_po_start_skipped", {"playbook": slice_.get("playbook")}, slice_id=slice_["id"])
            return 0

        run = self.start_or_resume_po(slice_)
        return self.run_po_and_handle_status(slice_, run)

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
    parser.add_argument("--strict-swr", metavar="SLICE_ID", help="With --doctor, run strict SWR provider preflight for a slice before selection.")
    parser.add_argument("--readiness", choices=["S02", "S03", "S04", "S05"], help="Run a slice-specific pre-launch readiness check and exit.")
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

    read_only_invocation = bool(args.doctor or args.status or args.next_slice or args.replay_events)
    if read_only_invocation:
        # Diagnostics must not create or contend on the run lock; a lock file's
        # presence should always mean a mutating AutoKeel process ran.
        return _main_dispatch(args, root)

    with AutoKeelLock(lock_path):
        return _main_dispatch(args, root)


def _main_dispatch(args: argparse.Namespace, root: Path) -> int:
    autokeel = AutoKeel(root=root, dry_run=args.dry_run)
    if args.doctor:
        cmd = ["python", "-m", "scripts.verify_autonomy_preflight", "--json"]
        if args.strict:
            cmd.extend(["--strict-tools", "--strict-clean"])
        result = autokeel.runner.run(cmd, cwd=root, execute_in_dry_run=True)
        if args.strict_swr:
            slice_ = autokeel.find_slice(args.strict_swr)
            if slice_ is None:
                print(json.dumps({"status": "error", "errors": [f"unknown slice: {args.strict_swr}"]}, indent=2, sort_keys=True))
                return 1
            swr = autokeel.strict_swr_provider_preflight(slice_)
            try:
                preflight_payload = json.loads(result.stdout) if result.stdout.strip() else {}
            except json.JSONDecodeError:
                preflight_payload = {"status": "error", "errors": ["verify_autonomy_preflight emitted invalid JSON"]}
            swr_payload = autokeel.parse_json_stdout(swr, {})
            errors = list(preflight_payload.get("errors", []) if isinstance(preflight_payload, dict) else [])
            if not swr.ok:
                errors.append(swr.stderr or "strict SWR provider preflight failed")
            combined = {
                "status": "ok" if result.ok and swr.ok and not errors else "error",
                "errors": errors,
                "warnings": preflight_payload.get("warnings", []) if isinstance(preflight_payload, dict) else [],
                "checks": preflight_payload.get("checks", {}) if isinstance(preflight_payload, dict) else {},
                "strict_swr": swr_payload,
            }
            print(json.dumps(combined, indent=2, sort_keys=True))
            return 0 if combined["status"] == "ok" else 1
        print(result.stdout, end="")
        if result.stderr:
            print(result.stderr, file=sys.stderr, end="")
        return result.exit_code
    if args.readiness == "S02":
        from scripts.verify_s02_readiness import verify_s02_readiness

        report = verify_s02_readiness(root)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["status"] == "ok" else 1
    if args.readiness == "S03":
        from scripts.verify_s03_readiness import verify_s03_readiness

        report = verify_s03_readiness(root)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["status"] == "ok" else 1
    if args.readiness == "S04":
        from scripts.verify_s04_readiness import verify_s04_readiness

        report = verify_s04_readiness(root)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["status"] == "ok" else 1
    if args.readiness == "S05":
        from scripts.verify_s05_readiness import verify_s05_readiness

        report = verify_s05_readiness(root)
        print(json.dumps(report, indent=2, sort_keys=True))
        return 0 if report["status"] == "ok" else 1
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

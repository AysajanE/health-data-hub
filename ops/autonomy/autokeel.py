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
import subprocess
import sys
import time
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

        payload = yaml.safe_load(text) or {}
    except Exception:
        payload = _simple_yaml_load(text) or {}
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
    ) -> CommandResult:
        self.assert_allowed(argv)
        if self.dry_run and not execute_in_dry_run:
            return CommandResult(argv=argv, exit_code=0, stdout='{"dry_run": true}', stderr="")
        proc = subprocess.run(
            argv,
            cwd=str(cwd or self.root),
            env={**os.environ, **(env or {})},
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.timeout,
            check=False,
        )
        return CommandResult(argv=argv, exit_code=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


class AutoKeel:
    ACTIONABLE_STATUSES = {"pending", "waiting_for_playbook", "replan_required", "evidence_ready"}
    BLOCKED_STATUSES = {"blocked", "blocked_external", "blocked_external_waiting_for_evidence", "blocked_compile_inputs", "complete"}

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

    def choose_next_slice(self, requested: str | None = None) -> dict[str, Any] | None:
        slices = self.load_slices()

        if requested:
            for slice_ in slices:
                if slice_.get("id") == requested or slice_.get("slug") == requested:
                    if slice_.get("status") == "complete":
                        return None
                    return slice_
            raise AutoKeelError(f"Unknown slice: {requested}")

        completed = {item.get("id") for item in slices if item.get("status") == "complete"}

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
            if slice_.get("id") == slice_id:
                if status == "replan_required":
                    retry_count = int(slice_.get("retry_count") or 0) + 1
                    slice_["retry_count"] = retry_count
                    max_retries = int(self.policy.get("loop", {}).get("max_retries_per_slice_before_replan", 3))
                    if retry_count >= max_retries:
                        status = "blocked"
                        extra.setdefault("reason", "retry cap exceeded")
                slice_["status"] = status
                slice_["updated_at"] = now_iso()
                slice_.update(extra)
                found = True
                break
        if not found:
            raise AutoKeelError(f"Unknown slice: {slice_id}")
        self.save_slices(slices)
        state = self.load_state()
        if status == "complete":
            completed = list(dict.fromkeys([*state.get("completed_slices", []), slice_id]))
            state["completed_slices"] = completed
            if state.get("current_slice") == slice_id:
                state["current_slice"] = None
            if (state.get("active_run") or {}).get("slice") == slice_id:
                state["active_run"] = None
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

    def ensure_playbook(self, slice_: dict[str, Any]) -> CommandResult:
        playbook = self.root / slice_["playbook"]
        if playbook.exists():
            return CommandResult(["test", "-f", str(playbook)], 0, "playbook exists", "")

        autoplan_rel = slice_.get("autoplan")
        if not autoplan_rel:
            self.log_event(
                "compile_inputs_missing",
                {"missing": "autoplan", "playbook": slice_.get("playbook")},
                slice_id=slice_["id"],
            )
            self.mark_slice_status(slice_["id"], "blocked_compile_inputs", reason="missing autoplan path")
            return CommandResult([], 20, "", "missing autoplan path in slices.json")

        autoplan = self.root / autoplan_rel
        brief = self.root / slice_["brief"]

        if not autoplan.exists():
            self.log_event(
                "compile_inputs_missing",
                {"missing": str(autoplan.relative_to(self.root)), "playbook": slice_.get("playbook")},
                slice_id=slice_["id"],
            )
            self.mark_slice_status(slice_["id"], "blocked_compile_inputs", reason=f"missing {autoplan_rel}")
            return CommandResult([], 21, "", f"missing autoplan: {autoplan}")

        design_rel = slice_.get("design_doc") or self.policy.get("compile", {}).get("design_doc") or "docs/gstack/health-data-hub-office-hours.md"
        design = self.root / design_rel
        if not design.exists():
            fallback = self.root / "aeziz-local-AysajanE-health-data-hub-design-20260515-114138.md"
            if fallback.exists():
                design = fallback
            else:
                self.mark_slice_status(slice_["id"], "blocked_compile_inputs", reason=f"missing design doc {design_rel}")
                return CommandResult([], 22, "", f"missing design doc: {design}")

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
            os.environ.get("KEEL_PO_ROOT", str(Path(self.policy.get("keel_root", "/Users/aeziz-local/keel")) / "plan-orchestrator")),
            "--human-approved-by",
            "AUTO-KEEL-AUTONOMOUS-NOT-HUMAN",
        ]

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
            ["python", "-m", "scripts.validate_playbook_autonomous", str(playbook), "--json"],
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

    def start_or_resume_po(self, slice_: dict[str, Any]) -> CommandResult:
        state = self.load_state()
        active = state.get("active_run") or {}
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
        result = self.runner.run(
            [
                str(Path(self.policy.get("keel_root", "/Users/aeziz-local/keel")) / "bin" / "keel-run"),
                "supervise",
                "run",
                "--playbook",
                str(playbook.relative_to(self.root)),
                "--next",
            ],
            cwd=self.root,
            env={"PLAN_ORCHESTRATOR_CLEAN_ENV_CONFIRMED": "1"},
        )
        run_id = self._extract_run_id(result.stdout) or self._extract_run_id(result.stderr)
        if run_id:
            state["active_run"] = {"slice": slice_["id"], "run_id": run_id, "started_at": now_iso()}
            state["current_slice"] = slice_["id"]
            history = state.setdefault("run_history", [])
            history.append({"slice": slice_["id"], "run_id": run_id, "started_at": now_iso()})
            self.save_state(state)
        self.log_event("po_started" if result.ok else "po_start_failed", {"exit_code": result.exit_code, "run_id": run_id, "stdout": result.stdout[-4000:], "stderr": result.stderr[-4000:]}, slice_id=slice_["id"])
        return result

    def inspect_po_status(self, run_id: str) -> dict[str, Any]:
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
        failure_file = failure_dir / f"{slice_id}-{failure_class}-{slug_ts()}.md"
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

    def run_once(self, requested_slice: str | None = None) -> int:
        self.log_heartbeat()

        verify = self.run_verify_v1()
        if verify.ok:
            self.log_event("v1_complete", {})
            return 0

        tripwires = self.evaluate_tripwires()
        if not tripwires.ok:
            self.record_failure(
                "GLOBAL",
                "tripwire_failure",
                "high",
                "One or more AutoKeel tripwires fired.",
                "Stopped the supervisor before more autonomous work.",
                None,
            )
            return 6

        slice_ = self.choose_next_slice(requested_slice)
        if not slice_:
            self.log_event("no_actionable_slice", {})
            return 1

        self.ensure_slice_brief(slice_)

        compiled = self.ensure_playbook(slice_)
        if not compiled.ok:
            self.log_event(
                "waiting_for_playbook_or_compile_inputs",
                {"exit_code": compiled.exit_code, "stderr": compiled.stderr[-2000:]},
                slice_id=slice_["id"],
            )
            if compiled.exit_code not in {20, 21, 22}:
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

        validation = self.validate_playbook(slice_)
        if not validation.ok:
            self.record_failure(
                slice_["id"],
                "unsafe_write_root",
                "high",
                "Autonomous playbook validation failed.",
                "Rejected playbook before PO execution.",
                self.root / slice_["playbook"],
            )
            self.mark_slice_status(slice_["id"], "replan_required")
            return 3

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

    def run_loop(self, max_loops: int | None = None, requested_slice: str | None = None) -> int:
        loops = 0
        while True:
            code = self.run_once(requested_slice=requested_slice)
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
                    return str(payload[key])
        except json.JSONDecodeError:
            pass
        match = re.search(r"\b(run[_-][A-Za-z0-9_.:-]+)\b", text)
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
    parser.add_argument("--status", action="store_true", help="Print current autonomy state and exit.")
    parser.add_argument("--failures", action="store_true", help="Include failure ledger rows with --status.")
    parser.add_argument("--doctor", action="store_true", help="Run AutoKeel preflight checks and exit.")
    parser.add_argument("--replay-events", action="store_true", help="Print event log rows and exit.")
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
            result = autokeel.runner.run(["python", "-m", "scripts.verify_autonomy_preflight", "--json"], cwd=root, execute_in_dry_run=True)
            print(result.stdout, end="")
            if result.stderr:
                print(result.stderr, file=sys.stderr, end="")
            return result.exit_code
        if args.replay_events:
            print(json.dumps(list(iter_jsonl(autokeel.events_path) or []), indent=2, sort_keys=True))
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
            return autokeel.run_once(requested_slice=args.slice_id)
        return autokeel.run_loop(max_loops=args.max_loops, requested_slice=args.slice_id)


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
"""Install, inspect, or remove the Health Data Hub launchd agents.

Three per-user agents keep the product running on an always-on Mac:

- ``com.healthhub.mood-form``: the LAN-only Streamlit mood form, kept alive.
- ``com.healthhub.explainer``: the loopback-only retrospective page, kept alive.
- ``com.healthhub.oura-sync``: the Oura sleep sync, morning and early evening.
- ``com.healthhub.retrain``: the nightly model retrain after the evening log.
- ``com.healthhub.backup``: the nightly encrypted snapshot to iCloud Drive.

Plists are generated from this file so paths always match the checkout. Logs
go to ``~/Library/Logs`` and never contain tokens or health values because the
underlying scripts never print them.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import plistlib
import subprocess
import sys
import time
from typing import Any


REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = REPO_ROOT / ".venv" / "bin" / "python"
LAUNCH_AGENTS_DIR = Path.home() / "Library" / "LaunchAgents"
LOG_DIR = Path.home() / "Library" / "Logs"
LABEL_PREFIX = "com.healthhub."
MOOD_FORM_LABEL = f"{LABEL_PREFIX}mood-form"
EXPLAINER_LABEL = f"{LABEL_PREFIX}explainer"
OURA_SYNC_LABEL = f"{LABEL_PREFIX}oura-sync"
RETRAIN_LABEL = f"{LABEL_PREFIX}retrain"
BACKUP_LABEL = f"{LABEL_PREFIX}backup"
SYNC_TIMES = ({"Hour": 8, "Minute": 0}, {"Hour": 19, "Minute": 30})
RETRAIN_TIME = {"Hour": 23, "Minute": 0}
BACKUP_TIME = {"Hour": 23, "Minute": 30}
MINIMAL_PATH = "/usr/bin:/bin:/usr/sbin:/sbin"


def _base_plist(label: str, program_arguments: list[str], *, repo_root: Path, log_dir: Path) -> dict[str, Any]:
    return {
        "Label": label,
        "ProgramArguments": program_arguments,
        "WorkingDirectory": str(repo_root),
        "EnvironmentVariables": {"PATH": MINIMAL_PATH, "PYTHONUNBUFFERED": "1"},
        "StandardOutPath": str(log_dir / f"healthhub-{label.removeprefix(LABEL_PREFIX)}.log"),
        "StandardErrorPath": str(log_dir / f"healthhub-{label.removeprefix(LABEL_PREFIX)}.log"),
        "ProcessType": "Background",
    }


def build_plists(
    *,
    repo_root: Path = REPO_ROOT,
    python: Path = PYTHON,
    log_dir: Path = LOG_DIR,
) -> dict[str, dict[str, Any]]:
    """Return the three agent definitions keyed by label."""

    mood_form = _base_plist(
        MOOD_FORM_LABEL,
        [str(python), str(repo_root / "scripts" / "run_mood_form.py")],
        repo_root=repo_root,
        log_dir=log_dir,
    )
    mood_form.update({"RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10})

    # The explainer binds to 127.0.0.1 only; it is the same-host read surface.
    explainer = _base_plist(
        EXPLAINER_LABEL,
        [str(python), str(repo_root / "scripts" / "run_explainer.py")],
        repo_root=repo_root,
        log_dir=log_dir,
    )
    explainer.update({"RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10})

    oura_sync = _base_plist(
        OURA_SYNC_LABEL,
        [str(python), str(repo_root / "scripts" / "sync_oura.py"), "--json"],
        repo_root=repo_root,
        log_dir=log_dir,
    )
    oura_sync.update({"RunAtLoad": False, "StartCalendarInterval": [dict(item) for item in SYNC_TIMES]})

    # nightly_retrain.py prints an allowlisted summary; retrain_model.py --json
    # would put feature values and the latest rating into the launchd log.
    retrain = _base_plist(
        RETRAIN_LABEL,
        [str(python), str(repo_root / "scripts" / "nightly_retrain.py")],
        repo_root=repo_root,
        log_dir=log_dir,
    )
    retrain.update({"RunAtLoad": False, "StartCalendarInterval": dict(RETRAIN_TIME)})

    # Encrypted snapshot to iCloud Drive after the retrain; --notify raises a
    # macOS notification on failure with no detail text.
    backup = _base_plist(
        BACKUP_LABEL,
        [str(python), str(repo_root / "scripts" / "backup_snapshot.py"), "--json", "--notify"],
        repo_root=repo_root,
        log_dir=log_dir,
    )
    backup.update({"RunAtLoad": False, "StartCalendarInterval": dict(BACKUP_TIME)})

    return {
        MOOD_FORM_LABEL: mood_form,
        EXPLAINER_LABEL: explainer,
        OURA_SYNC_LABEL: oura_sync,
        RETRAIN_LABEL: retrain,
        BACKUP_LABEL: backup,
    }


def plist_path(label: str, launch_agents_dir: Path = LAUNCH_AGENTS_DIR) -> Path:
    return launch_agents_dir / f"{label}.plist"


def write_plists(plists: dict[str, dict[str, Any]], launch_agents_dir: Path) -> list[Path]:
    launch_agents_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for label, payload in plists.items():
        path = plist_path(label, launch_agents_dir)
        if path.is_symlink():
            raise OSError(f"refusing symlink plist path: {path}")
        with path.open("wb") as handle:
            plistlib.dump(payload, handle, sort_keys=True)
        path.chmod(0o644)
        written.append(path)
    return written


def _launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["launchctl", *args],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def _domain() -> str:
    return f"gui/{os.getuid()}"


BOOTSTRAP_ATTEMPTS = 6
BOOTSTRAP_RETRY_SECONDS = 1.5


def bootstrap(label: str, path: Path, *, attempts: int = BOOTSTRAP_ATTEMPTS) -> dict[str, Any]:
    """(Re)load one agent; bootout first so edited plists take effect.

    launchd briefly reports "Input/output error" (code 5) while a booted-out
    KeepAlive job is still tearing down, so bootstrap is retried a few times.
    """

    _launchctl("bootout", f"{_domain()}/{label}")
    result = _launchctl("bootstrap", _domain(), str(path))
    attempt = 1
    while result.returncode != 0 and attempt < attempts and "Input/output error" in (result.stderr + result.stdout):
        time.sleep(BOOTSTRAP_RETRY_SECONDS)
        result = _launchctl("bootstrap", _domain(), str(path))
        attempt += 1
    return {
        "label": label,
        "ok": result.returncode == 0,
        "attempts": attempt,
        "detail": (result.stderr or result.stdout).strip()[:200],
    }


def bootout(label: str) -> dict[str, Any]:
    result = _launchctl("bootout", f"{_domain()}/{label}")
    return {
        "label": label,
        "ok": result.returncode == 0 or "No such process" in result.stderr,
        "detail": (result.stderr or result.stdout).strip()[:200],
    }


def agent_status(label: str) -> dict[str, Any]:
    result = _launchctl("print", f"{_domain()}/{label}")
    loaded = result.returncode == 0
    state = None
    pid = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("state = "):
            state = stripped.removeprefix("state = ")
        elif stripped.startswith("pid = "):
            pid = stripped.removeprefix("pid = ")
    return {"label": label, "loaded": loaded, "state": state, "pid": pid}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Manage the Health Data Hub launchd agents.")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--dry-run", action="store_true", help="Print the generated plists and exit.")
    action.add_argument("--install", action="store_true", help="Write the plists and (re)load the agents.")
    action.add_argument("--uninstall", action="store_true", help="Unload the agents and delete the plists.")
    action.add_argument("--status", action="store_true", help="Show whether each agent is loaded.")
    parser.add_argument("--launch-agents-dir", default=str(LAUNCH_AGENTS_DIR))
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    launch_agents_dir = Path(args.launch_agents_dir).expanduser()
    plists = build_plists()

    if args.dry_run:
        print(json.dumps(plists, indent=2, sort_keys=True))
        return 0

    if args.status:
        report = {"agents": [agent_status(label) for label in plists]}
    elif args.install:
        if not PYTHON.exists():
            print(json.dumps({"status": "error", "errors": [f"missing interpreter: {PYTHON}"]}, indent=2))
            return 1
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        written = write_plists(plists, launch_agents_dir)
        results = [bootstrap(label, plist_path(label, launch_agents_dir)) for label in plists]
        report = {
            "status": "ok" if all(item["ok"] for item in results) else "error",
            "written": [str(path) for path in written],
            "agents": results,
        }
    else:
        results = [bootout(label) for label in plists]
        removed: list[str] = []
        for label in plists:
            path = plist_path(label, launch_agents_dir)
            if path.exists() and not path.is_symlink():
                path.unlink()
                removed.append(str(path))
        report = {
            "status": "ok" if all(item["ok"] for item in results) else "error",
            "removed": removed,
            "agents": results,
        }

    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        for agent in report["agents"]:
            print(json.dumps(agent, sort_keys=True))
        print(report.get("status", "ok"))
    return 0 if report.get("status", "ok") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())

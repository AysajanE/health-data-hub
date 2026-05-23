from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


SECRET_KEYS = ("TOKEN", "SECRET", "PASSWORD", "AUTHORIZATION", "API_KEY")


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def redact_env(env: dict[str, str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in env.items():
        if any(marker in key.upper() for marker in SECRET_KEYS):
            redacted[key] = "[REDACTED]"
        else:
            redacted[key] = value
    return redacted


def evidence_dir(root: Path, collector: str) -> Path:
    path = root / "private" / "evidence" / "S03" / collector
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_report(root: Path, collector: str, payload: dict[str, Any]) -> Path:
    path = evidence_dir(root, collector) / f"{collector}-{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')}.json"
    safe_payload = {
        "collector": collector,
        "created_at": now_iso(),
        **payload,
    }
    path.write_text(json.dumps(safe_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path

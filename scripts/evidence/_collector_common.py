from __future__ import annotations

import json
import os
import re
from datetime import date
from datetime import datetime
from pathlib import Path
from typing import Any
from typing import Iterable


SECRET_KEYS = ("TOKEN", "SECRET", "PASSWORD", "AUTHORIZATION", "API_KEY")
FAILURE_LEDGER_SCHEMA = "autokeel.failure_ledger.v2"
REPORT_SCHEMA = "s03_evidence.v1"


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


def env_presence_markers(names: Iterable[str], env: dict[str, str] | None = None) -> dict[str, str]:
    source = os.environ if env is None else env
    return {name: "[REDACTED]" if source.get(name) else "[UNSET]" for name in names}


def sanitize_text(text: str, *, secret_values: Iterable[str] = ()) -> str:
    redacted = text
    for value in secret_values:
        if value:
            redacted = redacted.replace(value, "[REDACTED]")
    redacted = re.sub(r"https?://\S+", "[REDACTED_URL]", redacted)
    redacted = re.sub(r"\b\d{4}-\d{2}-\d{2}(?:[T ][0-9:.+\-Z]+)?\b", "[REDACTED_DATE]", redacted)
    redacted = re.sub(r"\b[A-Za-z0-9_-]{24,}\b", "[REDACTED]", redacted)
    return redacted[:240]


def parse_report_date(value: Any) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        pass
    normalized = value.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).date()
    except ValueError:
        return None


def coarse_freshness_bucket(records: Iterable[dict[str, Any]], *, reference_date: date, date_keys: Iterable[str]) -> str:
    newest: date | None = None
    for record in records:
        if not isinstance(record, dict):
            continue
        for key in date_keys:
            parsed = parse_report_date(record.get(key))
            if parsed and (newest is None or parsed > newest):
                newest = parsed
    if newest is None:
        return "unknown"
    days_old = max(0, (reference_date - newest).days)
    if days_old == 0:
        return "same_day"
    if days_old == 1:
        return "1_day_old"
    if days_old <= 3:
        return "2_to_3_days_old"
    if days_old <= 7:
        return "4_to_7_days_old"
    return "8_plus_days_old"


def evidence_dir(root: Path, collector: str) -> Path:
    path = root / "private" / "evidence" / "S03" / collector
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_report(root: Path, collector: str, payload: dict[str, Any]) -> Path:
    path = evidence_dir(root, collector) / f"{collector}-{datetime.now().astimezone().strftime('%Y%m%dT%H%M%S%z')}.json"
    safe_payload = {
        "collector": collector,
        "created_at": now_iso(),
        "report_schema": REPORT_SCHEMA,
        **payload,
    }
    path.write_text(json.dumps(safe_payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.strip():
            yield json.loads(line)


def append_jsonl(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def ensure_open_failure(
    root: Path,
    *,
    slice_id: str,
    failure_class: str,
    severity: str,
    description: str,
    action_taken: str,
    evidence_path: str,
    failure_origin: str = "external_provider",
) -> bool:
    ledger_path = root / "ops" / "autonomy" / "failure_ledger.jsonl"
    existing = list(iter_jsonl(ledger_path) or [])
    for row in reversed(existing):
        if row.get("slice") == slice_id and row.get("failure_class") == failure_class and row.get("open", True):
            return False
    payload = {
        "schema_version": FAILURE_LEDGER_SCHEMA,
        "ts": now_iso(),
        "slice": slice_id,
        "run_id": None,
        "failure_class": failure_class,
        "severity": severity,
        "description": description,
        "action_taken": action_taken,
        "evidence_path": evidence_path,
        "root_cause_id": f"{slice_id}-{failure_class}".upper().replace("_", "-"),
        "failure_origin": failure_origin,
        "supersedes": [],
        "superseded_by": None,
        "false_positive": False,
        "closure_validation_command": "",
        "open": True,
    }
    append_jsonl(ledger_path, payload)
    return True

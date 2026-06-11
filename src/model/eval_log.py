from __future__ import annotations

from dataclasses import asdict, is_dataclass
from datetime import date, datetime
import json
from pathlib import Path
from typing import Any, Mapping, Sequence


def confidence_label_for_interval(full_width: float) -> str:
    if full_width <= 2.0:
        return "high"
    if full_width <= 3.5:
        return "medium"
    return "low"


def append_eval_record(path: Path | str, record: Mapping[str, Any]) -> dict[str, Any]:
    log_path = Path(path)
    normalized = json_ready(dict(record))
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(normalized, sort_keys=True) + "\n")
    return normalized


def read_eval_records(path: Path | str) -> list[dict[str, Any]]:
    log_path = Path(path)
    if not log_path.exists():
        return []
    return [
        json.loads(line)
        for line in log_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


def json_ready(value: Any) -> Any:
    if is_dataclass(value):
        return json_ready(asdict(value))
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [json_ready(item) for item in value]
    return value


__all__ = [
    "append_eval_record",
    "confidence_label_for_interval",
    "json_ready",
    "read_eval_records",
]

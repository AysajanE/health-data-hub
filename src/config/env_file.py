"""Read explicitly allowed environment settings without dotenv dependencies."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterable, Mapping

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / ".env.local"


def read_env_file(path: Path, keys: Iterable[str]) -> dict[str, str]:
    """Read only the whitelisted ``KEY=VALUE`` lines from a dotenv-style file."""
    wanted = set(keys)
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        key, _, value = line.partition("=")
        key = key.strip()
        if key not in wanted:
            continue
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def resolve_env(
    keys: Iterable[str],
    *,
    env: Mapping[str, str] = os.environ,
    env_file: Path = DEFAULT_ENV_FILE,
) -> dict[str, str]:
    wanted = tuple(keys)
    file_values = read_env_file(Path(env_file), wanted)
    return {
        key: (env.get(key) or "").strip() or (file_values.get(key) or "").strip()
        for key in wanted
    }

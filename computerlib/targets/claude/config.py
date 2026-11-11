"""Claude Desktop's `claude_desktop_config.json`: read, validate, and rewrite it without losing anything else in it.

HOW IT IS REACHED: the `computer claude` commands.
INVARIANTS: invalid JSON or a wrong-shaped `mcpServers` is a usage error (exit 2) and the file is never overwritten; every other
key survives (JSON re-dump, indent 2); a write always follows a timestamped backup and is atomic.
"""

from __future__ import annotations

import json
from pathlib import Path

from agenttools_errors import PermissionDeniedError, UsageError

from computerlib.fileio import atomic_write, backup
from computerlib.jsonvalue import as_dict, is_object
from computerlib.paths import config_home

SERVER_NAME = "desktop-commander"


def config_path() -> Path:
    return config_home() / "Claude" / "claude_desktop_config.json"


def load() -> dict[str, object] | None:
    """The parsed config, or None when the file does not exist yet."""
    path = config_path()
    if not path.exists():
        return None
    try:
        data: object = json.loads(path.read_text(encoding="utf-8"))
    except PermissionError as exc:
        raise PermissionDeniedError(what=f"cannot read {path}", why=str(exc), fix="fix its permissions and re-run") from exc
    except OSError as exc:
        raise UsageError(what=f"cannot read {path}", why=str(exc), fix="fix the path and re-run") from exc
    except ValueError as exc:
        raise UsageError(
            what=f"{path} is not valid JSON",
            why=str(exc),
            fix="fix or move the file, then re-run; `computer` never overwrites a config it cannot parse",
        ) from exc
    if not is_object(data):
        raise UsageError(
            what=f"{path} is not a JSON object", why="the top level must be an object", fix="fix the file and re-run"
        )
    config = as_dict(data)
    if "mcpServers" in config and not isinstance(config["mcpServers"], dict):
        raise UsageError(
            what=f"{path}: mcpServers is not an object",
            why="Claude Desktop expects a map of servers",
            fix="fix the file and re-run",
        )
    return config


def servers(config: dict[str, object]) -> dict[str, object]:
    return as_dict(config.get("mcpServers"))


def entry(config: dict[str, object]) -> object:
    return servers(config).get(SERVER_NAME)


def save(config: dict[str, object]) -> Path | None:
    """Write the config atomically after backing up the existing file; returns the backup path (None for a first write)."""
    path = config_path()
    made = backup(path) if path.exists() else None
    atomic_write(path, json.dumps(config, indent=2, ensure_ascii=False) + "\n")
    return made

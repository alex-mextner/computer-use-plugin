"""Claude Desktop's XDG autostart entry (`~/.config/autostart/claude-desktop.desktop`): detect, create or re-enable.

INVARIANTS: an entry that is present and enabled is never rewritten (it may carry the user's own Exec line); only a missing or
disabled entry is touched.
"""

from __future__ import annotations

import re
import shutil
from collections.abc import Iterator
from enum import Enum
from pathlib import Path

from agenttools_errors import MissingTargetError, PermissionDeniedError

from computerlib.fileio import atomic_write, backup
from computerlib.paths import config_home

# Where the packaged Claude Desktop installs its launcher; `claude-desktop` on PATH is the fallback.
CLAUDE_DESKTOP_BINARIES = ("/usr/lib/claude-desktop/claude-desktop",)

_HIDDEN_TRUE = re.compile(r"^\s*Hidden\s*=\s*(true|1)\s*$", re.IGNORECASE)
_ENABLED_KEY = re.compile(r"^\s*X-GNOME-Autostart-enabled\s*=", re.IGNORECASE)
_ENABLED_FALSE = re.compile(r"^\s*X-GNOME-Autostart-enabled\s*=\s*(false|0)\s*$", re.IGNORECASE)


class AutostartState(Enum):
    MISSING = "missing"
    DISABLED = "disabled"
    ENABLED = "enabled"
    UNREADABLE = "unreadable"


def autostart_path() -> Path:
    return config_home() / "autostart" / "claude-desktop.desktop"


def _entry_lines(lines: list[str]) -> Iterator[tuple[int, str]]:
    """(index, line) for the lines inside `[Desktop Entry]`; other sections (actions) may legitimately repeat these keys."""
    inside = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            inside = stripped == "[Desktop Entry]"
        elif inside:
            yield index, line


def state() -> AutostartState:
    path = autostart_path()
    if not path.exists():
        return AutostartState.MISSING
    try:
        lines = path.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    except OSError:
        return AutostartState.UNREADABLE
    disabled = any(_HIDDEN_TRUE.match(line) or _ENABLED_FALSE.match(line) for _, line in _entry_lines(lines))
    return AutostartState.DISABLED if disabled else AutostartState.ENABLED


def find_binary() -> str | None:
    for candidate in CLAUDE_DESKTOP_BINARIES:
        if Path(candidate).is_file():
            return candidate
    return shutil.which("claude-desktop")


def _with_enabled_key(rewritten: list[str]) -> list[str]:
    """Insert the enabled key right under the `[Desktop Entry]` header (or at the end when the header is missing)."""
    header = next((i for i, line in enumerate(rewritten) if line.strip() == "[Desktop Entry]"), None)
    position = len(rewritten) if header is None else header + 1
    return [*rewritten[:position], "X-GNOME-Autostart-enabled=true", *rewritten[position:]]


def _reenable(path: Path) -> None:
    backup(path)
    lines = path.read_text(encoding="utf-8", errors="surrogateescape").splitlines()
    inside = list(_entry_lines(lines))
    hidden = {i for i, line in inside if _HIDDEN_TRUE.match(line)}
    enabled = {i for i, line in inside if _ENABLED_KEY.match(line)}
    rewritten = ["X-GNOME-Autostart-enabled=true" if i in enabled else line for i, line in enumerate(lines) if i not in hidden]
    atomic_write(path, "\n".join(rewritten if enabled else _with_enabled_key(rewritten)) + "\n")


def _create(path: Path) -> None:
    binary = find_binary()
    if binary is None:
        raise MissingTargetError(
            what="cannot create the Claude Desktop autostart entry",
            why="the Claude Desktop launcher was not found (not at /usr/lib/claude-desktop/claude-desktop, not on PATH)",
            fix="install Claude Desktop, then re-run `computer claude install` (the Claude config entry was already written)",
        )
    atomic_write(
        path, f'[Desktop Entry]\nType=Application\nName=Claude\nExec="{binary}" --startup\nX-GNOME-Autostart-enabled=true\n'
    )


def ensure() -> str:
    """Create or re-enable the autostart entry when needed; returns a one-line report."""
    path = autostart_path()
    current = state()
    if current is AutostartState.UNREADABLE:
        raise PermissionDeniedError(
            what=f"cannot read the autostart entry {path}",
            why="it is not a readable file (permissions, or it is a directory)",
            fix=f"fix or remove {path} and re-run `computer claude install`",
        )
    if current is AutostartState.ENABLED:
        return f"autostart already enabled ({path})"
    if current is AutostartState.DISABLED:
        _reenable(path)
        return f"autostart was disabled; re-enabled ({path})"
    _create(path)
    return f"autostart created ({path})"

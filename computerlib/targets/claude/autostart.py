"""Claude Desktop's XDG autostart entry (`~/.config/autostart/claude-desktop.desktop`): detect, create or repair.

INVARIANTS: an entry that is present, enabled and whose Exec program exists is never rewritten (it may carry the user's own
Exec line). A disabled entry only gets its enabled keys fixed; an entry whose Exec is missing or names a program that does not
exist only gets its Exec line replaced (the session would fail to start it at login anyway); an empty or `[Desktop Entry]`-less
file is replaced as a whole. Every change is backed up first. Exec is written and read per the Desktop Entry spec's escaping.
"""

from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterator
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from agenttools_errors import MissingTargetError, PermissionDeniedError

from computerlib.fileio import atomic_write, backup
from computerlib.paths import config_home

# Where the packaged Claude Desktop installs its launcher; `claude-desktop` on PATH is the fallback.
CLAUDE_DESKTOP_BINARIES = ("/usr/lib/claude-desktop/claude-desktop",)

_HEADER = "[Desktop Entry]"
_HIDDEN_TRUE = re.compile(r"^\s*Hidden\s*=\s*(true|1)\s*$", re.IGNORECASE)
_ENABLED_KEY = re.compile(r"^\s*X-GNOME-Autostart-enabled\s*=", re.IGNORECASE)
_ENABLED_FALSE = re.compile(r"^\s*X-GNOME-Autostart-enabled\s*=\s*(false|0)\s*$", re.IGNORECASE)
# Keys are case-sensitive in the spec; `Exec[de]` style localized variants do not exist for Exec.
_EXEC_KEY = re.compile(r"^\s*Exec\s*=(.*)$")
_STRING_ESCAPES = {"s": " ", "n": "\n", "t": "\t", "r": "\r", "\\": "\\"}
# Inside a quoted Exec argument these must be backslash-escaped (spec: "Exec key", reserved characters).
_QUOTED_ESCAPED = '"`$\\'


class AutostartState(Enum):
    MISSING = "missing"
    INVALID = "invalid"
    DISABLED = "disabled"
    BROKEN_EXEC = "broken-exec"
    ENABLED = "enabled"
    UNREADABLE = "unreadable"


@dataclass(frozen=True)
class Inspection:
    state: AutostartState
    detail: str = ""


def autostart_path() -> Path:
    return config_home() / "autostart" / "claude-desktop.desktop"


def _entry_lines(lines: list[str]) -> Iterator[tuple[int, str]]:
    """(index, line) for the lines inside `[Desktop Entry]`; other sections (actions) may legitimately repeat these keys."""
    inside = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("["):
            inside = stripped == _HEADER
        elif inside:
            yield index, line


def quote_exec_argument(argument: str) -> str:
    """One Exec argument, quoted and escaped so a desktop-entry reader gets `argument` back verbatim.

    The spec applies the string-value escapes (`\\\\`, `\\n`, ...) before the Exec quoting rule, so a literal backslash takes
    four backslashes in the file; `%` is the field-code prefix and is written `%%`.
    """
    quoted = "".join(f"\\{char}" if char in _QUOTED_ESCAPED else char for char in argument).replace("%", "%%")
    as_string = quoted.replace("\\", "\\\\").replace("\n", "\\n").replace("\t", "\\t").replace("\r", "\\r")
    return f'"{as_string}"'


def _unescape_string(value: str) -> str:
    out: list[str] = []
    chars = iter(value)
    for char in chars:
        if char == "\\":
            following = next(chars, "")
            out.append(_STRING_ESCAPES.get(following, "\\" + following))
        else:
            out.append(char)
    return "".join(out)


def exec_program(value: str) -> str:
    """The program (first argument) of an Exec value as written in the file; "" when there is none."""
    command = _unescape_string(value).strip()
    if not command.startswith('"'):
        return command.split(maxsplit=1)[0].replace("%%", "%") if command else ""
    out: list[str] = []
    chars = iter(command[1:])
    for char in chars:
        if char == '"':
            break
        if char == "\\":
            following = next(chars, "")
            out.append(following if following in _QUOTED_ESCAPED else "\\" + following)
        else:
            out.append(char)
    return "".join(out).replace("%%", "%")


def _program_exists(program: str) -> bool:
    # A relative program is looked up in PATH by the session (spec), the same lookup shutil.which does here.
    if os.path.isabs(program):
        return os.path.isfile(program) and os.access(program, os.X_OK)
    return bool(program) and "/" not in program and shutil.which(program) is not None


def _inspect_lines(lines: list[str]) -> Inspection:
    if not any(line.strip() == _HEADER for line in lines):
        return Inspection(AutostartState.INVALID, f"no {_HEADER} section")
    inside = list(_entry_lines(lines))
    if any(_HIDDEN_TRUE.match(line) or _ENABLED_FALSE.match(line) for _, line in inside):
        return Inspection(AutostartState.DISABLED)
    execs = [match.group(1) for _, line in inside if (match := _EXEC_KEY.match(line))]
    if not execs:
        return Inspection(AutostartState.BROKEN_EXEC, "no Exec line")
    program = exec_program(execs[-1])
    if not _program_exists(program):
        return Inspection(AutostartState.BROKEN_EXEC, f"Exec program {program or '(empty)'!r} does not exist")
    return Inspection(AutostartState.ENABLED)


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="surrogateescape").splitlines()


def inspect() -> Inspection:
    path = autostart_path()
    if not path.exists():
        return Inspection(AutostartState.MISSING)
    try:
        lines = _read_lines(path)
    except OSError:
        return Inspection(AutostartState.UNREADABLE)
    return _inspect_lines(lines)


def state() -> AutostartState:
    return inspect().state


def find_binary() -> str | None:
    for candidate in CLAUDE_DESKTOP_BINARIES:
        if Path(candidate).is_file():
            return candidate
    return shutil.which("claude-desktop")


def _our_exec_line() -> str:
    binary = find_binary()
    if binary is None:
        raise MissingTargetError(
            what="cannot write the Claude Desktop autostart entry",
            why="the Claude Desktop launcher was not found (not at /usr/lib/claude-desktop/claude-desktop, not on PATH)",
            fix="install Claude Desktop, then re-run `computer claude install` (the Claude config entry was already written)",
        )
    return f"Exec={quote_exec_argument(binary)} --startup"


def _insert_under_header(lines: list[str], line: str) -> list[str]:
    header = next(i for i, current in enumerate(lines) if current.strip() == _HEADER)
    return [*lines[: header + 1], line, *lines[header + 1 :]]


def _reenabled(lines: list[str]) -> list[str]:
    inside = list(_entry_lines(lines))
    hidden = {i for i, line in inside if _HIDDEN_TRUE.match(line)}
    enabled = {i for i, line in inside if _ENABLED_KEY.match(line)}
    rewritten = ["X-GNOME-Autostart-enabled=true" if i in enabled else line for i, line in enumerate(lines) if i not in hidden]
    return rewritten if enabled else _insert_under_header(rewritten, "X-GNOME-Autostart-enabled=true")


def _with_our_exec(lines: list[str], exec_line: str) -> list[str]:
    execs = {i for i, line in _entry_lines(lines) if _EXEC_KEY.match(line)}
    if not execs:
        return _insert_under_header(lines, exec_line)
    return [exec_line if i in execs else line for i, line in enumerate(lines)]


def _fresh_entry(exec_line: str) -> str:
    return f"{_HEADER}\nType=Application\nName=Claude\n{exec_line}\nX-GNOME-Autostart-enabled=true\n"


def ensure() -> str:
    """Create or repair the autostart entry when needed; returns a one-line report."""
    path = autostart_path()
    current = inspect()
    if current.state is AutostartState.UNREADABLE:
        raise PermissionDeniedError(
            what=f"cannot read the autostart entry {path}",
            why="it is not a readable file (permissions, or it is a directory)",
            fix=f"fix or remove {path} and re-run `computer claude install`",
        )
    if current.state is AutostartState.ENABLED:
        return f"autostart already enabled ({path})"
    if current.state is AutostartState.MISSING:
        atomic_write(path, _fresh_entry(_our_exec_line()))
        return f"autostart created ({path})"
    if current.state is AutostartState.INVALID:
        exec_line = _our_exec_line()
        backup(path)
        atomic_write(path, _fresh_entry(exec_line))
        return f"autostart had {current.detail}; replaced ({path})"
    lines = _read_lines(path)
    repairs: list[str] = []
    if current.state is AutostartState.DISABLED:
        lines = _reenabled(lines)
        repairs.append("was disabled; re-enabled")
        current = _inspect_lines(lines)
    if current.state is AutostartState.BROKEN_EXEC:
        lines = _with_our_exec(lines, _our_exec_line())
        repairs.append(f"had {current.detail}; Exec replaced")
    backup(path)
    atomic_write(path, "\n".join(lines) + "\n")
    return f"autostart {', '.join(repairs)} ({path})"

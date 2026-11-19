"""Claude Desktop's XDG autostart entry (`~/.config/autostart/claude-desktop.desktop`): detect, create or repair.

INVARIANTS: an entry that is present, enabled and whose Exec parses and names an executable program is never rewritten (it may
carry the user's own Exec line). A disabled entry only gets its enabled keys fixed; an entry whose Exec is missing, cannot be
parsed (an unterminated quote) or names a program that does not exist or cannot be executed only gets its Exec line replaced (the
session would fail to start it at login anyway); an empty or `[Desktop Entry]`-less file is replaced as a whole. A repair is
decided by the content it rewrites, read just before the write. Every change is backed up first. Exec is written per the
Desktop Entry spec's escaping and read the way GIO reads it (GLib's shell quoting), since GIO starts it when GNOME logs in.
"""

from __future__ import annotations

import os
import re
import shlex
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


class _ShellWords:
    """`command` cut into still-quoted words as GLib's g_shell_parse_argv cuts it: blanks outside quotes separate words, a `#`
    after a blank starts a comment, a backslash outside quotes escapes the next character. `split` raises ValueError for the text
    GLib refuses (an unterminated quote, a trailing backslash, a `#` that ends the text)."""

    def __init__(self, command: str) -> None:
        self.command = command
        self.tokens: list[str] = []
        self.token: list[str] = []
        self.quote = ""  # the open quote, "\\" right after an unquoted backslash, or "#" inside a comment
        self.odd_backslashes = (
            False  # inside double quotes, a `"` after an odd run of backslashes is escaped, not a closing quote
        )
        self.comment_at = -1

    def split(self) -> list[str]:
        for index, char in enumerate(self.command):
            if self.quote == "\\":
                self._escaped(char)
            elif self.quote == "#":
                self.quote = "" if char == "\n" else self.quote
            elif self.quote:
                self._quoted(char)
            else:
                self._unquoted(index, char)
            self.odd_backslashes = not self.odd_backslashes if char == "\\" else False
        self._check_end()
        return [*self.tokens, "".join(self.token)] if self.token else self.tokens

    def _escaped(self, char: str) -> None:
        if char != "\n":
            self.token += ["\\", char]
        self.quote = ""

    def _quoted(self, char: str) -> None:
        if char == self.quote and not (self.quote == '"' and self.odd_backslashes):
            self.quote = ""
        self.token.append(char)

    def _unquoted(self, index: int, char: str) -> None:
        if char in " \t\n":
            if self.token:
                self.tokens.append("".join(self.token))
                self.token = []
        elif char in "'\"\\":
            self.token += [] if char == "\\" else [char]
            self.quote = char
        elif char == "#" and (index == 0 or self.command[index - 1] in " \n"):
            self.quote = "#"
            self.comment_at = index
        else:
            self.token.append(char)

    def _check_end(self) -> None:
        if self.quote == "\\":
            raise ValueError("it ends in a backslash")
        if self.quote in ("'", '"'):
            raise ValueError(f"its {self.quote} quote is never closed")
        # GLib only enters a comment on the character after its `#`, so a `#` that ends the text is an unclosed quote to it.
        if self.quote == "#" and self.comment_at == len(self.command) - 1:
            raise ValueError("it ends in a comment sign")


def _single_quoted(token: str, index: int) -> tuple[str, int]:
    """The text of the single-quoted part opened just before `index` (nothing is escaped in it), and the index after it."""
    end = token.find("'", index)
    if end == -1:
        raise ValueError("its ' quote is never closed")
    return token[index:end], end + 1


def _double_quoted(token: str, index: int) -> tuple[str, int]:
    """The text of the double-quoted part opened just before `index`, where a backslash only escapes `"`, `\\`, `` ` ``, `$` and
    a newline, and the index after it."""
    out: list[str] = []
    while index < len(token) and token[index] != '"':
        if token[index] == "\\" and token[index + 1 : index + 2] in ('"', "\\", "`", "$", "\n"):
            index += 1
        out.append(token[index])
        index += 1
    if index == len(token):
        raise ValueError('its " quote is never closed')
    return "".join(out), index + 1


_QUOTED_PARTS = {"'": _single_quoted, '"': _double_quoted}


def _shell_unquote(token: str) -> str:
    """One word of `_ShellWords` with its quoting removed, as g_shell_unquote does."""
    out: list[str] = []
    index = 0
    while index < len(token):
        char = token[index]
        index += 1
        if char == "\\":
            # Outside quotes a backslash escapes any character; before a newline both vanish.
            out.append(token[index : index + 1].replace("\n", ""))
            index += 1
        elif char in _QUOTED_PARTS:
            part, index = _QUOTED_PARTS[char](token, index)
            out.append(part)
        else:
            out.append(char)
    return "".join(out)


def exec_program(value: str) -> str:
    """The program (first argument) of an Exec value as written in the file; "" when there is none. Raises ValueError (the
    reason) when GIO could not parse the value, and so would not start the entry at all."""
    tokens = _ShellWords(_unescape_string(value)).split()
    return _shell_unquote(tokens[0]).replace("%%", "%") if tokens else ""


def _login_path() -> str:
    """PATH as the login session can use it: only absolute entries, since a relative or empty one names whatever directory this
    command runs in."""
    return os.pathsep.join(entry for entry in os.environ.get("PATH", os.defpath).split(os.pathsep) if os.path.isabs(entry))


def _program_exists(program: str) -> bool:
    # A relative program is looked up in PATH by the session (spec).
    if os.path.isabs(program):
        return os.path.isfile(program) and os.access(program, os.X_OK)
    return bool(program) and "/" not in program and shutil.which(program, path=_login_path()) is not None


def _inspect_lines(lines: list[str]) -> Inspection:
    if not any(line.strip() == _HEADER for line in lines):
        return Inspection(AutostartState.INVALID, f"no {_HEADER} section")
    inside = list(_entry_lines(lines))
    if any(_HIDDEN_TRUE.match(line) or _ENABLED_FALSE.match(line) for _, line in inside):
        return Inspection(AutostartState.DISABLED)
    execs = [match.group(1) for _, line in inside if (match := _EXEC_KEY.match(line))]
    if not execs:
        return Inspection(AutostartState.BROKEN_EXEC, "no Exec line")
    try:
        program = exec_program(execs[-1])
    except ValueError as error:
        return Inspection(AutostartState.BROKEN_EXEC, f"Exec cannot be parsed: {error}")
    if not _program_exists(program):
        return Inspection(
            AutostartState.BROKEN_EXEC, f"Exec program {program or '(empty)'!r} does not exist or is not executable"
        )
    return Inspection(AutostartState.ENABLED)


def _read_lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8", errors="surrogateescape").splitlines()


def _read_entry(path: Path) -> tuple[Inspection, list[str]]:
    """The entry's classification together with the lines it was made from."""
    if not path.exists():
        return Inspection(AutostartState.MISSING), []
    try:
        lines = _read_lines(path)
    except OSError:
        return Inspection(AutostartState.UNREADABLE), []
    return _inspect_lines(lines), lines


def inspect() -> Inspection:
    return _read_entry(autostart_path())[0]


def state() -> AutostartState:
    return inspect().state


def find_binary() -> str | None:
    # The check inspection makes: a launcher it would call broken (no execute bit) must not be written into Exec, or every
    # install would find the entry broken again and repeat the repair.
    packaged = next((candidate for candidate in CLAUDE_DESKTOP_BINARIES if _program_exists(candidate)), None)
    return packaged or shutil.which("claude-desktop", path=_login_path())


def _our_exec_line(binary: str | None) -> str:
    if binary is None:
        unusable = next((candidate for candidate in CLAUDE_DESKTOP_BINARIES if os.path.isfile(candidate)), None)
        if unusable is not None:
            why = f"{unusable} is not executable and no other claude-desktop launcher was found on PATH"
            fix = f"make it executable (`chmod +x {shlex.quote(unusable)}`, as root) or reinstall Claude Desktop"
        else:
            why = "the Claude Desktop launcher was not found (not at /usr/lib/claude-desktop/claude-desktop, not on PATH)"
            fix = "install Claude Desktop"
        raise MissingTargetError(
            what="cannot write the Claude Desktop autostart entry",
            why=why,
            fix=f"{fix}, then re-run `computer claude install` (the Claude config entry was already written)",
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


def _unreadable(path: Path) -> PermissionDeniedError:
    return PermissionDeniedError(
        what=f"cannot read the autostart entry {path}",
        why="it is not a readable file (permissions, or it is a directory)",
        fix=f"fix or remove {path} and re-run `computer claude install`",
    )


def ensure() -> str:
    """Create or repair the autostart entry when needed; returns a one-line report."""
    path = autostart_path()
    # Looked up first, so the read below is the last look before any write and the repair is decided by the very content it
    # rewrites: an Exec the user fixed meanwhile is kept. A missing launcher only fails a repair that needs it.
    binary = find_binary()
    current, lines = _read_entry(path)
    if current.state is AutostartState.UNREADABLE:
        raise _unreadable(path)
    if current.state is AutostartState.ENABLED:
        return f"autostart already enabled ({path})"
    if current.state is AutostartState.MISSING:
        atomic_write(path, _fresh_entry(_our_exec_line(binary)))
        return f"autostart created ({path})"
    if current.state is AutostartState.INVALID:
        exec_line = _our_exec_line(binary)
        backup(path)
        atomic_write(path, _fresh_entry(exec_line))
        return f"autostart had {current.detail}; replaced ({path})"
    repairs: list[str] = []
    if current.state is AutostartState.DISABLED:
        lines = _reenabled(lines)
        repairs.append("was disabled; re-enabled")
        current = _inspect_lines(lines)
    if current.state is AutostartState.BROKEN_EXEC:
        lines = _with_our_exec(lines, _our_exec_line(binary))
        repairs.append(f"had {current.detail}; Exec replaced")
    backup(path)
    atomic_write(path, "\n".join(lines) + "\n")
    return f"autostart {', '.join(repairs)} ({path})"

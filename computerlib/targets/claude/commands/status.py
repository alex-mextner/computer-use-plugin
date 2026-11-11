"""`computer claude status`: is Claude Desktop really able to start Desktop Commander?

INVARIANTS: never changes the user's config or autostart files (the handshake does start the real server, which keeps its own
state under its home directory); the handshake runs the configured command as a bare-PATH autostart would (HOME, a minimal PATH
and the entry's own env), so a pass here means a pass at login; exit 0 only when every check passes, 3 otherwise; whether Claude
is running is informational.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
from typing import cast

from agenttools_errors import AgentToolError

from computerlib.commandtree import Command
from computerlib.desktop_commander import EXPECTED_TOOLS
from computerlib.jsonvalue import as_dict
from computerlib.mcp_probe import HandshakeError, handshake
from computerlib.targets.claude import autostart, config

HANDSHAKE_TIMEOUT_SECONDS = 30.0
_SHOWN_TOOLS = 8


def _autostart_environment(entry_env: object) -> dict[str, str]:
    """What Claude Desktop gives the server at login: HOME, a bare PATH and the entry's own `env`, never the caller's shell."""
    env = {"HOME": str(Path.home()), "PATH": "/usr/bin:/bin"}
    env.update({k: v for k, v in as_dict(entry_env).items() if isinstance(v, str)})
    return env


def claude_running() -> bool:
    """Whether a Claude Desktop process exists (matched on argv[0] under /proc; informational only)."""
    for cmdline in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            argv0 = cmdline.read_bytes().split(b"\0", 1)[0].decode(errors="replace")
        except OSError:
            continue
        if os.path.basename(argv0) == "claude-desktop" or "/claude-desktop/" in argv0:
            return True
    return False


def _check_server(report: list[tuple[bool, str]]) -> None:
    try:
        current = config.load()
    except AgentToolError as error:
        report.append((False, f"config: {error.what}"))
        return
    path = config.config_path()
    if current is None:
        report.append((False, f"config: {path} does not exist (run `computer claude install`)"))
        return
    spec = as_dict(config.entry(current))
    command = spec.get("command")
    if not isinstance(command, str):
        report.append((False, f"config: no usable {config.SERVER_NAME} entry in {path} (run `computer claude install`)"))
        return
    raw_args = spec.get("args")
    args = [str(a) for a in cast("list[object]", raw_args)] if isinstance(raw_args, list) else []
    report.append((True, f"config: {config.SERVER_NAME} -> {command} {' '.join(args)}"))
    if not (os.path.isabs(command) and os.path.isfile(command) and os.access(command, os.X_OK)):
        report.append((False, f"executable: {command} is not an absolute path to an executable file"))
        return
    report.append((True, f"executable: {command}"))
    try:
        result = handshake(command, args, _autostart_environment(spec.get("env")), HANDSHAKE_TIMEOUT_SECONDS)
    except HandshakeError as error:
        report.append((False, f"handshake: {error}"))
        return
    missing = [tool for tool in EXPECTED_TOOLS if tool not in result.tools]
    shown = ", ".join(result.tools[:_SHOWN_TOOLS]) + (", ..." if len(result.tools) > _SHOWN_TOOLS else "")
    summary = f"{result.server_name} {result.server_version}: {len(result.tools)} tools: {shown}"
    if missing:
        report.append((False, f"handshake: ok but missing expected tools {missing}; {summary}"))
    else:
        report.append((True, f"handshake: {summary}"))


def run(_args: argparse.Namespace) -> int:
    report: list[tuple[bool, str]] = []
    _check_server(report)
    current = autostart.state()
    report.append((current is autostart.AutostartState.ENABLED, f"autostart: {current.value} ({autostart.autostart_path()})"))
    for ok, line in report:
        print(f"[{'ok' if ok else 'FAIL'}] {line}")
    print(f"[info] Claude Desktop running: {'yes' if claude_running() else 'no'}")
    return 0 if all(ok for ok, _ in report) else 3


COMMAND = Command(
    name="status",
    help="check config, executable, autostart and a real MCP handshake (exit 0 healthy, 3 unhealthy)",
    run=run,
)

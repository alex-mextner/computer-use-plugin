"""`computer mcp`: the client-neutral core, a pinned Desktop Commander and the stdio launcher every client adapter points at."""

from __future__ import annotations

from computerlib.commandtree import command_target

TARGET = command_target("mcp", "Install and launch the pinned Desktop Commander MCP server", __name__)

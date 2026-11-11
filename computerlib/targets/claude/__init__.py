"""`computer claude`: the Claude Desktop adapter; it knows Claude's config and autostart entry, never how DC is installed."""

from __future__ import annotations

from computerlib.commandtree import command_target

TARGET = command_target("claude", "Wire Claude Desktop to the pinned Desktop Commander (install, status, uninstall)", __name__)

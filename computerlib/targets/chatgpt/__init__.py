"""`computer chatgpt`: the ChatGPT adapter. ChatGPT reaches this PC through a public HTTPS URL (Cloudflare named tunnel -> shim ->
mcp-auth-proxy -> `computer mcp serve`); `computer chatgpt install --hostname <name>` sets all of it up in one command."""

from __future__ import annotations

from computerlib.commandtree import command_target

TARGET = command_target(
    "chatgpt",
    "Connect ChatGPT to this PC through a Cloudflare tunnel (install, status, connect, logs, doctor, uninstall, ...)",
    __name__,
)

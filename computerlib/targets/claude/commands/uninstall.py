"""`computer claude uninstall`: remove only the `desktop-commander` entry from Claude Desktop's config."""

from __future__ import annotations

import argparse

from computerlib.commandtree import Command
from computerlib.targets.claude import config


def run(_args: argparse.Namespace) -> int:
    current = config.load()
    if current is None or config.SERVER_NAME not in config.servers(current):
        print(f"{config.config_path()}: no {config.SERVER_NAME} entry, nothing to do")
        return 0
    updated = dict(current)
    updated["mcpServers"] = {k: v for k, v in config.servers(current).items() if k != config.SERVER_NAME}
    made = config.save(updated)
    print(f"{config.config_path()}: removed {config.SERVER_NAME}")
    if made is not None:
        print(f"backup: {made}")
    print("The managed Desktop Commander install and the autostart entry were left alone. Restart Claude Desktop to apply.")
    return 0


COMMAND = Command(
    name="uninstall",
    help="remove only the desktop-commander entry from Claude Desktop's config (the managed install and autostart stay)",
    run=run,
)

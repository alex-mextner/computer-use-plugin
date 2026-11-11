"""`computer claude install`: point Claude Desktop at `computer mcp serve` and make sure it autostarts.

INVARIANTS: nothing is written until the executable is found, the existing config parses and the core install succeeded; an
unchanged config is not rewritten and gets no backup; Claude Desktop is never restarted or signalled.
"""

from __future__ import annotations

import argparse
from pathlib import Path

from computerlib.commandtree import Command
from computerlib.desktop_commander import install as install_desktop_commander
from computerlib.executable import computer_executable
from computerlib.jsonvalue import as_dict
from computerlib.paths import explicit_config_home, explicit_data_home
from computerlib.targets.claude import autostart, config


def _merged_entry(existing: object, executable: Path) -> dict[str, object]:
    """Our command/args and (when needed) XDG_DATA_HOME env on top of whatever the user already keeps in the entry."""
    entry = as_dict(existing)
    entry["command"] = str(executable)
    entry["args"] = ["mcp", "serve"]
    env = as_dict(entry.get("env"))
    data_home = explicit_data_home()
    if data_home is not None:
        env["XDG_DATA_HOME"] = str(data_home)
    else:
        env.pop("XDG_DATA_HOME", None)
    if env:
        entry["env"] = env
    else:
        entry.pop("env", None)
    return entry


def run(_args: argparse.Namespace) -> int:
    executable = computer_executable()
    config.load()  # fail fast on a config we must not overwrite, before the slow npm install
    print(install_desktop_commander())
    # Re-read: npm took a while, and Claude Desktop may have written the file meanwhile.
    updated = config.load() or {}
    # Claude autostarts without the user's shell variables, so a custom data dir travels in the entry's env.
    wanted = _merged_entry(config.entry(updated), executable)
    if config.entry(updated) == wanted:
        print(f"{config.config_path()}: {config.SERVER_NAME} already set")
    else:
        updated["mcpServers"] = {**config.servers(updated), config.SERVER_NAME: wanted}
        made = config.save(updated)
        print(f"{config.config_path()}: set {config.SERVER_NAME} -> {executable} mcp serve")
        if made is not None:
            print(f"backup: {made}")
    print(autostart.ensure())
    if explicit_config_home() is not None:
        print(f"note: XDG_CONFIG_HOME={explicit_config_home()}; Claude Desktop only reads it if the whole session sets it")
    print("Restart Claude Desktop to pick the change up (this command never restarts it).")
    return 0


COMMAND = Command(
    name="install",
    help="register the pinned Desktop Commander in Claude Desktop's config and ensure autostart (idempotent)",
    run=run,
)

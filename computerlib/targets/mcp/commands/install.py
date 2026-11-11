"""`computer mcp install`: put the pinned Desktop Commander in the managed directory (idempotent)."""

from __future__ import annotations

import argparse

from computerlib.commandtree import Command
from computerlib.desktop_commander import install


def run(_args: argparse.Namespace) -> int:
    print(install())
    return 0


COMMAND = Command(
    name="install",
    help="install the pinned Desktop Commander into the managed directory (idempotent; needs node and npm)",
    run=run,
)

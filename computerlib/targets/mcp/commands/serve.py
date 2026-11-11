"""`computer mcp serve`: replace this process with the pinned Desktop Commander over stdio.

INVARIANTS: nothing is ever printed to stdout (it is the MCP protocol stream; errors go to stderr via `guard`); needs no PATH, the
node binary recorded by `computer mcp install` is exec'd by absolute path.
"""

from __future__ import annotations

import argparse
import os
import sys

from computerlib.commandtree import Command
from computerlib.desktop_commander import launch_spec


def run(_args: argparse.Namespace) -> int:
    spec = launch_spec()
    sys.stdout.flush()
    os.execv(spec.node, [str(spec.node), str(spec.entry)])


COMMAND = Command(
    name="serve",
    help="run the pinned Desktop Commander over stdio (what a client's config launches; stdout is MCP protocol only)",
    run=run,
)

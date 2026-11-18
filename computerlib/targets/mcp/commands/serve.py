"""`computer mcp serve`: replace this process with the pinned Desktop Commander over stdio.

INVARIANTS: nothing is ever printed to stdout (it is the MCP protocol stream; errors go to stderr via `guard`); needs no PATH, the
node binary recorded by `computer mcp install` is exec'd by absolute path; Desktop Commander always runs with `--no-onboarding`.
"""

from __future__ import annotations

import argparse
import os
import sys

from computerlib.commandtree import Command
from computerlib.desktop_commander import launch_spec

# On Desktop Commander's first run in a HOME (no ~/.claude-server-commander/config.json), its `initialize` handler runs the
# welcome-page onboarding first: it waits for the feature flags (up to 5 s) and, for users in the A/B arm that gets the page (100%
# of them in the flags served on 2026-11-18), awaits `xdg-open https://desktopcommander.app/welcome/`. Where that starts a browser
# that stays in the foreground (a desktop session whose browser is not already running for this HOME), xdg-open returns only when
# the browser closes, so `initialize` is never answered and mcp-auth-proxy exits (#21). The page is no use to the clients this
# command serves, so the welcome page is always off.
NO_ONBOARDING = "--no-onboarding"


def run(_args: argparse.Namespace) -> int:
    spec = launch_spec()
    sys.stdout.flush()
    os.execv(spec.node, [str(spec.node), str(spec.entry), NO_ONBOARDING])


COMMAND = Command(
    name="serve",
    help="run the pinned Desktop Commander over stdio (what a client's config launches; stdout is MCP protocol only)",
    run=run,
)

"""`computer mcp serve`: run the pinned Desktop Commander over stdio behind the relay that withholds its feedback channel.

INVARIANTS: nothing but MCP protocol is ever written to stdout (errors go to stderr via `guard`); needs no PATH, the node binary
recorded by `computer mcp install` is started by absolute path; Desktop Commander always runs with `--no-onboarding`; every
client reaches it through `dc_relay.relay`, which hides `give_feedback_to_desktop_commander` and DC's survey requests (#24).
ChatGPT selects `--terminal-only` to withhold file tools and interactive UI resources, including stale widget calls (#79).
"""

from __future__ import annotations

import argparse
import sys

from computerlib.commandtree import Command
from computerlib.dc_relay import relay
from computerlib.desktop_commander import launch_spec

# Until Desktop Commander has opened its welcome page once in a HOME (a fresh HOME has no ~/.claude-server-commander/config.json
# yet), its `initialize` handler runs the welcome-page onboarding first: it waits for the feature flags (up to 5 s) and, for users
# in the A/B arm that gets the page (100% of them in the flags served on 2026-11-18), awaits
# `xdg-open https://desktopcommander.app/welcome/`. Where that starts a browser
# that stays in the foreground (a desktop session whose browser is not already running for this HOME), xdg-open returns only when
# the browser closes, so `initialize` is never answered and mcp-auth-proxy exits (#21). The page is no use to the clients this
# command serves, so the welcome page is always off.
NO_ONBOARDING = "--no-onboarding"


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--terminal-only",
        action="store_true",
        help="expose terminal/process tools only, without file tools or interactive UI resources (used by ChatGPT)",
    )


def run(args: argparse.Namespace) -> int:
    spec = launch_spec()
    sys.stdout.flush()
    return relay(spec.node, [str(spec.entry), NO_ONBOARDING], terminal_only=args.terminal_only)


COMMAND = Command(
    name="serve",
    help="run the pinned Desktop Commander over stdio (what a client's config launches; stdout is MCP protocol only)",
    run=run,
    configure=configure,
)

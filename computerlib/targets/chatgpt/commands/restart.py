"""`computer chatgpt restart`: restart the stack in start order and wait until it serves."""

from __future__ import annotations

import argparse

from agenttools_errors import DriftError

from computerlib.commandtree import Command
from computerlib.targets.chatgpt.context import load_context
from computerlib.targets.chatgpt.lifecycle import not_serving_reason, unit_names, wait_until_serving


def configure(parser: argparse.ArgumentParser) -> None:
    del parser


def run(args: argparse.Namespace) -> int:
    del args
    ctx = load_context()
    names = unit_names(ctx)
    ctx.systemd.restart(names)
    print(f"restarted: {', '.join(names)}")
    if not wait_until_serving(ctx):
        raise DriftError(
            what="the stack did not start serving in time",
            why=not_serving_reason(ctx)
            if ctx.settings.mode == "tunnel"
            else "the shim or the proxy does not answer after the restart",
            fix="run `computer chatgpt doctor`",
        )
    print("serving")
    return 0


COMMAND = Command(name="restart", help="restart the stack and wait until it serves", run=run, configure=configure)

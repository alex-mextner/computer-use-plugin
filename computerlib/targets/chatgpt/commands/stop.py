"""`computer chatgpt stop`: stop the stack now. The units stay enabled, so it starts again at the next boot (use `end` to
disable)."""

from __future__ import annotations

import argparse

from computerlib.commandtree import Command
from computerlib.targets.chatgpt.context import load_context
from computerlib.targets.chatgpt.lifecycle import unit_names


def configure(parser: argparse.ArgumentParser) -> None:
    del parser


def run(args: argparse.Namespace) -> int:
    del args
    ctx = load_context()
    names = list(reversed(unit_names(ctx)))
    ctx.systemd.stop(names)
    print(f"stopped: {', '.join(names)}")
    return 0


COMMAND = Command(name="stop", help="stop the stack (stays enabled for the next boot)", run=run, configure=configure)

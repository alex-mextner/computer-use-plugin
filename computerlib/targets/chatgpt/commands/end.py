"""`computer chatgpt end`: stop the stack and disable the units, so nothing starts at boot.

`--revoke` also deletes the OAuth state of the proxy (clients, tokens, signing keys) and the client table of the shim: every
issued token stops working and ChatGPT must be connected again. Without it a later `start` resumes with the same sessions.
In tunnel mode there is no OAuth state; `--revoke` deletes the stored runtime key (revoke the key in the OpenAI Platform as well).
"""

from __future__ import annotations

import argparse

from computerlib.commandtree import Command
from computerlib.targets.chatgpt.context import load_context
from computerlib.targets.chatgpt.lifecycle import unit_names
from computerlib.targets.chatgpt.removal import remove_tree


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--revoke",
        action="store_true",
        help="also delete all OAuth sessions and registered clients (tunnel mode: the stored runtime key)",
    )


def run(args: argparse.Namespace) -> int:
    ctx = load_context()
    names = list(reversed(unit_names(ctx)))
    ctx.systemd.disable_now(names)
    print(f"stopped and disabled: {', '.join(names)}")
    if args.revoke and ctx.settings.mode == "tunnel":
        removed = remove_tree(ctx.layout.tunnel_key_file)
        print(
            f"revoked: {'removed' if removed else 'no stored key at'} {ctx.layout.tunnel_key_file}; "
            "also revoke the key in the OpenAI Platform, this deletes only the local copy"
        )
    elif args.revoke:
        remove_tree(ctx.layout.data_dir)
        remove_tree(ctx.layout.clients_file)
        print(f"revoked: removed {ctx.layout.data_dir} and the client table; ChatGPT must connect again")
    return 0


COMMAND = Command(
    name="end", help="stop and disable the stack (--revoke also drops all OAuth sessions)", run=run, configure=configure
)

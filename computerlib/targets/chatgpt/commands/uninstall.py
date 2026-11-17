"""`computer chatgpt uninstall`: stop the stack, remove the unit files, and with `--purge` also every file this target created.

Without `--purge` the OAuth state, the password, the tunnel runtime key, the access log, the installed binaries and the
cloudflared state are kept (a re-install resumes with the same sessions and tunnel). `--purge` removes them, including the default
cloudflared directory inside this target's config; a `--cloudflared-dir` the user pointed elsewhere (e.g. ~/.cloudflared) is never
touched.
Never removed here: the shared Desktop Commander (`computer mcp`, other clients use it), the Cloudflare tunnel and its DNS record
on the account (deleting them is the account owner's call), and the OpenAI runtime key in the Platform.
"""

from __future__ import annotations

import argparse

from computerlib.commandtree import Command
from computerlib.targets.chatgpt.context import load_context
from computerlib.targets.chatgpt.lifecycle import unit_names
from computerlib.targets.chatgpt.removal import remove_tree
from computerlib.targets.chatgpt.settings import config_dir


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--purge",
        action="store_true",
        help="also delete config, password, tunnel key, OAuth state, access log and installed binaries",
    )


def run(args: argparse.Namespace) -> int:
    ctx = load_context()
    names = list(reversed(unit_names(ctx)))
    ctx.systemd.disable_now(names)
    ctx.systemd.remove(names)
    print(f"removed units: {', '.join(names)}")
    if args.purge:
        owned = [ctx.layout.install_dir, ctx.layout.data_dir, ctx.layout.state_dir]
        if ctx.settings.mode == "tunnel":
            owned.append(ctx.layout.tunnel_key_file)
        for path in (*owned, config_dir()):
            print(f"deleted {path}" if remove_tree(path) else f"already absent: {path}")
    if ctx.settings.tunnel_name:
        print(
            f"kept on Cloudflare: tunnel {ctx.settings.tunnel_name} and the DNS record {ctx.settings.hostname}; "
            "delete them in the "
            "dashboard (Zero Trust -> Networks -> Tunnels, and the zone's DNS) if you no longer need them"
        )
    return 0


COMMAND = Command(name="uninstall", help="remove the units (--purge also all data and binaries)", run=run, configure=configure)

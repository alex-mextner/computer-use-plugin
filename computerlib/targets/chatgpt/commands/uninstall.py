"""`computer chatgpt uninstall`: stop the stack, remove the unit files, and with `--purge` also every file this target created.

Without `--purge` the OAuth state, the password, the tunnel runtime key, the access log, the installed binaries and the
cloudflared state are kept (a re-install resumes with the same sessions and tunnel). `--purge` removes them, including the default
cloudflared directory inside this target's config; a `--cloudflared-dir` the user pointed elsewhere (e.g. ~/.cloudflared) is never
touched. Without a config (a first install that stopped before saving it, a config deleted by hand) `--purge` stops and removes
the units of both modes under the default prefix and every unit file this tool wrote under another one (`--unit-prefix`),
recognised by the marker line `render_unit` puts first, so no stack keeps running on the state it deletes.
Never removed here: the shared Desktop Commander (`computer mcp`, other clients use it), the Cloudflare tunnel and its DNS record
on the account (deleting them is the account owner's call), and the OpenAI runtime key in the Platform.
"""

from __future__ import annotations

import argparse

from computerlib.commandtree import Command
from computerlib.targets.chatgpt.context import Context, load_context
from computerlib.targets.chatgpt.lifecycle import unit_names
from computerlib.targets.chatgpt.processes import TUNNEL_SERVICE
from computerlib.targets.chatgpt.removal import remove_tree
from computerlib.targets.chatgpt.settings import config_dir, config_file
from computerlib.targets.chatgpt.stack import SERVICES
from computerlib.targets.chatgpt.units import managed_prefixes, unit_name, written_here


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--purge",
        action="store_true",
        help="also delete config, password, tunnel key, OAuth state, access log and installed binaries",
    )


def _units_without_config(ctx: Context) -> tuple[list[str], list[str]]:
    """(the units to stop, those of them whose file is ours to disable and remove) when no config names the mode or prefix.

    Both modes' units under the default prefix and under every prefix a unit file of this tool carries. A unit keeps running
    after its file is deleted, so a name without a file is still stopped; a file without the marker under a discovered prefix
    is the user's and left alone."""
    services = (TUNNEL_SERVICE, *reversed(SERVICES))
    default = ctx.settings.unit_prefix
    stop: list[str] = []
    owned: list[str] = []
    for prefix in dict.fromkeys([default, *managed_prefixes(ctx.layout.unit_dir, services)]):
        for service in services:
            name = unit_name(prefix, service)
            path = ctx.layout.unit_dir / name
            if not path.exists():
                stop.append(name)
            elif prefix == default or written_here(path):
                stop.append(name)
                owned.append(name)
    return stop, owned


def _remove_units(ctx: Context, configured: bool) -> list[str]:
    if configured:
        names = list(reversed(unit_names(ctx)))
        ctx.systemd.disable_now(names)
    else:
        names, owned = _units_without_config(ctx)
        # `stop` goes on past an unknown unit; `disable` drops the whole list at the first one, stopping none.
        ctx.systemd.stop(names)
        if owned:
            ctx.systemd.disable_now(owned)
    ctx.systemd.remove(names)
    return names


def _purge(ctx: Context) -> None:
    owned = [ctx.layout.install_dir, ctx.layout.data_dir, ctx.layout.state_dir]
    if ctx.settings.mode == "tunnel":
        owned.append(ctx.layout.tunnel_key_file)
    for path in (*owned, config_dir()):
        print(f"deleted {path}" if remove_tree(path) else f"already absent: {path}")


def run(args: argparse.Namespace) -> int:
    # --purge also cleans up after a first install that stopped before saving the config (the account certificate is on disk).
    configured = config_file().exists()
    ctx = load_context(configured=not args.purge)
    print(f"removed units: {', '.join(_remove_units(ctx, configured))}")
    if args.purge:
        _purge(ctx)
    if ctx.settings.tunnel_name:
        print(
            f"kept on Cloudflare: tunnel {ctx.settings.tunnel_name} and the DNS record {ctx.settings.hostname}; "
            "delete them in the "
            "dashboard (Zero Trust -> Networks -> Tunnels, and the zone's DNS) if you no longer need them"
        )
    return 0


COMMAND = Command(name="uninstall", help="remove the units (--purge also all data and binaries)", run=run, configure=configure)

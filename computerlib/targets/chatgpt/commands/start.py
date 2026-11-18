"""`computer chatgpt start`: enable the units (they also start at boot) and wait until the stack serves (tunnel mode: until
tunnel-client is ready and polling the control plane). `computer chatgpt install` runs the same `start_stack` as one of its
steps."""

from __future__ import annotations

import argparse
import getpass

from agenttools_errors import DriftError, MissingTargetError

from computerlib.commandtree import Command
from computerlib.targets.chatgpt.context import Context, load_context
from computerlib.targets.chatgpt.lifecycle import not_serving_reason, unit_names, wait_until_serving
from computerlib.targets.chatgpt.tunnel import has_key, require_tunnel_id


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--no-wait", action="store_true", help="return as soon as the units are started")
    parser.add_argument(
        "--no-linger", action="store_true", help="do not enable lingering (the stack then stops when you log out)"
    )


def _require_inputs(ctx: Context, names: list[str]) -> None:
    """Everything the units need on disk: the tunnel key (tunnel mode) and every unit file."""
    if ctx.settings.mode == "tunnel":
        require_tunnel_id(ctx.settings.tunnel_id)
        if not has_key(ctx.layout.tunnel_key_file):
            raise MissingTargetError(
                what="the tunnel runtime key is not stored",
                why=f"{ctx.layout.tunnel_key_file} is missing or empty, so tunnel-client could not authenticate",
                fix="run `computer chatgpt login --key-stdin` (or without the flag for a hidden prompt)",
            )
    missing = [n for n in names if not (ctx.layout.unit_dir / n).exists()]
    if missing:
        raise MissingTargetError(
            what=f"unit file missing: {', '.join(missing)}",
            why=f"not found in {ctx.layout.unit_dir}",
            fix="run `computer chatgpt install` (or `computer chatgpt init`)",
        )


def start_stack(ctx: Context, *, linger: bool = True, wait: bool = True, restart: tuple[str, ...] = ()) -> None:
    """Enable and start every unit; `restart` names units whose files just changed (a running unit keeps its old command line
    until it is restarted)."""
    names = unit_names(ctx)
    _require_inputs(ctx, names)
    if linger and not ctx.systemd.lingering(getpass.getuser()):
        ctx.systemd.enable_lingering(getpass.getuser())
        print("lingering enabled: the stack starts at boot without a login")
    # Only a unit that was already running keeps its old command line; one that `enable --now` starts reads the new file anyway.
    changed = [name for name in names if name in restart and ctx.systemd.state(name).active == "active"]
    ctx.systemd.enable_now(names)
    if changed:
        ctx.systemd.restart(changed)
    print(f"started: {', '.join(names)}" + (f" (restarted for the new unit files: {', '.join(changed)})" if changed else ""))
    if not wait:
        return
    if not wait_until_serving(ctx):
        raise DriftError(
            what="the stack did not start serving in time",
            why=not_serving_reason(ctx),
            fix="run `computer chatgpt doctor` and `journalctl --user -u " + names[0] + "`",
        )
    print("serving locally")


def run(args: argparse.Namespace) -> int:
    start_stack(load_context(), linger=not args.no_linger, wait=not args.no_wait)
    return 0


COMMAND = Command(name="start", help="enable and start the stack, wait until it serves", run=run, configure=configure)

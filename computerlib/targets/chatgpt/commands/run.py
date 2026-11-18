"""`computer chatgpt run`: run the stack in the foreground without systemd, until Ctrl-C (development, proofs on scratch ports).

Uses the installed pinned binaries and the configured ports; point it at scratch ports and a scratch HOME to leave a running
install alone. Ctrl-C stops every process.
"""

from __future__ import annotations

import argparse

from agenttools_errors import MissingTargetError, UsageError

from computerlib import desktop_commander
from computerlib.commandtree import Command
from computerlib.targets.chatgpt.context import Context, load_context
from computerlib.targets.chatgpt.processes import processes as mode_processes
from computerlib.targets.chatgpt.stack import module_launcher
from computerlib.targets.chatgpt.supervise import run_foreground
from computerlib.targets.chatgpt.tunnel import has_key, require_tunnel_id


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--no-tunnel", action="store_true", help="do not start cloudflared even when a tunnel is configured (public mode)"
    )


def _check_tunnel_inputs(ctx: Context, no_tunnel: bool) -> None:
    require_tunnel_id(ctx.settings.tunnel_id)
    if no_tunnel:
        raise UsageError(
            what="--no-tunnel has no meaning in tunnel mode", why="tunnel-client is the only process", fix="drop the flag"
        )
    if not has_key(ctx.layout.tunnel_key_file):
        raise MissingTargetError(
            what="the tunnel runtime key is not stored",
            why=f"{ctx.layout.tunnel_key_file} is missing or empty",
            fix="run `computer chatgpt login --key-stdin`",
        )


def run(args: argparse.Namespace) -> int:
    ctx = load_context()
    tunnel_mode = ctx.settings.mode == "tunnel"
    if tunnel_mode:
        _check_tunnel_inputs(ctx, args.no_tunnel)
    processes = mode_processes(ctx.settings, ctx.layout, module_launcher())
    if args.no_tunnel:
        processes = [p for p in processes if p.name != "tunnel"]
    desktop_commander.launch_spec()  # exit 5 with the fix when `computer mcp install` has not run
    required = [ctx.layout.tunnel_client_binary if tunnel_mode else ctx.layout.bin_dir / "mcp-auth-proxy"]
    required += [ctx.layout.bin_dir / "cloudflared"] if any(p.name == "tunnel" for p in processes) else []
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise MissingTargetError(
            what=f"not installed: {', '.join(missing)}", why="the pinned downloads are missing", fix="run `computer chatgpt init`"
        )
    print("running in the foreground: " + ", ".join(p.name for p in processes) + " (Ctrl-C stops all)", flush=True)
    return run_foreground(processes)


COMMAND = Command(name="run", help="run the stack in the foreground without systemd", run=run, configure=configure)

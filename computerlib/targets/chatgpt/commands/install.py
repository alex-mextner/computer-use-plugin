"""`computer chatgpt install --hostname <name>`: the whole public ChatGPT setup in ONE command, safe to re-run.

Chain (each step is idempotent, the building blocks stay available as `init`, `login`, `start`, `connect`):
  1. prerequisites, before anything is written: node + npm, a reachable systemd user manager and loginctl, the `computer`
     executable the units will run, the hostname's zone served by Cloudflare (DNS-over-HTTPS lookup), and loopback ports that are
     free or already this stack's
  2. `init`: config, generated login password (0600, never printed), the shared Desktop Commander (`computer mcp install`), pinned
     and sha256-verified mcp-auth-proxy and cloudflared, systemd --user unit files
  3. `login`: Cloudflare login when this target has no certificate yet (the only interactive step: a browser page), the named
     tunnel, the DNS route (a record that points elsewhere is replaced only after a question, or with --yes)
  4. `start`: lingering, enable + start the units (restarting any whose file changed), wait until the local chain serves
  5. wait until the public URL answers 401 with the WWW-Authenticate resource_metadata challenge ChatGPT's OAuth discovery needs
  6. print the exact fields of ChatGPT's "Add custom MCP server" dialog and where the password is
It always configures the public mode; the experimental OpenAI tunnel mode keeps its separate `init --mode tunnel` flow.
"""

from __future__ import annotations

import argparse
import shutil
import socket
import time
from collections.abc import Callable
from dataclasses import replace

from agenttools_errors import ConfigError, MissingDepError, NetworkError, UsageError

from computerlib.commandtree import Command
from computerlib.targets.chatgpt import zone
from computerlib.targets.chatgpt.commands.connect import print_instructions
from computerlib.targets.chatgpt.commands.init import add_public_options, apply_flags, prepare
from computerlib.targets.chatgpt.commands.login import default_tunnel_name, require_hostname, require_tunnel_name, setup_tunnel
from computerlib.targets.chatgpt.commands.start import start_stack
from computerlib.targets.chatgpt.context import load_context
from computerlib.targets.chatgpt.probe import challenge_ok
from computerlib.targets.chatgpt.settings import Layout, Settings, load_settings
from computerlib.targets.chatgpt.stack import external_url, unit_launcher
from computerlib.targets.chatgpt.systemd import Systemd
from computerlib.targets.chatgpt.units import unit_name

DEFAULT_PUBLIC_TIMEOUT_SECONDS = 180.0
PUBLIC_POLL_SECONDS = 3.0


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--hostname", help="public hostname for ChatGPT inside a domain (zone) on your Cloudflare account; required once"
    )
    parser.add_argument(
        "--tunnel-name", help="Cloudflare tunnel name (default: the saved one, else computer-<hostname with dashes>)"
    )
    add_public_options(parser)
    parser.add_argument("--yes", action="store_true", help="replace an existing DNS record of the hostname without asking")
    parser.add_argument(
        "--skip-dns", action="store_true", help="do not check the zone or touch DNS (you route the hostname yourself)"
    )
    parser.add_argument(
        "--no-linger", action="store_true", help="do not enable lingering (the stack then stops when you log out)"
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_PUBLIC_TIMEOUT_SECONDS,
        help="seconds to wait for the public URL to answer (default 180)",
    )
    parser.add_argument(
        "--name", default="Computer", help="connector name shown in the printed ChatGPT fields (default Computer)"
    )


def _settings(args: argparse.Namespace) -> Settings:
    saved = load_settings()
    if saved.mode == "tunnel" and args.hostname is None:
        raise UsageError(
            what="this machine is set up for the experimental tunnel mode",
            why="install configures the public (Cloudflare) mode and needs a hostname for it",
            fix="run `computer chatgpt uninstall`, then `computer chatgpt install --hostname <name>`",
        )
    settings = apply_flags(replace(saved, mode="public", scheme="https"), args)
    if not settings.hostname:
        raise UsageError(
            what="no hostname",
            why="ChatGPT reaches this PC through a public HTTPS hostname on your Cloudflare zone",
            fix="run `computer chatgpt install --hostname computer.<your domain on Cloudflare>`",
        )
    hostname = require_hostname(settings.hostname)
    tunnel_name = require_tunnel_name(args.tunnel_name or settings.tunnel_name or default_tunnel_name(hostname))
    return replace(settings, hostname=hostname, tunnel_name=tunnel_name)


def _require_tools() -> None:
    for tool, why, package in (
        ("node", "Desktop Commander runs on Node.js", "nodejs"),
        ("npm", "the pinned Desktop Commander is installed with npm", "npm"),
        ("loginctl", "lingering keeps the stack running without a login", "systemd"),
    ):
        if shutil.which(tool) is None:
            raise MissingDepError(
                what=f"`{tool}` is not installed", why=why, fix="install it, then re-run", install=f"sudo apt install {package}"
            )


def _require_zone(hostname: str) -> None:
    found = zone.find_zone(hostname)
    if found.on_cloudflare is None:
        print(f"zone: could not look up the nameservers of {hostname} (DNS-over-HTTPS failed); the DNS step will tell")
    elif not found.on_cloudflare:
        where = (
            f"{found.name} is served by {', '.join(found.nameservers)}" if found.name else f"no DNS zone was found for {hostname}"
        )
        raise ConfigError(
            what=f"{hostname} is not in a zone on Cloudflare",
            why=f"{where}; a Cloudflare tunnel is reachable only through a hostname in a Cloudflare zone",
            fix=(
                "add the domain to your Cloudflare account (and switch its nameservers), "
                "or pick a hostname under a domain already there"
            ),
        )
    else:
        print(f"zone: {found.name} is on Cloudflare ({', '.join(found.nameservers)})")


def _listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1):
            return True
    except OSError:
        return False


def _require_ports(settings: Settings, systemd: Systemd) -> None:
    """Every loopback port must be free or held by this stack's own unit (a re-run): never fight another process for it."""
    for port, service, flag in (
        (settings.proxy_port, "bridge", "--proxy-port"),
        (settings.shim_port, "shim", "--shim-port"),
        (settings.metrics_port, "tunnel", "--metrics-port"),
    ):
        unit = unit_name(settings.unit_prefix, service)
        if _listening(port) and systemd.state(unit).active != "active":
            raise ConfigError(
                what=f"127.0.0.1:{port} is already in use by another process",
                why=f"the {service} of this stack ({unit}) would listen there and it is not the one running",
                fix=f"pass {flag} <free port> (the choice is saved for re-runs)",
            )


def prerequisites(settings: Settings, *, skip_dns: bool) -> None:
    _require_tools()
    systemd = Systemd(Layout.of(settings).unit_dir)
    systemd.require_manager()
    unit_launcher()  # the units run this executable: fail now, not after the login
    if not skip_dns:
        _require_zone(settings.hostname)
    _require_ports(settings, systemd)


def wait_public(
    base_url: str,
    timeout: float,
    check: Callable[[str], tuple[bool, str]],
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[bool, str]:
    """Poll the public URL until it answers 401 with the challenge (DNS and the tunnel's edge connection take a moment)."""
    deadline = clock() + timeout
    ok, detail = check(base_url)
    while not ok and clock() < deadline:
        sleep(PUBLIC_POLL_SECONDS)
        ok, detail = check(base_url)
    return ok, detail


def run(args: argparse.Namespace) -> int:
    settings = _settings(args)
    print(f"computer chatgpt install: {settings.hostname} via Cloudflare tunnel {settings.tunnel_name}")
    prerequisites(settings, skip_dns=args.skip_dns)
    layout, changed_units = prepare(settings)
    if not setup_tunnel(settings, layout, assume_yes=args.yes, skip_dns=args.skip_dns):
        raise ConfigError(
            what=f"{settings.hostname} still points somewhere else",
            why="its existing DNS record was kept, so ChatGPT would not reach this PC",
            fix="re-run with --yes to replace the record, or choose another --hostname",
        )
    ctx = load_context()
    start_stack(ctx, linger=not args.no_linger, restart=tuple(changed_units))
    base = external_url(settings)
    print(f"waiting for {base}/mcp to answer through the tunnel (up to {args.timeout:.0f} s) ...")
    ok, detail = wait_public(base, args.timeout, challenge_ok)
    print()
    print_instructions(ctx, args.name, f"{base}/mcp")
    if not ok:
        raise NetworkError(
            what=f"{base}/mcp is not reachable yet: {detail}",
            why="everything on this PC is up; DNS, the tunnel's edge connection or a local DNS cache has not caught up",
            fix=(
                "wait a minute and run `computer chatgpt doctor` "
                "(or re-run this install: it changes nothing that is already done)"
            ),
        )
    print(f"ready: {base}/mcp answers with the OAuth challenge ChatGPT needs")
    print("next: add the connector in ChatGPT as shown above; `computer chatgpt connect` watches it connect live")
    return 0


COMMAND = Command(
    name="install",
    help=(
        "set up everything for ChatGPT in one command: "
        "Desktop Commander, pinned proxy + cloudflared, tunnel, DNS, units, password"
    ),
    run=run,
    configure=configure,
)

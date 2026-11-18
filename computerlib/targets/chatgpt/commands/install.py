"""`computer chatgpt install --domain <zone>` (or `--hostname <name>`): the whole public ChatGPT setup in ONE command, safe to
re-run, once per machine: `--domain` names the public hostname after this machine (`<machine>-mcp.<zone>`, machine.py) and the
Cloudflare tunnel after the machine too, so every machine gets its own URL and ChatGPT connector.

Chain (each step is idempotent, the building blocks stay available as `init`, `login`, `start`, `connect`):
  1. prerequisites, before anything is written: a pinned build of every binary for this CPU, node + npm, a reachable systemd
     user manager and loginctl, the `computer` executable the units will run, the hostname's zone served by Cloudflare
     (DNS-over-HTTPS lookup), loopback ports that are free or already this stack's, and a `--cloudflare-cert` that reads as an
     account certificate
  2. `init` without the config: generated login password (0600, never printed), the shared Desktop Commander (`computer mcp
     install`), pinned and sha256-verified mcp-auth-proxy and cloudflared
  3. `login`: Cloudflare login when this target has no certificate yet (the only interactive step: a URL to open in a browser on
     any device; `--cloudflare-cert` reuses an existing account certificate instead), the named tunnel (an existing one without
     local credentials is refused unless named with --tunnel-name), the DNS route (a record that points elsewhere is replaced only
     after a question, or with --yes)
  4. config.toml and the systemd --user unit files, only now: a run that stops in `login` leaves the old ones, so `connect`,
     `doctor` and `uninstall` still describe the running stack and the corrected re-run sees the units change and restarts them
  5. `start`: lingering, enable + start the units (restarting any whose file changed), wait until the local chain serves
  6. wait until the public URL answers 401 with the WWW-Authenticate resource_metadata challenge ChatGPT's OAuth discovery needs
  7. print the exact fields of ChatGPT's "Add custom MCP server" dialog and where the password is
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
from computerlib.targets.chatgpt.cloudflare import read_account_cert
from computerlib.targets.chatgpt.commands.connect import connector_name, print_instructions
from computerlib.targets.chatgpt.commands.init import add_public_options, apply_flags, install_units, prepare, require_pins
from computerlib.targets.chatgpt.commands.login import add_cert_option, record_kept_error, require_tunnel_name, setup_tunnel
from computerlib.targets.chatgpt.commands.start import start_stack
from computerlib.targets.chatgpt.context import load_context
from computerlib.targets.chatgpt.machine import require_hostname
from computerlib.targets.chatgpt.probe import challenge_ok
from computerlib.targets.chatgpt.settings import Layout, Settings, load_settings, save_settings
from computerlib.targets.chatgpt.stack import external_url, unit_launcher
from computerlib.targets.chatgpt.systemd import Systemd
from computerlib.targets.chatgpt.units import unit_name

DEFAULT_PUBLIC_TIMEOUT_SECONDS = 180.0
PUBLIC_POLL_SECONDS = 3.0


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--hostname",
        help="public hostname in full, inside a domain (zone) on your Cloudflare account (instead of --domain; saved)",
    )
    parser.add_argument(
        "--tunnel-name",
        help=(
            "Cloudflare tunnel name (default: the saved one, else the machine name); naming an existing tunnel this machine has "
            "no credentials for adopts it"
        ),
    )
    add_public_options(parser)
    add_cert_option(parser)
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
    parser.add_argument("--name", help="connector name shown in the printed ChatGPT fields (default: the machine name)")


def _settings(args: argparse.Namespace) -> Settings:
    saved = load_settings()
    if saved.mode == "tunnel" and args.hostname is None and args.domain is None:
        raise UsageError(
            what="this machine is set up for the experimental tunnel mode",
            why="install configures the public (Cloudflare) mode and needs a domain or hostname for it",
            fix="run `computer chatgpt uninstall`, then `computer chatgpt install --domain <your domain on Cloudflare>`",
        )
    settings = apply_flags(replace(saved, mode="public", scheme="https"), args, cloudflare=True)
    if not settings.hostname:
        raise UsageError(
            what="no hostname",
            why="ChatGPT reaches this PC through a public HTTPS hostname on your Cloudflare zone",
            fix=(
                "run `computer chatgpt install --domain <your domain on Cloudflare>` (hostname <machine>-mcp.<domain>), "
                "or --hostname <full name>"
            ),
        )
    return replace(settings, hostname=require_hostname(settings.hostname), tunnel_name=require_tunnel_name(settings.tunnel_name))


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


def _require_zone(settings: Settings) -> None:
    hostname = settings.hostname
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
    elif settings.domain and found.name != settings.domain:
        raise ConfigError(
            what=f"{settings.domain} is not a zone itself: {hostname} lies in the zone {found.name}",
            why=(
                "Cloudflare's free Universal SSL certificate covers only names one label below the zone; "
                f"{hostname} would get no valid certificate"
            ),
            fix=f"pass --domain {found.name}",
        )
    else:
        print(f"zone: {found.name} is on Cloudflare ({', '.join(found.nameservers)})")
        if hostname.count(".") > found.name.count(".") + 1:
            print(
                f"warning: {hostname} is more than one label below the zone {found.name}: Cloudflare's free Universal SSL "
                "certificate does not cover it (an Advanced Certificate does)"
            )


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
    require_pins(settings)  # this CPU has a pinned build of every binary: nothing else matters otherwise
    _require_tools()
    systemd = Systemd(Layout.of(settings).unit_dir)
    systemd.require_manager()
    unit_launcher()  # the units run this executable: fail now, not after the login
    if not skip_dns:
        _require_zone(settings)
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
    print(
        f"computer chatgpt install: machine {settings.machine_name}, {settings.hostname} via Cloudflare tunnel "
        f"{settings.tunnel_name}"
    )
    prerequisites(settings, skip_dns=args.skip_dns)
    cert = read_account_cert(args.cloudflare_cert) if args.cloudflare_cert is not None else None
    layout = prepare(settings, save=False)
    if not setup_tunnel(
        settings,
        layout,
        assume_yes=args.yes,
        skip_dns=args.skip_dns,
        adopt=args.tunnel_name is not None,
        cert=cert,
    ):
        raise record_kept_error(settings)
    print(f"config: {save_settings(settings)}")
    changed_units = install_units(settings, layout)
    ctx = load_context()
    start_stack(ctx, linger=not args.no_linger, restart=tuple(changed_units))
    base = external_url(settings)
    print(f"waiting for {base}/mcp to answer through the tunnel (up to {args.timeout:.0f} s) ...")
    ok, detail = wait_public(base, args.timeout, challenge_ok)
    print()
    print_instructions(ctx, connector_name(ctx, args), f"{base}/mcp")
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

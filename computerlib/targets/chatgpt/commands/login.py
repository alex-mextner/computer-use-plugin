"""`computer chatgpt login`: public mode: set up the Cloudflare named tunnel for the public hostname (login, create the tunnel,
route the DNS name). Tunnel mode: store the OpenAI runtime API key (Tunnels Read+Use) for tunnel-client.
`computer chatgpt install` runs the public half (`setup_tunnel`) as one of its steps.

Each step is skipped when it is already done, so re-running is safe. Creating a NEW DNS record needs no question (the hostname was
given for exactly that); replacing an EXISTING record that points elsewhere asks first (`--yes` answers it). cloudflared's
certificate, credentials and config live in this target's own directory (`--cloudflared-dir`), never in ~/.cloudflared unless
pointed there; nothing secret or identifying (tunnel id, credentials) is printed.

Tunnel mode never takes the key as an argument (it would sit in the process list and the shell history): `--key-stdin` reads one
line from standard input, otherwise a hidden prompt asks for it. It is written to a 0600 file (`tunnel.key` next to config.toml),
which tunnel-client reads as `file:<path>`; it is never echoed, printed or logged. login never changes the mode.
"""

from __future__ import annotations

import argparse
import getpass
import re
import sys
from dataclasses import replace

from agenttools_errors import ConfigError, MissingTargetError, UsageError

from computerlib.commandtree import Command
from computerlib.targets.chatgpt.cloudflare import Cloudflare, Route
from computerlib.targets.chatgpt.context import Context, load_context
from computerlib.targets.chatgpt.settings import Layout, Settings, save_settings
from computerlib.targets.chatgpt.tunnel import write_key

HOSTNAME = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$")
TUNNEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tunnel-name", help="name of the Cloudflare tunnel (default: the saved one, else computer-<hostname with dashes>)"
    )
    parser.add_argument("--yes", action="store_true", help="replace an existing DNS record of the hostname without asking")
    parser.add_argument(
        "--key-stdin",
        action="store_true",
        help="tunnel mode: read the runtime API key from standard input (else a hidden prompt)",
    )
    parser.add_argument("--skip-dns", action="store_true", help="do not touch DNS (you route the hostname yourself)")


def require_hostname(hostname: str) -> str:
    """The hostname as a lowercase DNS name a Cloudflare zone can hold, or a usage error."""
    name = hostname.strip().lower().rstrip(".")
    if not HOSTNAME.match(name):
        raise UsageError(
            what=f"{hostname!r} is not a public hostname",
            why="it must be a DNS name inside a domain (zone) on your Cloudflare account, e.g. computer.example.com",
            fix="pass --hostname <name>.<your domain on Cloudflare>",
        )
    return name


def default_tunnel_name(hostname: str) -> str:
    return f"computer-{hostname.replace('.', '-')}"[:63].rstrip("-.")


def require_tunnel_name(name: str) -> str:
    if not TUNNEL_NAME.match(name):
        raise UsageError(
            what=f"{name!r} cannot name a tunnel here",
            why="the name is also the file name of its credentials: letters, digits, '_', '.', '-', at most 63",
            fix="pass another --tunnel-name",
        )
    return name


def confirmed(question: str, assume_yes: bool) -> bool:
    if assume_yes:
        return True
    if not sys.stdin.isatty():
        return False
    return input(f"{question} [y/N] ").strip().lower() in ("y", "yes")


def _read_key(from_stdin: bool) -> str:
    if from_stdin:
        key = sys.stdin.readline().strip()
    elif sys.stdin.isatty():
        key = getpass.getpass("OpenAI runtime API key (Tunnels Read+Use; input is hidden): ").strip()
    else:
        raise UsageError(
            what="no terminal to ask for the key",
            why="the key is never taken from an argument, and standard input is not a terminal",
            fix="pipe it in: `computer chatgpt login --key-stdin < keyfile`",
        )
    if not key or any(c.isspace() for c in key):
        raise UsageError(
            what="that is not a runtime API key", why="it is empty or contains whitespace", fix="paste the key as one line"
        )
    return key


def run_tunnel(ctx: Context, args: argparse.Namespace) -> int:
    if args.tunnel_name or args.yes or args.skip_dns:
        raise UsageError(
            what="--tunnel-name, --yes and --skip-dns belong to the public mode",
            why="in tunnel mode login only stores the runtime key",
            fix="drop them",
        )
    write_key(ctx.layout.tunnel_key_file, _read_key(args.key_stdin))
    print(f"runtime key stored in {ctx.layout.tunnel_key_file} (mode 600, never printed)")
    print("next: computer chatgpt start (or restart, when it is already running)")
    return 0


def _ensure_tunnel(cloudflare: Cloudflare, name: str, hostname: str) -> None:
    if cloudflare.logged_in():
        print(f"cloudflare: logged in (certificate in {cloudflare.directory})")
    else:
        print(f"cloudflare: log in and authorize the zone (domain) of {hostname} in the browser page that opens")
        cloudflare.login()
        print(f"cloudflare: logged in, certificate stored in {cloudflare.directory} (mode 600)")
    if not cloudflare.tunnel_exists(name):
        cloudflare.create_tunnel(name)
        print(f"tunnel {name}: created")
    elif cloudflare.credentials(name).is_file():
        print(f"tunnel {name}: exists")
    else:
        cloudflare.fetch_credentials(name)
        print(f"tunnel {name}: exists, credentials fetched into {cloudflare.directory}")


def _route(cloudflare: Cloudflare, name: str, hostname: str, assume_yes: bool) -> bool:
    """Route the hostname; True when it points at the tunnel afterwards, False when the user kept an existing record."""
    route = cloudflare.route_dns(name, hostname)
    if route is Route.CONFLICT:
        question = f"A DNS record for {hostname} already exists and does not point at tunnel {name}. Replace it?"
        if not confirmed(question, assume_yes):
            print(f"dns {hostname}: kept the existing record (re-run with --yes to replace it, or choose another --hostname)")
            return False
        route = cloudflare.route_dns(name, hostname, overwrite=True)
        print(f"dns {hostname}: replaced, now routed to tunnel {name}")
        return True
    print(f"dns {hostname}: {'routed to tunnel ' + name if route is Route.CREATED else 'already routed to tunnel ' + name}")
    return True


def setup_tunnel(settings: Settings, layout: Layout, *, assume_yes: bool, skip_dns: bool) -> bool:
    """Login, tunnel and DNS for `settings.hostname` / `settings.tunnel_name`; True when the hostname routes to the tunnel (or DNS
    is the user's business: `skip_dns`)."""
    binary = layout.bin_dir / "cloudflared"
    if not binary.exists():
        raise MissingTargetError(
            what="cloudflared is not installed",
            why=f"{binary} does not exist",
            fix="run `computer chatgpt install --hostname <name>` (or `init --tunnel-name <name>`) to install the pinned build",
        )
    hostname = require_hostname(settings.hostname)
    name = require_tunnel_name(settings.tunnel_name)
    cloudflare = Cloudflare(binary, layout.cloudflared_dir)
    cloudflare.prepare()
    _ensure_tunnel(cloudflare, name, hostname)
    if skip_dns:
        print(f"dns: skipped; route it yourself: point {hostname} at {name} (cloudflared tunnel route dns {name} {hostname})")
        return True
    return _route(cloudflare, name, hostname, assume_yes)


def run(args: argparse.Namespace) -> int:
    ctx = load_context()
    if ctx.settings.mode == "tunnel":
        return run_tunnel(ctx, args)
    if args.key_stdin:
        raise UsageError(
            what="--key-stdin belongs to tunnel mode", why="the public mode logs in to Cloudflare", fix="drop the flag"
        )
    if not ctx.settings.hostname:
        raise ConfigError(
            what="no hostname to route", why="the config has no hostname", fix="run `computer chatgpt init --hostname <name>`"
        )
    name = require_tunnel_name(args.tunnel_name or ctx.settings.tunnel_name or default_tunnel_name(ctx.settings.hostname))
    settings = replace(ctx.settings, tunnel_name=name)
    setup_tunnel(settings, ctx.layout, assume_yes=args.yes, skip_dns=args.skip_dns)
    if ctx.settings.tunnel_name != name:
        save_settings(settings)
        print(f"config: tunnel_name = {name}; run `computer chatgpt init` to write the cloudflared unit")
    return 0


COMMAND = Command(
    name="login",
    help=(
        "building block of install: Cloudflare login, create the tunnel, route the hostname (tunnel mode: store the runtime key)"
    ),
    run=run,
    configure=configure,
)

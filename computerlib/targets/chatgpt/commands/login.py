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
from pathlib import Path

from agenttools_errors import ConfigError, MissingTargetError, UsageError

from computerlib.commandtree import Command
from computerlib.targets.chatgpt.cloudflare import AccountCert, Cloudflare, Route, read_account_cert
from computerlib.targets.chatgpt.context import Context, load_context
from computerlib.targets.chatgpt.machine import default_machine_name, is_uuid, require_hostname
from computerlib.targets.chatgpt.settings import Layout, Settings, save_settings
from computerlib.targets.chatgpt.tunnel import write_key

TUNNEL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")


def add_cert_option(parser: argparse.ArgumentParser) -> None:
    """`login` and `install`: set up another machine of the same Cloudflare account without a browser."""
    parser.add_argument(
        "--cloudflare-cert",
        type=Path,
        help=(
            "reuse an existing Cloudflare account certificate (cert.pem of `cloudflared tunnel login`) instead of the browser "
            "login; copied mode 600 into this target's cloudflared directory"
        ),
    )


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tunnel-name",
        help=(
            "name of the Cloudflare tunnel (default: the saved one, else the machine name); naming an existing tunnel this "
            "machine has no credentials for adopts it"
        ),
    )
    add_cert_option(parser)
    parser.add_argument("--yes", action="store_true", help="replace an existing DNS record of the hostname without asking")
    parser.add_argument(
        "--key-stdin",
        action="store_true",
        help="tunnel mode: read the runtime API key from standard input (else a hidden prompt)",
    )
    parser.add_argument("--skip-dns", action="store_true", help="do not touch DNS (you route the hostname yourself)")


def require_tunnel_name(name: str) -> str:
    if not TUNNEL_NAME.match(name):
        raise UsageError(
            what=f"{name!r} cannot name a tunnel here",
            why="the name is also the file name of its credentials: letters, digits, '_', '.', '-', at most 63",
            fix="pass another --tunnel-name",
        )
    if is_uuid(name):
        raise UsageError(
            what=f"{name!r} cannot name a tunnel",
            why="cloudflared refuses tunnel names that read as a UUID (it takes them for tunnel ids)",
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
    if args.tunnel_name or args.yes or args.skip_dns or args.cloudflare_cert:
        raise UsageError(
            what="--tunnel-name, --cloudflare-cert, --yes and --skip-dns belong to the public mode",
            why="in tunnel mode login only stores the runtime key",
            fix="drop them",
        )
    write_key(ctx.layout.tunnel_key_file, _read_key(args.key_stdin))
    print(f"runtime key stored in {ctx.layout.tunnel_key_file} (mode 600, never printed)")
    print("next: computer chatgpt start (or restart, when it is already running)")
    return 0


def _taken_fix(name: str, domain: str) -> str:
    """How to get past a tunnel name this machine has no credentials for. With a fixed hostname the machine name never moves the
    tunnel (machine.identify), so only --tunnel-name helps there."""
    adopt = f"adopt it with --tunnel-name {name} only when it is this machine's own tunnel whose files were lost"
    if domain:
        return (
            f"name this machine differently: `computer chatgpt install --domain {domain} --machine-name <name>`; {adopt} "
            "(or delete a stale one: Cloudflare dashboard, Zero Trust > Networks > Tunnels)"
        )
    return f"{adopt}; otherwise pass another --tunnel-name (the tunnel serving this hostname)"


def _ensure_tunnel(cloudflare: Cloudflare, name: str, hostname: str, *, adopt: bool, domain: str) -> None:
    if cloudflare.logged_in():
        print(f"cloudflare: logged in (certificate in {cloudflare.directory})")
    else:
        print(
            f"cloudflare: log in and authorize the zone (domain) of {hostname}: open the URL below in a browser on this or any "
            "other device; this command waits for it"
        )
        cloudflare.login()
        print(f"cloudflare: logged in, certificate stored in {cloudflare.directory} (mode 600)")
    if not cloudflare.tunnel_exists(name):
        cloudflare.create_tunnel(name)
        print(f"tunnel {name}: created")
    elif cloudflare.credentials(name).is_file():
        print(f"tunnel {name}: exists")
    elif adopt:
        cloudflare.fetch_credentials(name)
        print(f"tunnel {name}: exists, adopted (credentials fetched into {cloudflare.directory})")
    else:
        # Tunnel names are per account: two machines that share a hostname would share a tunnel and split ChatGPT's traffic.
        raise ConfigError(
            what=f"a Cloudflare tunnel named {name!r} already exists on the account and this machine has no credentials for it",
            why=(
                "another machine with the same name runs it, or an earlier install here lost its files; "
                "a tunnel is never taken over silently"
            ),
            fix=_taken_fix(name, domain),
        )


def _route(cloudflare: Cloudflare, name: str, hostname: str, assume_yes: bool, domain: str) -> bool:
    """Route the hostname; True when it points at the tunnel afterwards, False when the user kept an existing record."""
    route = cloudflare.route_dns(name, hostname)
    if route is Route.CONFLICT:
        owner = " It may be another machine's with the same name." if domain else ""
        question = f"A DNS record for {hostname} already exists and does not point at tunnel {name}.{owner} Replace it?"
        if not confirmed(question, assume_yes):
            other = "--machine-name" if domain else "--hostname"
            print(f"dns {hostname}: kept the existing record (re-run with --yes to replace it, or choose another {other})")
            return False
        route = cloudflare.route_dns(name, hostname, overwrite=True)
        print(f"dns {hostname}: replaced, now routed to tunnel {name}")
        return True
    print(f"dns {hostname}: {'routed to tunnel ' + name if route is Route.CREATED else 'already routed to tunnel ' + name}")
    return True


def setup_tunnel(
    settings: Settings, layout: Layout, *, assume_yes: bool, skip_dns: bool, adopt: bool = False, cert: AccountCert | None = None
) -> bool:
    """Login, tunnel and DNS for `settings.hostname` / `settings.tunnel_name`; True when the hostname routes to the tunnel (or DNS
    is the user's business: `skip_dns`). `adopt`: the tunnel name was given on this run, so an existing tunnel without local
    credentials is taken over. `cert`: an account certificate (already read and checked) to reuse instead of the browser login."""
    binary = layout.bin_dir / "cloudflared"
    if not binary.exists():
        raise MissingTargetError(
            what="cloudflared is not installed",
            why=f"{binary} does not exist",
            fix=(
                "run `computer chatgpt install --domain <your domain>` (or --hostname <full name>, "
                "or `init --tunnel-name <name>`) to install the pinned build"
            ),
        )
    hostname = require_hostname(settings.hostname)
    name = require_tunnel_name(settings.tunnel_name)
    cloudflare = Cloudflare(binary, layout.cloudflared_dir)
    cloudflare.prepare()
    if cert is not None:
        copied = cloudflare.import_cert(cert)
        print(f"cloudflare: account certificate {'copied from' if copied else 'already the same as'} {cert.source}")
    _ensure_tunnel(cloudflare, name, hostname, adopt=adopt, domain=settings.domain)
    if skip_dns:
        print(f"dns: skipped; route it yourself: point {hostname} at {name} (cloudflared tunnel route dns {name} {hostname})")
        return True
    return _route(cloudflare, name, hostname, assume_yes, settings.domain)


def run(args: argparse.Namespace) -> int:
    ctx = load_context()
    if ctx.settings.mode == "tunnel":
        return run_tunnel(ctx, args)
    if args.key_stdin:
        raise UsageError(
            what="--key-stdin belongs to tunnel mode", why="the public mode logs in to Cloudflare", fix="drop the flag"
        )
    cert = read_account_cert(args.cloudflare_cert) if args.cloudflare_cert is not None else None
    if not ctx.settings.hostname:
        raise ConfigError(
            what="no hostname to route", why="the config has no hostname", fix="run `computer chatgpt init --hostname <name>`"
        )
    # An explicit name (even an empty one from an unset shell variable) is validated as given: it alone may adopt a tunnel.
    explicit = args.tunnel_name is not None
    name = require_tunnel_name(
        args.tunnel_name
        if explicit
        else ctx.settings.tunnel_name or ctx.settings.machine_name or default_machine_name("--tunnel-name")
    )
    settings = replace(ctx.settings, tunnel_name=name)
    setup_tunnel(
        settings,
        ctx.layout,
        assume_yes=args.yes,
        skip_dns=args.skip_dns,
        adopt=explicit,
        cert=cert,
    )
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

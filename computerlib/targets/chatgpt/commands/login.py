"""`computer chatgpt login`: public mode: set up the Cloudflare named tunnel for the public hostname (login, create the tunnel,
route the DNS name). Tunnel mode: store the OpenAI runtime API key (Tunnels Read+Use) for tunnel-client.
`computer chatgpt install` runs the public half (`setup_tunnel`) as one of its steps.

Each step is skipped when it is already done, so re-running is safe. Creating a NEW DNS record needs no question (the hostname was
given for exactly that); replacing an EXISTING record that points elsewhere asks first (`--yes` answers it), and keeping it fails
with exit 2, since the hostname would not reach this machine (`--skip-dns` leaves DNS to the user and succeeds). cloudflared's
certificate, credentials and config live in this target's own directory (`--cloudflared-dir`), never in ~/.cloudflared unless
pointed there; nothing secret or identifying (tunnel id, credentials) is printed.

Two machines with the same name must never end up serving one public hostname (Cloudflare would split ChatGPT's traffic between
them, and the other machine's OAuth keys differ). So a tunnel is this machine's only when a local credentials file carries the
TunnelID `tunnel list` shows (an old file without TunnelID only when named `<tunnel id>.json`, or the same as such a file; the
id is then written into `<name>.json`); adopting one by `--tunnel-name` is refused while `tunnel info` reports live connections
(or no connection data for the tunnel or any of its connectors: unknown fails closed); DNS is routed by the checked tunnel id,
never the name (cloudflared resolves a name through a configured credentials-file first); and the existing DNS record of a
`--domain` hostname (`<machine>-mcp.<zone>`, the same on a same-name machine) is never replaced by `--yes`. Only
`--force-takeover` passes the adoption and record refusals.

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
from computerlib.targets.chatgpt.cloudflare import (
    AccountCert,
    Adoption,
    Cloudflare,
    CredentialsFile,
    Route,
    read_account_cert,
    read_credentials,
)
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


TAKEOVER_FLAG = "--force-takeover"


def add_takeover_option(parser: argparse.ArgumentParser) -> None:
    """`login` and `install`: the one way past the same-name guards, named so nobody passes it by habit like --yes."""
    parser.add_argument(
        TAKEOVER_FLAG,
        action="store_true",
        help=(
            "DANGEROUS: take over what may be ANOTHER machine's: adopt a tunnel named on this run that has live connections, "
            "or replace the existing DNS record of a hostname derived from the domain; the other machine's connector breaks"
        ),
    )


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--tunnel-name",
        help=(
            "name of the Cloudflare tunnel (default: the saved one, else the machine name); naming an existing tunnel this "
            "machine has no credentials for adopts it (refused while it has live connections, unless forced)"
        ),
    )
    add_cert_option(parser)
    add_takeover_option(parser)
    parser.add_argument("--yes", action="store_true", help="replace an existing DNS record of a --hostname without asking")
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
    if args.tunnel_name or args.yes or args.skip_dns or args.cloudflare_cert or args.force_takeover:
        raise UsageError(
            what="--tunnel-name, --cloudflare-cert, --yes, --force-takeover and --skip-dns belong to the public mode",
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


def _require_own_credentials(cloudflare: Cloudflare, name: str, tunnel_id: str, domain: str) -> None:
    """`<name>.json` exists: it must belong to the listed tunnel. A tunnel deleted and created again under the same name (most
    likely by a same-name machine) has a new id, and the old file would make this machine believe it owns that tunnel."""
    own = cloudflare.credentials(name)
    found = read_credentials(own)
    if found.tunnel_id == tunnel_id:
        print(f"tunnel {name}: exists")
        return
    if found.legacy:
        _require_bound_legacy_credentials(cloudflare, name, tunnel_id, found, domain)
        return
    raise ConfigError(
        what=f"REFUSED: {own} is NOT the credentials of the tunnel {name!r} that Cloudflare lists now",
        why=(
            "its TunnelID differs from the id of the tunnel with that name (or it is no credentials file at all): the tunnel was "
            "deleted and created again, most likely by another machine with the same name, so this machine does not own it"
        ),
        fix=f"move {own} aside (its tunnel no longer exists under this name), then: {_taken_fix(name, domain)}",
    )


def _require_bound_legacy_credentials(
    cloudflare: Cloudflare, name: str, tunnel_id: str, legacy: CredentialsFile, domain: str
) -> None:
    """`<name>.json` is `legacy`, an old file without TunnelID (cloudflared still runs it), as a copy of an old
    `<tunnel id>.json` under the tunnel name leaves it. Nothing in it says which tunnel it is, so it is taken only when this
    directory holds the same file under the listed tunnel's id. --force-takeover does not pass this: it overrides doubt about
    who RUNS a tunnel, while here it is unknown which tunnel the file is."""
    own = cloudflare.credentials(name)
    if cloudflare.migrate_legacy_credentials(name, tunnel_id, legacy):
        print(f"tunnel {name}: exists; its old credentials file matches the one named by tunnel id here, TunnelID added")
        return
    raise ConfigError(
        what=f"REFUSED: {own} is an old credentials file without a TunnelID",
        why=(
            f"nothing in it says which tunnel it belongs to, so it cannot be checked against the tunnel {name!r} that Cloudflare "
            "lists now (and a tunnel that old may have no credentials to fetch again); when its original no longer matches "
            "that tunnel, the tunnel was deleted and created again, most likely by another machine with the same name"
        ),
        fix=(
            f"if it is that tunnel's file, copy the original into {cloudflare.directory} under the name cloudflared gave it "
            "when it created the tunnel (`<tunnel id>.json`, for example from ~/.cloudflared) and re-run: that name binds it to "
            f"the tunnel and {name}.json gets its TunnelID. Never name a copy after the id Cloudflare lists now: only the name "
            "cloudflared gave it shows which tunnel the file is. If the original is refused too, or there is none, move "
            f"{own} aside, then: {_taken_fix(name, domain)}"
        ),
    )


def _adopt(cloudflare: Cloudflare, name: str, tunnel_id: str, *, force_takeover: bool, domain: str) -> None:
    """Fetch the credentials of an existing tunnel named on this run. A tunnel with live connections is running somewhere right
    now: on a same-name machine that serves its hostname, so it is taken only with --force-takeover; so is one whose
    connections Cloudflare's answer does not show (fail closed)."""
    live = cloudflare.live_connections(tunnel_id)
    state = "whose live connections Cloudflare did not report" if live is None else f"with {live} live connection(s)"
    if live != 0 and not force_takeover:
        running = "cannot tell whether it runs right now" if live is None else f"is RUNNING right now ({live} live connection(s))"
        raise ConfigError(
            what=f"REFUSED: the tunnel {name!r} {running} and this machine has no credentials for it",
            why=(
                "another machine (most likely one with the same name) may run it; adopting it here would split ChatGPT's "
                "traffic between the two machines and break that machine's connector (its OAuth keys differ)"
            ),
            fix=(
                f"stop it on the machine that runs it (`computer chatgpt stop`) and re-run, or {_taken_fix(name, domain)}; only "
                f"when you are SURE no other machine serves it, re-run with {TAKEOVER_FLAG}"
            ),
        )
    if live != 0:
        print(
            f"WARNING: {TAKEOVER_FLAG}: adopting the tunnel {name} {state}; any other machine running it now shares this "
            "machine's ChatGPT traffic"
        )
    cloudflare.fetch_credentials(name, tunnel_id)
    print(f"tunnel {name}: exists, adopted (credentials fetched into {cloudflare.directory})")


def _ensure_tunnel(cloudflare: Cloudflare, name: str, hostname: str, *, adopt: bool, force_takeover: bool, domain: str) -> str:
    """Log in, then make the tunnel `name` this machine's; its id, which every later step names it by."""
    if cloudflare.logged_in():
        print(f"cloudflare: logged in (certificate in {cloudflare.directory})")
    else:
        print(
            f"cloudflare: log in and authorize the zone (domain) of {hostname}: open the URL below in a browser on this or any "
            "other device; this command waits for it"
        )
        cloudflare.login()
        print(f"cloudflare: logged in, certificate stored in {cloudflare.directory} (mode 600)")
    tunnel_id = cloudflare.find_tunnel(name)
    if tunnel_id is None:
        tunnel_id = cloudflare.create_tunnel(name)
        print(f"tunnel {name}: created")
        return tunnel_id
    if cloudflare.credentials(name).exists():
        _require_own_credentials(cloudflare, name, tunnel_id, domain)
        return tunnel_id
    adoption = cloudflare.adopt_credentials_by_id(name, tunnel_id)
    if adoption is Adoption.COPIED:
        # A plain `cloudflared tunnel create` names the file after the tunnel id; its TunnelID matches, so it is ours.
        print(f"tunnel {name}: exists, this machine's credentials file (named by tunnel id) copied to {name}.json")
    elif adoption is Adoption.MIGRATED:
        print(
            f"tunnel {name}: exists, this machine's old credentials file (named by tunnel id, no TunnelID) copied to "
            f"{name}.json, TunnelID added"
        )
    elif adopt:
        _adopt(cloudflare, name, tunnel_id, force_takeover=force_takeover, domain=domain)
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
    return tunnel_id


def _route(
    cloudflare: Cloudflare, name: str, hostname: str, *, tunnel_id: str, assume_yes: bool, force_takeover: bool, domain: str
) -> bool:
    """Route the hostname to the tunnel `tunnel_id` (the checked one, called `name`; keyword-only so it cannot be swapped with
    the name); True when it points at the tunnel afterwards, False when the user kept an existing record. The record of a
    --domain hostname is replaced only with --force-takeover: a same-name machine derives the very same hostname."""
    route = cloudflare.route_dns(name, hostname, tunnel_id=tunnel_id)
    if route is not Route.CONFLICT:
        print(f"dns {hostname}: {'routed to tunnel ' + name if route is Route.CREATED else 'already routed to tunnel ' + name}")
        return True
    if domain and not force_takeover:
        raise ConfigError(
            what=f"REFUSED: the DNS record {hostname} already exists and does not point at tunnel {name}",
            why=(
                f"{hostname} is derived from --domain {domain}, so another machine with the same name (or an older install "
                "under another tunnel name) serves it; replacing it would move that machine's ChatGPT traffic here, so --yes "
                "does not replace it"
            ),
            fix=(
                f"name this machine differently: `computer chatgpt install --domain {domain} --machine-name <name>`; only when "
                f"you are SURE no other machine uses {hostname} (Cloudflare dashboard > DNS), re-run with {TAKEOVER_FLAG}"
            ),
        )
    if not domain:
        question = f"A DNS record for {hostname} already exists and does not point at tunnel {name}. Replace it?"
        if not confirmed(question, assume_yes or force_takeover):
            print(f"dns {hostname}: kept the existing record (re-run with --yes to replace it, or choose another --hostname)")
            return False
    cloudflare.route_dns(name, hostname, tunnel_id=tunnel_id, overwrite=True)
    if domain:
        print(f"WARNING: {TAKEOVER_FLAG}: dns {hostname}: the existing record was REPLACED, now routed to tunnel {name}")
    else:
        print(f"dns {hostname}: replaced, now routed to tunnel {name}")
    return True


def record_kept_error(settings: Settings) -> ConfigError:
    """The user declined to replace the hostname's DNS record: the tunnel is unreachable, so login and install fail."""
    return ConfigError(
        what=f"{settings.hostname} still points somewhere else",
        why="its existing DNS record was kept, so ChatGPT would not reach this PC",
        fix=(
            "re-run the same command with --yes to replace the record (only when it is not another machine's), "
            f"or choose another {'--machine-name' if settings.domain else '--hostname'}"
        ),
    )


def setup_tunnel(
    settings: Settings,
    layout: Layout,
    *,
    assume_yes: bool,
    skip_dns: bool,
    adopt: bool = False,
    force_takeover: bool = False,
    cert: AccountCert | None = None,
) -> bool:
    """Login, tunnel and DNS for `settings.hostname` / `settings.tunnel_name`; True when the hostname routes to the tunnel (or DNS
    is the user's business: `skip_dns`). `adopt`: the tunnel name was given on this run, so an existing tunnel without local
    credentials is taken over (one with live connections only with `force_takeover`, which also replaces the record of a
    --domain hostname). `cert`: an account certificate (already read and checked) to reuse instead of the browser login."""
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
    tunnel_id = _ensure_tunnel(cloudflare, name, hostname, adopt=adopt, force_takeover=force_takeover, domain=settings.domain)
    if skip_dns:
        print(
            f"dns: skipped; route it yourself: point {hostname} at tunnel {name} by its id, not its name: first set "
            "COMPUTER_TUNNEL_ID to that id in your shell (Cloudflare dashboard, Zero Trust > Networks > Tunnels), then run: "
            f"{cloudflare.manual_route_command(hostname)}"
        )
        return True
    return _route(
        cloudflare,
        name,
        hostname,
        tunnel_id=tunnel_id,
        assume_yes=assume_yes,
        force_takeover=force_takeover,
        domain=settings.domain,
    )


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
    routed = setup_tunnel(
        settings,
        ctx.layout,
        assume_yes=args.yes,
        skip_dns=args.skip_dns,
        adopt=explicit,
        force_takeover=args.force_takeover,
        cert=cert,
    )
    # The tunnel exists even when its DNS record was kept, so its name is saved before the failure is reported.
    if ctx.settings.tunnel_name != name:
        save_settings(settings)
        print(f"config: tunnel_name = {name}; run `computer chatgpt init` to write the cloudflared unit")
    if not routed:
        raise record_kept_error(settings)
    return 0


COMMAND = Command(
    name="login",
    help=(
        "building block of install: Cloudflare login, create the tunnel, route the hostname (tunnel mode: store the runtime key)"
    ),
    run=run,
    configure=configure,
)

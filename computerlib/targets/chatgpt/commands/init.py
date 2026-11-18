"""`computer chatgpt init`: choose the mode, write the config, install the pinned pieces, create the password, write the unit
files.

Idempotent: re-running changes only what differs (config values, a missing download, a changed unit). It never starts anything and
never changes the default mode `public` by itself: the comparison table is always shown, and `--mode` or a saved config choice is
the only way to another mode (a saved choice wins over the default, a flag wins over the saved choice).
Desktop Commander comes from the shared core (`computer mcp install`); this command runs that install, never a second one.
Tunnel mode installs tunnel-client (a pinned zip, one member), writes ONE unit, and creates no password: the runtime key is stored
by `login`. `computer chatgpt install` runs the same steps (`prepare`, then `install_units` once its tunnel is settled) as part of
its one-command setup.
"""

from __future__ import annotations

import argparse
import os
import platform
from dataclasses import replace

from agenttools_errors import UsageError

from computerlib import desktop_commander
from computerlib.commandtree import Command
from computerlib.pins import binary_pin, pinned_machines, zip_member_pin
from computerlib.targets.chatgpt import compare
from computerlib.targets.chatgpt.cloudflare import Cloudflare
from computerlib.targets.chatgpt.install import ensure_binary, ensure_zip_member
from computerlib.targets.chatgpt.machine import identify
from computerlib.targets.chatgpt.password import ensure_password
from computerlib.targets.chatgpt.processes import TUNNEL_SERVICE, processes
from computerlib.targets.chatgpt.settings import MODES, Layout, Settings, load_settings, save_settings, validate
from computerlib.targets.chatgpt.stack import SERVICES, unit_launcher
from computerlib.targets.chatgpt.systemd import Systemd
from computerlib.targets.chatgpt.tunnel import has_key, require_tunnel_id
from computerlib.targets.chatgpt.units import render_unit, unit_name


def _absolute(path: str) -> str:
    """A directory flag is saved absolute: a later command (or a unit) runs from another working directory."""
    return os.path.abspath(os.path.expanduser(path))


def add_public_options(parser: argparse.ArgumentParser) -> None:
    """Options `init` and `install` share: how this machine is named, where the public stack listens, what it is called, where
    cloudflared keeps its state."""
    parser.add_argument(
        "--domain",
        help=(
            "your domain (zone) on Cloudflare; the hostname becomes <machine>-mcp.<domain>, one label below the zone so the free "
            "certificate covers it (saved; --hostname instead sets the full name)"
        ),
    )
    parser.add_argument(
        "--machine-name",
        help=(
            "name of this machine: hostname label, tunnel name, ChatGPT connector name "
            "(default: the saved one, else the system hostname as a DNS label)"
        ),
    )
    parser.add_argument("--proxy-port", type=int, help="loopback port of mcp-auth-proxy (default 8766)")
    parser.add_argument("--shim-port", type=int, help="loopback port of the shim, where the tunnel points (default 8767)")
    parser.add_argument("--metrics-port", type=int, help="loopback port of cloudflared's readiness endpoint (default 20241)")
    parser.add_argument("--unit-prefix", help="name prefix of the systemd user units (default computer-chatgpt)")
    parser.add_argument(
        "--cloudflared-dir",
        type=_absolute,
        help=(
            "cloudflared state of this target: cert.pem, tunnel credentials, config.yml "
            "(default ~/.config/computer/chatgpt/cloudflared)"
        ),
    )


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--mode", choices=MODES, help="connection mode (default: the saved choice, else public)")
    parser.add_argument(
        "--hostname", help="public hostname in full, e.g. dc.example.com (public mode; --domain derives one instead)"
    )
    parser.add_argument(
        "--scheme", choices=("https", "http"), help="http only for local development on a scratch port (default https)"
    )
    parser.add_argument(
        "--tunnel-name",
        help="Cloudflare named tunnel that serves the hostname (default: the saved one; with --domain, the machine name)",
    )
    parser.add_argument("--tunnel-id", help="OpenAI tunnel id to serve, tunnel_... (tunnel mode)")
    parser.add_argument(
        "--tunnel-health-port", type=int, help="loopback port of tunnel-client's health endpoint (tunnel mode, default 8794)"
    )
    add_public_options(parser)
    parser.add_argument("--lang", choices=compare.LANGUAGES, help="language of the comparison table (default: from LANG)")
    parser.add_argument("--no-install", action="store_true", help="do not download or install anything")
    parser.add_argument("--no-units", action="store_true", help="do not write systemd unit files")


# Settings a flag of the same name can set; a flag that is absent, or that the running command does not have, changes nothing.
# hostname, domain, machine_name and tunnel_name depend on each other: machine.identify resolves them.
FLAG_SETTINGS = (
    "mode",
    "scheme",
    "proxy_port",
    "shim_port",
    "metrics_port",
    "unit_prefix",
    "cloudflared_dir",
    "tunnel_id",
    "tunnel_health_port",
)


def apply_flags(saved: Settings, args: argparse.Namespace, *, cloudflare: bool = False) -> Settings:
    """`saved` with every flag the user passed on top. `cloudflare`: the run sets up a Cloudflare tunnel (install), so the
    tunnel name defaults to the machine name."""
    passed = {name: getattr(args, name, None) for name in FLAG_SETTINGS}
    flagged = replace(saved, **{name: value for name, value in passed.items() if value is not None})
    return validate(
        identify(
            flagged,
            hostname=getattr(args, "hostname", None),
            domain=getattr(args, "domain", None),
            machine_name=getattr(args, "machine_name", None),
            tunnel_name=getattr(args, "tunnel_name", None),
            cloudflare=cloudflare,
        )
    )


def _pinned(name: str) -> UsageError:
    on = ", ".join(pinned_machines(name)) or "no CPU"
    return UsageError(
        what=f"no pinned {name} for this CPU ({platform.machine()})",
        why=f"the pin table has {name} for Linux on {on} only: its project publishes no build pinned for this CPU",
        fix=(
            f"run it on a Linux machine with one of these CPUs: {on} (a 32-bit board running a 64-bit capable CPU needs a "
            "64-bit OS image)"
        ),
    )


def _needed(settings: Settings) -> list[str]:
    if settings.mode == "tunnel":
        return ["tunnel-client"]  # a zip member pin
    return ["mcp-auth-proxy", *(["cloudflared"] if settings.tunnel_name else [])]  # binary pins


def require_pins(settings: Settings) -> None:
    """Refuse before anything is written when this CPU has no pinned build of a piece the mode needs."""
    lookup = zip_member_pin if settings.mode == "tunnel" else binary_pin
    for name in _needed(settings):
        if lookup(name) is None:
            raise _pinned(name)


def _install_tunnel_client(layout: Layout) -> list[str]:
    zip_pin = zip_member_pin("tunnel-client")
    if zip_pin is None:
        raise _pinned("tunnel-client")
    installed = ensure_zip_member(zip_pin, layout.bin_dir)
    return [f"tunnel-client {zip_pin.version}: {'installed, sha256 verified' if installed.changed else 'already installed'}"]


def _install_public_binaries(settings: Settings, layout: Layout) -> list[str]:
    done: list[str] = []
    for name in _needed(settings):
        pin = binary_pin(name)
        if pin is None:
            raise _pinned(name)
        installed = ensure_binary(pin, layout.bin_dir)
        done.append(
            f"{name} {pin.version}: {'installed, sha256 verified' if installed.changed else 'already installed, sha256 matches'}"
        )
    return done


def install_pieces(settings: Settings, layout: Layout) -> list[str]:
    done = _install_tunnel_client(layout) if settings.mode == "tunnel" else _install_public_binaries(settings, layout)
    done.append(desktop_commander.install())
    return done


def _refuse_other_mode_units(settings: Settings, layout: Layout) -> None:
    """Switching modes with the other mode's units still installed would leave two stacks fighting over one ChatGPT connector."""
    foreign = SERVICES if settings.mode == "tunnel" else (TUNNEL_SERVICE,)
    present = [
        name for name in (unit_name(settings.unit_prefix, service) for service in foreign) if (layout.unit_dir / name).exists()
    ]
    if present:
        raise UsageError(
            what=f"units of the other mode are installed: {', '.join(present)}",
            why=f"`--mode {settings.mode}` would run next to them; the mode never switches by itself",
            fix="run `computer chatgpt uninstall` (it removes the units, keeps your data) and then init again",
        )


def _require_mode_inputs(settings: Settings) -> None:
    if settings.mode == "tunnel":
        require_tunnel_id(settings.tunnel_id)
    elif not settings.hostname:
        raise UsageError(
            what="no public hostname",
            why="public mode serves ChatGPT through a hostname you own",
            fix="run `computer chatgpt install --domain <your domain on Cloudflare>` (or --hostname <full name>)",
        )


def _prepare_secret(settings: Settings, layout: Layout) -> None:
    if settings.mode == "tunnel":
        state = "present" if has_key(layout.tunnel_key_file) else "not stored yet (run `computer chatgpt login`)"
        print(f"runtime key: {state} in {layout.tunnel_key_file} (mode 600, never printed)")
    else:
        created = ensure_password(layout.password_file)
        print(f"password: {'created' if created else 'kept'} in {layout.password_file} (mode 600, never printed)")


def write_units(settings: Settings, layout: Layout) -> list[str]:
    units = {
        unit_name(settings.unit_prefix, p.name): render_unit(p, settings.unit_prefix)
        for p in processes(settings, layout, unit_launcher())
    }
    changed = Systemd(layout.unit_dir).install(units)
    print(f"units: {', '.join(units)} ({'written' if changed else 'unchanged'}) in {layout.unit_dir}")
    return changed


def install_units(settings: Settings, layout: Layout) -> list[str]:
    """Write the unit files; returns the ones that changed (a running unit keeps its old command line until restarted)."""
    if settings.mode == "public" and settings.tunnel_name:
        # The tunnel unit names this directory's config.yml; it must exist even before (or without) `login`.
        Cloudflare(layout.bin_dir / "cloudflared", layout.cloudflared_dir).prepare()
    return write_units(settings, layout)


def prepare(settings: Settings, *, install: bool = True, units: bool = True, save: bool = True) -> Layout:
    """Everything `init` does after the flags are applied except the unit files: config, secret, pinned installs. Starts nothing.
    The caller has run `require_pins` when `install`. `units`: the caller will write the unit files (`install_units`), so the
    other mode's units are refused before anything is written. `save`: write config.toml now; install saves it only once the
    tunnel step succeeded, so a refused run leaves the config describing the running stack."""
    _require_mode_inputs(settings)
    layout = Layout.of(settings)
    if units:
        _refuse_other_mode_units(settings, layout)
    if save:
        print(f"config: {save_settings(settings)}")
    _prepare_secret(settings, layout)
    if install:
        for line in install_pieces(settings, layout):
            print(line)
    return layout


def run(args: argparse.Namespace) -> int:
    settings = apply_flags(load_settings(), args)
    lang = args.lang or compare.language_from_environment(os.environ.get("LANG", ""))
    print(compare.render(lang))
    if not args.no_install:
        require_pins(settings)
    layout = prepare(settings, install=not args.no_install, units=not args.no_units)
    if not args.no_units:
        install_units(settings, layout)
    if settings.mode == "tunnel":
        print("next: computer chatgpt login (stores the runtime key), computer chatgpt start, then computer chatgpt connect")
    elif settings.tunnel_name:
        print("next: computer chatgpt login (Cloudflare tunnel + DNS), computer chatgpt start, then computer chatgpt connect")
    else:
        print("next: computer chatgpt start, then computer chatgpt connect (or `computer chatgpt install` for all of it)")
    return 0


COMMAND = Command(
    name="init",
    help="building block of install: choose the mode, install the pinned pieces, create the password and unit files",
    run=run,
    configure=configure,
)

"""Machine identity of the public mode: the name this machine goes by on the Cloudflare account and in ChatGPT, and the public
hostname derived from it, so one command per machine gives every machine its own tunnel and URL.

WHAT: `default_machine_name()` is the short system hostname turned into a DNS label; `public_hostname(machine, domain)` is
`<machine>-mcp.<domain>`; `identify()` resolves hostname, domain, machine name and tunnel name from the saved settings and the
flags of one run.
WHY ONE LABEL BELOW THE ZONE: Cloudflare's free Universal SSL certificate covers the zone apex and first-level subdomains only
(`*.example.com`); `<machine>.mcp.example.com` would need a paid Advanced Certificate, so the machine goes into the first label.
INVARIANTS: a saved `domain` means "the hostname is derived" (re-derived on every run, so `--machine-name` moves it); an explicit
`--hostname` clears it. The tunnel follows the machine name only when nothing is saved yet, or when a changed `--machine-name`
moves a derived hostname: a re-run never renames a tunnel by itself, and a fixed hostname keeps its tunnel.
"""

from __future__ import annotations

import re
import socket
from dataclasses import replace

from agenttools_errors import UsageError

from computerlib.targets.chatgpt.settings import Settings

HOSTNAME = re.compile(r"^(?=.{4,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z][a-z0-9-]{0,61}[a-z0-9]$")
HOSTNAME_SUFFIX = "-mcp"
LABEL_MAX = 63
MACHINE_MAX = LABEL_MAX - len(HOSTNAME_SUFFIX)  # `<machine>-mcp` is one DNS label, at most 63 characters
_NOT_LABEL = re.compile(r"[^a-z0-9]+")
# The forms of Go's uuid.Parse a tunnel name can take; the pinned cloudflared refuses such a name (cfapi/tunnel.go).
_UUID = re.compile(r"^(?:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}|[0-9a-f]{32})$", re.IGNORECASE)


def system_hostname() -> str:
    return socket.gethostname()


def is_uuid(name: str) -> bool:
    return bool(_UUID.match(name))


def sanitize_label(raw: str) -> str:
    """`raw` as a DNS label that leaves room for the `-mcp` suffix: lowercase, runs of anything but [a-z0-9] become one '-', no
    leading or trailing '-', at most MACHINE_MAX characters. May be empty."""
    label = _NOT_LABEL.sub("-", raw.lower()).strip("-")
    return label[:MACHINE_MAX].rstrip("-")


def _system_label() -> str:
    """The short system hostname (`box.lan` -> `box`) as a label; may be empty."""
    return sanitize_label(system_hostname().split(".", 1)[0])


def default_machine_name(flag: str = "--machine-name") -> str:
    """The short system hostname as a label, or a usage error. `flag`: the option of the running command that names the
    machine (or what follows from it) instead, for the fix of the error."""
    name = _system_label()
    if not name:
        raise UsageError(
            what=f"the system hostname {system_hostname()!r} gives no usable machine name",
            why="the machine name must contain letters or digits",
            fix=f"pass {flag} <name> (letters, digits and '-')",
        )
    return name


def require_machine_name(name: str) -> str:
    if not name or sanitize_label(name) != name:
        suggestion = sanitize_label(name)
        raise UsageError(
            what=f"{name!r} cannot name a machine",
            why=f"it becomes a DNS label and a tunnel name: lowercase letters, digits and '-', at most {MACHINE_MAX} characters",
            fix=f"pass --machine-name {suggestion}" if suggestion else "pass --machine-name with letters or digits",
        )
    return name


def _dns_name(value: str, *, what: str, why: str, fix: str) -> str:
    name = value.strip().lower().rstrip(".")
    if not HOSTNAME.match(name):
        raise UsageError(what=what, why=why, fix=fix)
    return name


def require_hostname(hostname: str) -> str:
    """The hostname as a lowercase DNS name a Cloudflare zone can hold, or a usage error."""
    return _dns_name(
        hostname,
        what=f"{hostname!r} is not a public hostname",
        why="it must be a DNS name inside a domain (zone) on your Cloudflare account, e.g. computer.example.com",
        fix="pass --hostname <name>.<your domain on Cloudflare>",
    )


def require_domain(domain: str) -> str:
    return _dns_name(
        domain,
        what=f"{domain!r} is not a domain",
        why="--domain is a zone on your Cloudflare account, e.g. example.com",
        fix="pass --domain <your domain on Cloudflare>",
    )


def public_hostname(machine: str, domain: str) -> str:
    hostname = f"{machine}{HOSTNAME_SUFFIX}.{domain}"
    return _dns_name(
        hostname,
        what=f"{hostname!r} is not a public hostname",
        why="the machine name and the domain together exceed the 253 characters of a DNS name",
        fix="pass a shorter --machine-name (or --hostname <full name>)",
    )


def identify(
    settings: Settings,
    *,
    hostname: str | None,
    domain: str | None,
    machine_name: str | None,
    tunnel_name: str | None,
    cloudflare: bool,
) -> Settings:
    """`settings` with hostname, domain, machine name and tunnel name resolved for this run (flags are None when not passed).
    `cloudflare`: the run uses a Cloudflare tunnel, so a missing tunnel name defaults to the machine name."""
    if hostname is not None and domain is not None:
        raise UsageError(
            what="--hostname and --domain together",
            why="--domain derives the hostname (<machine>-mcp.<domain>), --hostname sets it",
            fix="pass one of them",
        )
    machine = require_machine_name(machine_name) if machine_name is not None else settings.machine_name or default_machine_name()
    resolved = _with_hostname(settings, machine, hostname=hostname, domain=domain)
    tunnel = _tunnel_for(settings, resolved, machine, machine_name=machine_name, tunnel_name=tunnel_name, cloudflare=cloudflare)
    return replace(resolved, machine_name=machine, tunnel_name=tunnel)


def _with_hostname(settings: Settings, machine: str, *, hostname: str | None, domain: str | None) -> Settings:
    if hostname is not None:
        # Taken as given: `init --scheme http --hostname 127.0.0.1:<port>` is a development setup; install validates it.
        return replace(settings, hostname=hostname, domain="")
    if domain is not None or settings.domain:
        zone = require_domain(domain if domain is not None else settings.domain)
        return replace(settings, hostname=public_hostname(machine, zone), domain=zone)
    return settings


def _tunnel_for(
    settings: Settings,
    resolved: Settings,
    machine: str,
    *,
    machine_name: str | None,
    tunnel_name: str | None,
    cloudflare: bool,
) -> str:
    if tunnel_name is not None:
        return tunnel_name
    # A 0.2.x config saved no machine name: it went by the system hostname, so passing that name spelled out is no rename.
    renamed = machine_name is not None and machine_name != (settings.machine_name or _system_label())
    if renamed and resolved.domain:
        # A new machine name moves the derived hostname, so the tunnel moves with it (the fix for a name already taken on the
        # account). Never with a fixed --hostname: that would re-point its DNS record and orphan the tunnel serving it.
        tunnel = machine
    else:
        uses_tunnel = cloudflare or bool(resolved.domain) or bool(settings.tunnel_name)
        tunnel = settings.tunnel_name or (machine if uses_tunnel else "")
    if tunnel == machine and is_uuid(machine):
        # Only a tunnel named after the machine is affected: a saved or explicit tunnel name, or no tunnel, keeps working.
        raise UsageError(
            what=f"the machine name {machine!r} cannot name the Cloudflare tunnel",
            why="the tunnel is named after the machine, and cloudflared refuses tunnel names that read as a UUID",
            fix="pass --machine-name <another name> (or --tunnel-name)",
        )
    return tunnel

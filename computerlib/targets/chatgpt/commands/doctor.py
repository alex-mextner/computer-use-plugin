"""`computer chatgpt doctor`: diagnose the whole chain and say how to fix what is wrong. It never restarts or changes anything,
except with `--heal`, which instead runs one bounded watchdog pass (see heal.py) and restarts only a unit that is active but hung.

Tunnel mode checks instead: the tunnel id, the pinned tunnel-client, Desktop Commander and node, the runtime key file (present,
mode 600), unit files, the unit, lingering, and tunnel-client's own health port (ready, control plane polled); there is nothing
public to probe.

Checks: configuration, pinned installs (digest and exec bit of every binary), password file mode, unit files up to date, units
active and lingering, local probes (shim, proxy), the 401 challenge through the shim, the cloudflared readiness endpoint, and
(unless --local) the same challenge through the public URL. Exit 0 when all pass, 3 otherwise. `--json` prints the list.
Both modes also check that write_pdf's Chrome can start its sandbox under AppArmor (`pdf_sandbox`); that one only warns.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shlex
import stat
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

from agenttools_errors import AgentToolError, UsageError

from computerlib import desktop_commander, pdf_sandbox
from computerlib.commandtree import Command
from computerlib.pins import DESKTOP_COMMANDER_VERSION, binary_pin, zip_member_pin
from computerlib.targets.chatgpt.context import Context, load_context
from computerlib.targets.chatgpt.heal import heal
from computerlib.targets.chatgpt.install import sha256_file
from computerlib.targets.chatgpt.lifecycle import tunnel_serving, unit_names
from computerlib.targets.chatgpt.password import read_password
from computerlib.targets.chatgpt.probe import challenge_ok, local_alive, request
from computerlib.targets.chatgpt.processes import processes
from computerlib.targets.chatgpt.stack import external_url, unit_launcher
from computerlib.targets.chatgpt.tunnel import has_key, valid_tunnel_id
from computerlib.targets.chatgpt.tunnel_health import read_state
from computerlib.targets.chatgpt.units import render_unit, unit_name
from computerlib.targets.chatgpt.watchdog import DEFAULT_POLICY


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str
    fix: str = ""
    # A failed check that is not required is printed as a warning and leaves the exit code alone: the chain still serves.
    required: bool = True


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--local", action="store_true", help="skip the checks through the public URL (public mode)")
    parser.add_argument("--json", action="store_true", help="print the checks as JSON")
    policy = DEFAULT_POLICY
    parser.add_argument(
        "--heal",
        action="store_true",
        help=(
            "instead of the checks, run one watchdog pass (public mode): restart a unit that is active but fails its local "
            f"probe {policy.threshold} passes in a row, at most {policy.budget} times an hour and {policy.spacing / 60:g} "
            "minutes apart, never while the internet is down; meant for a user timer (README)"
        ),
    )


def _not_executable(name: str, path: Path) -> Check:
    # The digest survives a backup restore that drops the exec bit, but systemd then cannot start the unit.
    return Check(name, False, f"{path} is not executable", f"run `computer chatgpt init` (or chmod 755 {shlex.quote(str(path))})")


def _binary(ctx: Context, name: str) -> Check:
    pin = binary_pin(name)
    path = ctx.layout.bin_dir / name
    if pin is None:
        return Check(f"{name} installed", False, "no pinned build for this CPU", "install it by hand")
    if not path.is_file():
        return Check(f"{name} installed", False, f"missing: {path}", "run `computer chatgpt init`")
    if sha256_file(path) != pin.sha256:
        return Check(
            f"{name} installed", False, f"{path} does not match the pinned {pin.version} digest", "run `computer chatgpt init`"
        )
    if not os.access(path, os.X_OK):
        return _not_executable(f"{name} installed", path)
    return Check(f"{name} installed", True, f"{pin.version}, digest matches the pin")


def _tunnel_client(ctx: Context) -> Check:
    pin = zip_member_pin("tunnel-client")
    path = ctx.layout.tunnel_client_binary
    if pin is None:
        return Check("tunnel-client installed", False, "no pinned build for this CPU", "install it by hand")
    if not path.is_file():
        return Check("tunnel-client installed", False, f"missing: {path}", "run `computer chatgpt init`")
    if sha256_file(path) != pin.member_sha256:
        return Check(
            "tunnel-client installed",
            False,
            f"{path} does not match the pinned {pin.version} digest",
            "run `computer chatgpt init`",
        )
    if not os.access(path, os.X_OK):
        return _not_executable("tunnel-client installed", path)
    return Check("tunnel-client installed", True, f"{pin.version}, digest matches the pin")


def _tunnel_id(ctx: Context) -> Check:
    ok = valid_tunnel_id(ctx.settings.tunnel_id)
    fix = "run `computer chatgpt init --mode tunnel --tunnel-id <tunnel_...>`"
    return Check("tunnel id configured", ok, "set" if ok else "missing or malformed", "" if ok else fix)


def _runtime_key(ctx: Context) -> Check:
    path = ctx.layout.tunnel_key_file
    if not has_key(path):
        return Check("tunnel runtime key", False, f"nothing stored in {path}", "run `computer chatgpt login --key-stdin`")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return Check(
            "tunnel runtime key", False, f"{path} is mode {mode:o}, readable by others", f"chmod 600 {shlex.quote(str(path))}"
        )
    return Check("tunnel runtime key", True, f"present in {path}, mode 600")


def _daemon(ctx: Context) -> Check:
    health = read_state(ctx.settings.tunnel_health_port)
    fixes = {
        "unavailable": (
            f"run `computer chatgpt start`, then journalctl --user -u {unit_name(ctx.settings.unit_prefix, 'tunnel-client')}"
        ),
        "not_ready": "Desktop Commander or the daemon is still starting, or failed: see the unit's journal",
        "awaiting_poll": (
            "the key needs Tunnels Read+Use and must belong to the project of the tunnel; check the tunnel id and the network"
        ),
    }
    return Check("tunnel-client healthy", tunnel_serving(health), f"{health.state}: {health.detail}", fixes.get(health.state, ""))


def _desktop_commander() -> Check:
    name = "desktop-commander installed"
    try:
        spec = desktop_commander.launch_spec()
    except AgentToolError as error:
        return Check(name, False, f"{error.what}: {error.why}", error.fix)
    return Check(name, True, f"{DESKTOP_COMMANDER_VERSION} with node {spec.node}")


def _pdf_sandbox() -> Check:
    name = "PDF browser sandbox (write_pdf)"
    found = pdf_sandbox.problem()
    if found is None:
        return Check(name, True, "no AppArmor user-namespace restriction stops it")
    return Check(name, False, found.detail, found.fix, required=False)


def _cloudflare_fix(ctx: Context, credentials: Path) -> str:
    if credentials.is_file():
        return "run `computer chatgpt install` (it logs in to Cloudflare)"
    # A tunnel without local credentials is refused unless named: this machine's own tunnel is adopted only by name.
    return f"run `computer chatgpt install --tunnel-name {ctx.settings.tunnel_name}` (fetches this machine's tunnel credentials)"


def _cloudflare_files(ctx: Context) -> Check:
    credentials = ctx.layout.tunnel_credentials(ctx.settings.tunnel_name)
    missing = [str(path) for path in (ctx.layout.origin_cert, credentials) if not path.is_file()]
    return Check(
        "cloudflare login and tunnel credentials",
        not missing,
        f"in {ctx.layout.cloudflared_dir}" if not missing else f"missing: {', '.join(missing)}",
        "" if not missing else _cloudflare_fix(ctx, credentials),
    )


def _password(ctx: Context) -> Check:
    path = ctx.layout.password_file
    if not read_password(path):
        return Check("login password", False, f"no PASSWORD in {path}", "run `computer chatgpt init`")
    mode = stat.S_IMODE(path.stat().st_mode)
    if mode & 0o077:
        return Check(
            "login password", False, f"{path} is mode {mode:o}, readable by others", f"chmod 600 {shlex.quote(str(path))}"
        )
    return Check("login password", True, f"present in {path}, mode 600")


def _unit_files(ctx: Context) -> Check:
    stale: list[str] = []
    for process in processes(ctx.settings, ctx.layout, unit_launcher()):
        path = ctx.layout.unit_dir / unit_name(ctx.settings.unit_prefix, process.name)
        if not path.exists() or path.read_text() != render_unit(process, ctx.settings.unit_prefix):
            stale.append(path.name)
    return Check(
        "unit files current",
        not stale,
        "match the configuration" if not stale else f"missing or stale: {', '.join(stale)}",
        "" if not stale else "run `computer chatgpt init`",
    )


def _units(ctx: Context) -> list[Check]:
    checks: list[Check] = []
    for name in unit_names(ctx):
        state = ctx.systemd.state(name)
        ok = state.active == "active"
        checks.append(
            Check(
                f"{name} active",
                ok,
                f"{state.active}/{state.sub}, {state.restarts} restart(s)",
                "" if ok else "run `computer chatgpt start`",
            )
        )
    return checks


def _linger(ctx: Context) -> Check:
    on = ctx.systemd.lingering(getpass.getuser())
    return Check(
        "lingering (starts at boot)",
        on,
        "enabled" if on else "off: the stack stops when you log out",
        "" if on else "run `computer chatgpt start`",
    )


def _local(ctx: Context) -> list[Check]:
    # The proxy has no unit of its own: it runs inside the bridge unit, so its journal is the bridge's.
    checks = [
        Check(
            f"{label} answers",
            local_alive(port),
            f"127.0.0.1:{port}",
            f"journalctl --user -u {unit_name(ctx.settings.unit_prefix, process)}",
        )
        for label, process, port in (
            ("proxy", "bridge", ctx.settings.proxy_port),
            ("shim", "shim", ctx.settings.shim_port),
        )
    ]
    ok, detail = challenge_ok(f"http://127.0.0.1:{ctx.settings.shim_port}")
    checks.append(
        Check(
            "401 challenge through the shim", ok, detail, "the shim or the proxy is not serving: run `computer chatgpt restart`"
        )
    )
    return checks


def _tunnel(ctx: Context) -> list[Check]:
    if not ctx.settings.tunnel_name:
        return []
    answer = request(f"http://127.0.0.1:{ctx.settings.metrics_port}/ready")
    ok = answer is not None and answer.status == 200
    return [
        Check(
            "cloudflared has an edge connection",
            ok,
            "ready" if ok else "not ready",
            "check the Cloudflare login and tunnel credentials; a public outage is not fixed by restarting",
        )
    ]


def _edge(ctx: Context) -> list[Check]:
    ok, detail = challenge_ok(external_url(ctx.settings))
    return [
        Check(
            "401 challenge through the public URL",
            ok,
            f"{external_url(ctx.settings)}: {detail}",
            "check DNS, the tunnel and `computer chatgpt status`",
        )
    ]


def collect_tunnel(ctx: Context) -> list[Check]:
    return [
        _tunnel_id(ctx),
        _tunnel_client(ctx),
        _desktop_commander(),
        _pdf_sandbox(),
        _runtime_key(ctx),
        _unit_files(ctx),
        *_units(ctx),
        _linger(ctx),
        _daemon(ctx),
    ]


def collect(ctx: Context, local_only: bool) -> list[Check]:
    if ctx.settings.mode == "tunnel":
        return collect_tunnel(ctx)
    builders: list[Callable[[], list[Check]]] = [
        lambda: [_binary(ctx, "mcp-auth-proxy")],
        lambda: [_binary(ctx, "cloudflared"), _cloudflare_files(ctx)] if ctx.settings.tunnel_name else [],
        lambda: [_desktop_commander(), _pdf_sandbox(), _password(ctx), _unit_files(ctx)],
        lambda: [*_units(ctx), _linger(ctx)],
        lambda: _local(ctx),
        lambda: _tunnel(ctx),
        lambda: [] if local_only else _edge(ctx),
    ]
    return [check for build in builders for check in build()]


def _print(check: Check) -> None:
    label = "ok" if check.ok else "FAIL" if check.required else "warn"
    print(f"[{label}] {check.name}: {check.detail}")
    if not check.ok and check.fix:
        print(f"       fix: {check.fix}")


def _heal(ctx: Context, args: argparse.Namespace) -> int:
    if args.json or args.local:
        raise UsageError(
            what="--heal takes no --json or --local",
            why="the watchdog pass prints one line per decision and never probes the public URL",
            fix="run `computer chatgpt doctor --heal` alone",
        )
    return heal(ctx)


def run(args: argparse.Namespace) -> int:
    ctx = load_context()
    if args.heal:
        return _heal(ctx, args)
    checks = collect(ctx, args.local)
    if args.json:
        print(json.dumps([asdict(c) for c in checks], indent=2))
    else:
        for check in checks:
            _print(check)
    return 0 if all(c.ok or not c.required for c in checks) else 3


COMMAND = Command(
    name="doctor",
    help="diagnose the chain end to end and say how to fix it (never restarts; --heal restarts a hung unit)",
    run=run,
    configure=configure,
)

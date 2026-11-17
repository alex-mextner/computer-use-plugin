"""The processes of the public-mode stack and how each one is started.

WHAT: three processes, each described once here and used both by the systemd units (`units.py`) and by `computer chatgpt run`
(foreground, no systemd, for development and tests):
  bridge  mcp-auth-proxy (OAuth 2.1 login) whose stdio backend is `computer mcp serve` (the shared, pinned Desktop Commander),
          stdout filtered by `quiet`
  shim    the rules of shim/ in front of the bridge (challenge header, path rewrite, access log)
  tunnel  cloudflared named tunnel to the shim (only when a tunnel name is configured), with its own config/credentials dir
The public hostname reaches cloudflared -> shim -> bridge -> Desktop Commander; nothing but cloudflared talks to the internet.
INVARIANTS: no secret is ever in an argv (the password reaches the bridge as PASSWORD= from a 0600 env file); every process
listens on 127.0.0.1 only; Desktop Commander is never installed or launched by this target itself (no second install, no
`npx @latest`).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path

from computerlib.executable import computer_executable
from computerlib.paths import explicit_data_home
from computerlib.targets.chatgpt.settings import Layout, Settings

SERVICES = ("bridge", "shim", "tunnel")


@dataclass(frozen=True)
class Process:
    name: str
    description: str
    argv: tuple[str, ...]
    env: tuple[tuple[str, str], ...] = ()
    env_files: tuple[Path, ...] = ()
    after: str | None = None  # the service of this stack it is ordered after (and wants)
    restart_seconds: int = 3


def external_url(settings: Settings) -> str:
    return f"{settings.scheme}://{settings.hostname}"


Launcher = tuple[str, ...]


def unit_launcher() -> Launcher:
    """How a unit calls back into this CLI (`shim`, `quiet`, `mcp serve`): the absolute path of the installed `computer`."""
    return (str(computer_executable()),)


def module_launcher() -> Launcher:
    """How `run` calls back into this CLI: the interpreter that runs it, so a source checkout needs no install."""
    return (sys.executable, "-m", "computerlib.cli")


def core_environment() -> tuple[tuple[str, str], ...]:
    """A custom XDG_DATA_HOME travels into the unit: the user manager does not have the shell's variables, and
    `computer mcp serve` finds the managed Desktop Commander under it."""
    data_home = explicit_data_home()
    return (("XDG_DATA_HOME", str(data_home)),) if data_home is not None else ()


def bridge(settings: Settings, layout: Layout, launcher: Launcher) -> Process:
    return Process(
        name="bridge",
        description="Desktop Commander MCP behind mcp-auth-proxy (OAuth 2.1 login for ChatGPT)",
        argv=(
            *launcher,
            "chatgpt",
            "quiet",
            "--",
            str(layout.bin_dir / "mcp-auth-proxy"),
            "--external-url",
            external_url(settings),
            "--no-auto-tls",
            "--listen",
            f"127.0.0.1:{settings.proxy_port}",
            "--data-path",
            str(layout.data_dir),
            "--",
            *launcher,
            "mcp",
            "serve",
        ),
        env=core_environment(),
        env_files=(layout.password_file,),
    )


def shim(settings: Settings, layout: Layout, launcher: Launcher) -> Process:
    return Process(
        name="shim",
        description="Shim in front of mcp-auth-proxy (WWW-Authenticate challenge, protected-resource path, access log)",
        argv=(
            *launcher,
            "chatgpt",
            "shim",
            "--external-url",
            external_url(settings),
            "--listen-port",
            str(settings.shim_port),
            "--upstream-port",
            str(settings.proxy_port),
            "--log-file",
            str(layout.access_log),
        ),
        after="bridge",
        restart_seconds=2,
    )


def tunnel(settings: Settings, layout: Layout) -> Process:
    return Process(
        name="tunnel",
        description="Cloudflare named tunnel to the shim",
        argv=(
            str(layout.bin_dir / "cloudflared"),
            "tunnel",
            "--no-autoupdate",
            "--config",
            str(layout.cloudflared_config),
            "--origincert",
            str(layout.origin_cert),
            "--metrics",
            f"127.0.0.1:{settings.metrics_port}",
            "run",
            "--credentials-file",
            str(layout.tunnel_credentials(settings.tunnel_name)),
            "--url",
            f"http://127.0.0.1:{settings.shim_port}",
            settings.tunnel_name,
        ),
        after="shim",
        restart_seconds=5,
    )


def stack(settings: Settings, layout: Layout, launcher: Launcher) -> list[Process]:
    """The processes of this configuration, in start order. The tunnel is part of it only when a tunnel name is configured."""
    processes = [bridge(settings, layout, launcher), shim(settings, layout, launcher)]
    if settings.tunnel_name:
        processes.append(tunnel(settings, layout))
    return processes

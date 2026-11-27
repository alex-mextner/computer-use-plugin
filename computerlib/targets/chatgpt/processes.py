"""The processes of the configured mode, in start order: the one place that knows which mode runs what.

WHAT: `processes()` returns the three public-mode processes (`stack.stack`) or the single tunnel-mode process
(`tunnel.tunnel_client`); `services()` names them without needing the launcher. Units, `run`, `doctor`, `start`, `status` and
`end` all go through here.
INVARIANTS: nothing here changes the mode; the mode comes from the settings only. Both modes run Desktop Commander through the
shared core (`computer mcp serve --terminal-only`).
"""

from __future__ import annotations

from computerlib.targets.chatgpt.settings import Layout, Settings
from computerlib.targets.chatgpt.stack import Launcher, Process, core_environment, stack
from computerlib.targets.chatgpt.tunnel import tunnel_client

TUNNEL_SERVICE = "tunnel-client"


def processes(settings: Settings, layout: Layout, launcher: Launcher) -> list[Process]:
    if settings.mode == "tunnel":
        return [
            tunnel_client(
                layout.tunnel_client_binary,
                settings.tunnel_id,
                layout.tunnel_key_file,
                (*launcher, "mcp", "serve", "--terminal-only"),
                settings.tunnel_health_port,
                core_environment(),
            )
        ]
    return stack(settings, layout, launcher)


def services(settings: Settings) -> list[str]:
    """The service names of this configuration in start order (public: the tunnel only when a Cloudflare tunnel is configured)."""
    if settings.mode == "tunnel":
        return [TUNNEL_SERVICE]
    return ["bridge", "shim", *(["tunnel"] if settings.tunnel_name else [])]

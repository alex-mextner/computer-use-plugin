"""Shared by start/stop/restart/end/status: which units belong to this stack and whether it is serving."""

from __future__ import annotations

import time
from collections.abc import Callable

from computerlib.targets.chatgpt import processes
from computerlib.targets.chatgpt.context import Context
from computerlib.targets.chatgpt.probe import local_alive
from computerlib.targets.chatgpt.tunnel_health import TunnelHealth, read_state
from computerlib.targets.chatgpt.units import unit_name

READY_TIMEOUT_SECONDS = 45.0
# tunnel-client answers on its health port before the control plane has accepted its key; these are the states past that point.
TUNNEL_SERVING = ("awaiting_discovery", "awaiting_tool_call", "connected")


def services(ctx: Context) -> list[str]:
    """The service names of this configuration in start order (public mode: the tunnel only when one is configured)."""
    return processes.services(ctx.settings)


def unit_names(ctx: Context) -> list[str]:
    return [unit_name(ctx.settings.unit_prefix, service) for service in services(ctx)]


def tunnel_serving(health: TunnelHealth) -> bool:
    return health.state in TUNNEL_SERVING


def wait_until_serving(
    ctx: Context,
    timeout: float | None = None,
    alive: Callable[[int], bool] = local_alive,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> bool:
    """Poll until the stack serves.

    Public mode: the shim answers (it proxies the bridge, which opens its port only once Desktop Commander speaks MCP).
    Tunnel mode: tunnel-client is ready (its stdio child is up) and its control-plane poll has succeeded.
    """
    deadline = clock() + (READY_TIMEOUT_SECONDS if timeout is None else timeout)
    while clock() < deadline:
        if ctx.settings.mode == "tunnel":
            if tunnel_serving(read_state(ctx.settings.tunnel_health_port)):
                return True
        elif alive(ctx.settings.shim_port) and alive(ctx.settings.proxy_port):
            return True
        sleep(0.5)
    return False


def not_serving_reason(ctx: Context) -> str:
    """Why `wait_until_serving` gave up, for the error message."""
    if ctx.settings.mode == "tunnel":
        health = read_state(ctx.settings.tunnel_health_port)
        return f"tunnel-client is {health.state}: {health.detail}"
    return f"shim :{ctx.settings.shim_port} or proxy :{ctx.settings.proxy_port} does not answer"

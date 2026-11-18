"""What a running OpenAI `tunnel-client` says about itself, as a small state machine.

WHAT: reads the daemon's loopback operator endpoints (/readyz, /health?details=true, /health/mcp, /metrics) and reduces them to
one state, in the order a connection has to get through:
  unavailable        nothing answers on the health port (daemon down)
  not_ready          /readyz is not 200 (startup probe or MCP child not up)
  awaiting_poll      ready, but the control plane has not been polled successfully yet (key without Tunnels Read+Use, wrong id,
                     no network)
  awaiting_discovery polling works, no initialize or tools/list has come through the tunnel since the watch started (the daemon's
                     own startup probe of its stdio child shows as "discovered" in /health/mcp, so that endpoint is NOT the
                     evidence)
  awaiting_tool_call a discovery request came through, no tools/call yet
  connected          a tools/call went through the tunnel since the watch started
The schemas were read from tunnel-client 0.0.17 running against a local stand-in control plane (a real daemon, a stub MCP child).
`connected` means a tool call arrived through YOUR tunnel; the daemon cannot say which OpenAI surface (ChatGPT, Codex, the
Responses API) sent it, so unlike the public mode no client label can be verified.
"""

from __future__ import annotations

import http.client
import json
import re
from dataclasses import dataclass, field

from computerlib.targets.chatgpt.shim.jsonish import as_object

STATES = ("unavailable", "not_ready", "awaiting_poll", "awaiting_discovery", "awaiting_tool_call", "connected")
COUNT_LINE = re.compile(
    r'^command_end_to_end_latency_milliseconds_count\{[^}]*request_method="([^"]+)"[^}]*\}\s+([0-9.eE+-]+)\s*$'
)


@dataclass(frozen=True)
class Counts:
    """Commands the daemon has answered, per MCP method, since it started (its own counters)."""

    initialize: int = 0
    tools_list: int = 0
    tools_call: int = 0


@dataclass(frozen=True)
class TunnelHealth:
    state: str
    detail: str
    counts: Counts = field(default_factory=Counts)


def _get(port: int, path: str, timeout: float = 3.0) -> tuple[int, bytes] | None:
    try:
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, response.read(2 * 1024 * 1024)
        finally:
            connection.close()
    except (OSError, http.client.HTTPException):
        return None


def _json(port: int, path: str) -> dict[str, object]:
    got = _get(port, path)
    if got is None or got[0] != 200:
        return {}
    try:
        return as_object(json.loads(got[1]))
    except ValueError:
        return {}


def parse_counts(metrics_text: str) -> Counts:
    """The per-method command counters of /metrics (Prometheus text): requests that went through the tunnel and were answered."""
    totals: dict[str, float] = {}
    for line in metrics_text.splitlines():
        match = COUNT_LINE.match(line)
        if match:
            totals[match.group(1)] = totals.get(match.group(1), 0.0) + float(match.group(2))
    return Counts(int(totals.get("initialize", 0)), int(totals.get("tools/list", 0)), int(totals.get("tools/call", 0)))


def read_counts(port: int) -> Counts:
    metrics = _get(port, "/metrics")
    return parse_counts(metrics[1].decode("utf-8", "replace")) if metrics and metrics[0] == 200 else Counts()


def _polling(health: dict[str, object]) -> bool:
    control = as_object(as_object(health.get("components")).get("control-plane"))
    details = as_object(control.get("details"))
    return control.get("state") == "polling" and bool(details.get("last_success")) and details.get("consecutive_failures", 0) == 0


def read_state(port: int, baseline: Counts | None = None) -> TunnelHealth:
    """The state of the daemon on `port`; `baseline` is the counters at the start of the watch (earlier traffic does not
    count)."""
    baseline = baseline or Counts()
    ready = _get(port, "/readyz")
    if ready is None:
        return TunnelHealth("unavailable", f"nothing answers on 127.0.0.1:{port}")
    if ready[0] != 200:
        return TunnelHealth("not_ready", f"/readyz answered {ready[0]}")
    if not _polling(_json(port, "/health?details=true")):
        return TunnelHealth(
            "awaiting_poll", "the control plane has not been polled successfully (check the key, the tunnel id and the network)"
        )
    counts = read_counts(port)
    if counts.tools_call > baseline.tools_call:
        return TunnelHealth("connected", "a tool call went through the tunnel", counts)
    if counts.initialize > baseline.initialize or counts.tools_list > baseline.tools_list:
        return TunnelHealth(
            "awaiting_tool_call", "ChatGPT discovered the server through the tunnel; waiting for a tool call", counts
        )
    return TunnelHealth("awaiting_discovery", "no discovery request has come through the tunnel yet", counts)

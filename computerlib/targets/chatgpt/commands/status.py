"""`computer chatgpt status`: units, local probes, and what reached this PC (last request, verified ChatGPT calls).

Exit 0 when every unit is active and the shim and the proxy answer, 3 otherwise. `--json` prints the same facts as one JSON
object.
Tunnel mode: the unit, the tunnel-client state read from its health port (unavailable ... connected) and the counters of requests
that came through the tunnel since the daemon started; healthy means active, ready and polling the control plane.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import asdict, dataclass

from computerlib.commandtree import Command
from computerlib.targets.chatgpt.accessview import Record, read_records
from computerlib.targets.chatgpt.context import Context, load_context
from computerlib.targets.chatgpt.lifecycle import tunnel_serving, unit_names
from computerlib.targets.chatgpt.probe import local_alive
from computerlib.targets.chatgpt.stack import external_url
from computerlib.targets.chatgpt.systemd import UnitState
from computerlib.targets.chatgpt.tunnel_health import read_state


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="print one JSON object instead of text")


def _chatgpt_calls(records: list[Record]) -> list[Record]:
    return [r for r in records if r.path == "/mcp" and r.verified_openai and "tools/call" in r.rpc and 200 <= r.status < 300]


@dataclass(frozen=True)
class StatusReport:
    mode: str
    url: str
    healthy: bool
    units: list[UnitState]
    local: dict[str, bool]
    last_request: str | None
    chatgpt_tool_calls: int
    last_chatgpt_tool_call: str | None

    def text(self) -> str:
        lines = [f"mode: {self.mode}   url: {self.url}   {'healthy' if self.healthy else 'UNHEALTHY'}"]
        lines += [f"  {u.name}: {u.active}/{u.sub} restarts={u.restarts} enabled={u.enabled}" for u in self.units]
        lines.append(
            "  local: " + ", ".join(f"{name} {'answers' if ok else 'DOES NOT ANSWER'}" for name, ok in self.local.items())
        )
        lines.append(f"  last request: {self.last_request or 'none yet'}")
        lines.append(f"  verified ChatGPT tool calls: {self.chatgpt_tool_calls} (last: {self.last_chatgpt_tool_call or 'never'})")
        return "\n".join(lines)


def gather(ctx: Context) -> StatusReport:
    units = [ctx.systemd.state(name) for name in unit_names(ctx)]
    local = {"shim": local_alive(ctx.settings.shim_port), "proxy": local_alive(ctx.settings.proxy_port)}
    records = read_records(ctx.layout.access_log)
    calls = _chatgpt_calls(records)
    return StatusReport(
        mode=ctx.settings.mode,
        url=f"{external_url(ctx.settings)}/mcp",
        healthy=all(u.active == "active" for u in units) and all(local.values()),
        units=units,
        local=local,
        last_request=records[-1].raw if records else None,
        chatgpt_tool_calls=len(calls),
        last_chatgpt_tool_call=calls[-1].time if calls else None,
    )


@dataclass(frozen=True)
class TunnelStatusReport:
    mode: str
    tunnel_id: str
    healthy: bool
    units: list[UnitState]
    state: str
    detail: str
    initialize: int
    tools_list: int
    tools_call: int

    def text(self) -> str:
        lines = [f"mode: {self.mode}   tunnel: {self.tunnel_id}   {'healthy' if self.healthy else 'UNHEALTHY'}"]
        lines += [f"  {u.name}: {u.active}/{u.sub} restarts={u.restarts} enabled={u.enabled}" for u in self.units]
        lines.append(f"  tunnel-client: {self.state} ({self.detail})")
        lines.append(
            f"  through the tunnel since the daemon started: initialize {self.initialize}, tools/list {self.tools_list}, "
            f"tools/call {self.tools_call}"
        )
        lines.append(
            "  the daemon cannot tell which OpenAI surface sent a request: "
            "a tool call proves the tunnel, not ChatGPT specifically"
        )
        return "\n".join(lines)


def gather_tunnel(ctx: Context) -> TunnelStatusReport:
    units = [ctx.systemd.state(name) for name in unit_names(ctx)]
    health = read_state(ctx.settings.tunnel_health_port)
    return TunnelStatusReport(
        mode=ctx.settings.mode,
        tunnel_id=ctx.settings.tunnel_id,
        healthy=all(u.active == "active" for u in units) and tunnel_serving(health),
        units=units,
        state=health.state,
        detail=health.detail,
        initialize=health.counts.initialize,
        tools_list=health.counts.tools_list,
        tools_call=health.counts.tools_call,
    )


def run(args: argparse.Namespace) -> int:
    ctx = load_context()
    report = gather_tunnel(ctx) if ctx.settings.mode == "tunnel" else gather(ctx)
    print(json.dumps(asdict(report), indent=2) if args.json else report.text())
    return 0 if report.healthy else 3


COMMAND = Command(name="status", help="units, local probes and the last ChatGPT activity (--json)", run=run, configure=configure)

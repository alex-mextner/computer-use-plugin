"""`computer chatgpt doctor --heal`: the watchdog pass of the public-mode stack, meant to run from a user timer.

WHAT: watches the bridge (mcp-auth-proxy answers on its local port), the shim (answers on its local port; it proxies the bridge,
so its failure is blamed on a bridge that does not serve) and, when a tunnel is configured, cloudflared (its own /ready on the
pinned metrics port is 200 only with at least one edge connection). The policy is `watchdog.Policy`; state and lock live in the
target's state directory.
WHY THESE PROBES: measured on cloudflared 2026.10.7 and the shim of this repo: a SIGSTOPped proxy keeps its port, the kernel
accepts the connection and the request hangs, and the shim in front of it hangs the same way; cloudflared with no edge connection
(rejected credentials, or an unreachable edge as in an outage) stays active and answers /ready with 503 `readyConnections: 0`.
Neither process exits, so Restart=always never fires. The outage and the broken tunnel look the same on /ready, hence the
connectivity gate. The public URL is never probed here: it fails for reasons (DNS, Cloudflare, the internet) a local restart
cannot fix, so `doctor` reports it and never acts on it.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable

from agenttools_errors import UsageError

from computerlib.targets.chatgpt import watchdog
from computerlib.targets.chatgpt.context import Context
from computerlib.targets.chatgpt.probe import local_alive, request
from computerlib.targets.chatgpt.units import unit_name

PROBE_TIMEOUT_SECONDS = 5.0
# Two operators, by address: one provider's outage or a DNS failure alone is not "the internet is down".
CONNECTIVITY_ANCHORS: tuple[tuple[str, int], ...] = (("1.1.1.1", 443), ("8.8.8.8", 443))
STATE_FILE = "heal.json"
LOCK_FILE = "heal.lock"

Probe = Callable[[], str | None]


def wall_clock() -> float:
    # Wall time, not monotonic: the restart budget has to survive a reboot between two timer runs.
    return time.time()


def _answers(port: int) -> Probe:
    def probe() -> str | None:
        if local_alive(port, PROBE_TIMEOUT_SECONDS):
            return None
        return f"nothing answers on 127.0.0.1:{port} within {PROBE_TIMEOUT_SECONDS:g}s"

    return probe


def _edge_ready(metrics_port: int) -> Probe:
    def probe() -> str | None:
        answer = request(f"http://127.0.0.1:{metrics_port}/ready", timeout=PROBE_TIMEOUT_SECONDS)
        if answer is None:
            return f"nothing answers on 127.0.0.1:{metrics_port}/ready within {PROBE_TIMEOUT_SECONDS:g}s"
        if answer.status != 200:
            return f"/ready answered {answer.status}: no edge connection"
        return None

    return probe


def watched(ctx: Context) -> list[watchdog.Watched]:
    settings = ctx.settings
    bridge = unit_name(settings.unit_prefix, "bridge")
    units = [
        watchdog.Watched(bridge, _answers(settings.proxy_port)),
        watchdog.Watched(unit_name(settings.unit_prefix, "shim"), _answers(settings.shim_port), depends_on=bridge),
    ]
    if settings.tunnel_name:
        units.append(watchdog.Watched(unit_name(settings.unit_prefix, "tunnel"), _edge_ready(settings.metrics_port)))
    return units


def heal(ctx: Context) -> int:
    """One watchdog pass: 0 when nothing is left that heal could fix, 3 when a unit's restart budget is spent."""
    if ctx.settings.mode == "tunnel":
        raise UsageError(
            what="`doctor --heal` covers the public mode only",
            why="tunnel mode runs one tunnel-client unit, which heal has no probe for",
            fix="run `computer chatgpt doctor` to diagnose it",
        )
    journal = watchdog.on_journal(sys.stdout)

    def say(priority: int, text: str) -> None:
        print(f"<{priority}>{text}" if journal else text, flush=True)

    state_dir = ctx.layout.state_dir
    with watchdog.exclusive(state_dir / LOCK_FILE) as held:
        if not held:
            say(watchdog.INFO, f"heal: another heal pass holds {state_dir / LOCK_FILE}: skipped")
            return 0
        spent = watchdog.heal_pass(
            watched(ctx),
            state_file=state_dir / STATE_FILE,
            active_state=lambda unit: ctx.systemd.state(unit).active,
            restart=ctx.systemd.try_restart,
            online=lambda: watchdog.reachable(CONNECTIVITY_ANCHORS),
            clock=wall_clock,
            say=say,
        )
    return 3 if spent else 0

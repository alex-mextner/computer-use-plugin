"""`computer chatgpt connect`: walk through ChatGPT's "Add custom MCP server" dialog, then WAIT and show what reached this PC.

ChatGPT cannot be pre-filled, so this prints every field with its value and reason, copies the server URL to the clipboard, opens
the right page, and then ticks a live checklist from the access log: discovery, client registration, login, token, initialize,
tools/list and a real tools/call. Only a tools/call from a client the shim could verify as ChatGPT counts as connected.
Nothing here automates the ChatGPT page, reads its cookies or calls its undocumented backend; the password is never printed or
copied.

Tunnel mode: the dialog fields are the Tunnel connection's (the tunnel id is copied, it is not a secret), and the live checklist
follows tunnel-client's health port: ready, control plane polled, a discovery request came through, a tools/call came through.
Counters are baselined when the watch starts. `connected` means a tool call came through YOUR tunnel; tunnel-client cannot say
which OpenAI surface (ChatGPT, Codex, the Responses API) sent it, so this is weaker than the public mode's verified ChatGPT label,
and the output says so.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from agenttools_errors import DriftError

from computerlib.commandtree import Command
from computerlib.targets.chatgpt import dialog
from computerlib.targets.chatgpt.accessview import LogTail
from computerlib.targets.chatgpt.checklist import STEPS, Checklist
from computerlib.targets.chatgpt.context import Context, load_context
from computerlib.targets.chatgpt.lifecycle import tunnel_serving
from computerlib.targets.chatgpt.probe import challenge_ok, local_alive
from computerlib.targets.chatgpt.stack import external_url
from computerlib.targets.chatgpt.tunnel import require_tunnel_id
from computerlib.targets.chatgpt.tunnel_health import Counts, TunnelHealth, read_counts, read_state

POLL_SECONDS = 1.0
DEFAULT_TIMEOUT_SECONDS = 600.0
CLIPBOARD_TOOLS: tuple[tuple[str, list[str]], ...] = (("wl-copy", []), ("xclip", ["-selection", "clipboard"]), ("pbcopy", []))


def configure(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--name", default="Computer", help="name to give the connector (default Computer)")
    parser.add_argument("--no-open", action="store_true", help="do not open the ChatGPT page in a browser")
    parser.add_argument("--no-copy", action="store_true", help="do not copy the server URL to the clipboard")
    parser.add_argument("--no-wait", action="store_true", help="print the fields and return, without the live checklist")
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS, help="seconds to wait for the connection (default 600)"
    )


def copy_to_clipboard(text: str) -> str | None:
    """Copy `text` with the first clipboard tool found; returns the tool name, or None when there is none or it failed."""
    for tool, extra in CLIPBOARD_TOOLS:
        path = shutil.which(tool)
        if path is None:
            continue
        result = subprocess.run([path, *extra], input=text, text=True, capture_output=True, check=False)
        if result.returncode == 0:
            return tool
    return None


def open_page(url: str) -> bool:
    opener = shutil.which("xdg-open") or shutil.which("open")
    if opener is None:
        return False
    subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    return True


def print_instructions(ctx: Context, name: str, url: str) -> None:
    print(f"ChatGPT connector for {url}")
    print(f"Who can add it: {dialog.PLAN_NOTE}\n")
    print("Steps:")
    for number, step in enumerate(dialog.steps(), start=1):
        print(f"  {number}. {step}")
    fields = dialog.public_fields(url, name)
    width = max(len(f.label) for f in fields)
    print("\nFields:")
    for item in fields:
        print(f"  {item.label:<{width}}  {item.value}   ({item.why})")
    print(f"\nLogin password: the PASSWORD value in {ctx.layout.password_file} (never printed here)")
    print("If something fails:")
    for toast, cause in dialog.FAILURES:
        print(f"  - {toast}\n      {cause}")
    print()


def watch(
    ctx: Context,
    timeout: float,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> Checklist:
    """Tail the access log from its current end, tick the checklist, and stop on `connected` or the timeout."""
    tail = LogTail(ctx.layout.access_log)
    checklist = Checklist()
    deadline = clock() + timeout
    out("Waiting for ChatGPT (Ctrl-C to stop). Create the connector now.")
    try:
        while not checklist.connected and clock() < deadline:
            for record in tail.poll():
                for event in checklist.feed(record):
                    out(f"  [{'!' if event.warning else 'x'}] {event.text}")
            if not checklist.connected:
                sleep(POLL_SECONDS)
    finally:
        tail.close()
    return checklist


def report_timeout(checklist: Checklist, out: Callable[[str], None] = print) -> None:
    pending = [text for key, text in STEPS if key not in checklist.done]
    out("Not connected yet. Still missing:")
    for text in pending:
        out(f"  [ ] {text}")
    if not checklist.done:
        out("No request reached this PC: the click did not happen, ChatGPT failed before contacting it, or the URL is wrong.")


TUNNEL_EVENTS = {
    "not_ready": ("!", "tunnel-client is not ready (Desktop Commander or the daemon is still starting, or failed)"),
    "awaiting_poll": (
        "!",
        "tunnel-client cannot poll the control plane: check the key (Tunnels Read+Use), the tunnel id and the network",
    ),
    "awaiting_discovery": ("x", "tunnel-client is ready and polling the control plane"),
    "awaiting_tool_call": ("x", "a discovery request (initialize / tools/list) came through the tunnel"),
    "connected": ("x", "a tools/call came through the tunnel"),
    "unavailable": ("!", "tunnel-client stopped answering on its health port"),
}


@dataclass
class TunnelWatch:
    connected: bool = False
    last: TunnelHealth | None = None
    reached: list[str] = field(default_factory=lambda: list[str]())


def watch_tunnel(
    ctx: Context,
    timeout: float,
    out: Callable[[str], None] = print,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> TunnelWatch:
    """Poll tunnel-client's health port, print each state change once, and stop on `connected` or the timeout.

    The counters at the start are the baseline, so traffic from before `connect` does not count; if the daemon restarts (its
    counters fall below the baseline) the baseline restarts from zero.
    """
    port = ctx.settings.tunnel_health_port
    baseline = read_counts(port)
    watch = TunnelWatch()
    deadline = clock() + timeout
    out("Waiting for a request through the tunnel (Ctrl-C to stop). Create the connector now.")
    while not watch.connected and clock() < deadline:
        health = read_state(port, baseline)
        if tunnel_serving(health) and (
            health.counts.tools_call < baseline.tools_call
            or health.counts.initialize < baseline.initialize
            or health.counts.tools_list < baseline.tools_list
        ):
            baseline = Counts()
            health = read_state(port, baseline)
        if watch.last is None or health.state != watch.last.state:
            mark, text = TUNNEL_EVENTS[health.state]
            out(f"  [{mark}] {text}")
            if mark == "x":
                watch.reached.append(health.state)
        watch.last = health
        watch.connected = health.state == "connected"
        if not watch.connected:
            sleep(POLL_SECONDS)
    return watch


def print_tunnel_instructions(ctx: Context, name: str) -> None:
    print("Steps:")
    for number, step in enumerate(dialog.tunnel_steps(), start=1):
        print(f"  {number}. {step}")
    fields = dialog.tunnel_fields(ctx.settings.tunnel_id, name)
    width = max(len(f.label) for f in fields)
    print("\nFields:")
    for item in fields:
        print(f"  {item.label:<{width}}  {item.value}   ({item.why})")
    print(
        "\nDesktop Commander gives ChatGPT a shell on this PC. "
        "Use a personal OpenAI org/workspace or keep the tunnel's association tight."
    )
    print("If something fails:")
    for toast, cause in dialog.TUNNEL_FAILURES:
        print(f"  - {toast}\n      {cause}")
    print()


def run_tunnel(ctx: Context, args: argparse.Namespace) -> int:
    tunnel_id = require_tunnel_id(ctx.settings.tunnel_id)
    health = read_state(ctx.settings.tunnel_health_port)
    if health.state == "unavailable":
        raise DriftError(
            what="the tunnel is not running",
            why=health.detail,
            fix="run `computer chatgpt start` first",
        )
    if not tunnel_serving(health):
        print(f"warning: tunnel-client is {health.state} ({health.detail}); ChatGPT's discovery would fail until that is fixed")
    print_tunnel_instructions(ctx, args.name)
    if not args.no_copy:
        tool = copy_to_clipboard(tunnel_id)
        print(f"tunnel id copied to the clipboard ({tool})" if tool else f"tunnel id (no clipboard tool found): {tunnel_id}")
    if not args.no_open and not open_page(dialog.SETTINGS_PAGE):
        print(f"open {dialog.SETTINGS_PAGE} yourself (no browser opener found)")
    if args.no_wait:
        return 0
    try:
        watch = watch_tunnel(ctx, args.timeout)
    except KeyboardInterrupt:
        print("\nstopped waiting", file=sys.stderr)
        return 130
    if watch.connected:
        print(
            "Connected: a tool call came through your tunnel. tunnel-client cannot tell which OpenAI surface sent it, "
            "so this does not by "
            "itself prove it was ChatGPT; run the smoke prompt in a chat (ask it to run `get_config`) to see it end to end."
        )
        return 0
    print("Not connected yet. No tool call has come through the tunnel.")
    if watch.last is not None:
        print(f"Last state: {watch.last.state} ({watch.last.detail})")
    raise DriftError(
        what="no tool call came through the tunnel in time",
        why="see the last state above",
        fix="run `computer chatgpt doctor`, then connect again",
    )


def run(args: argparse.Namespace) -> int:
    ctx = load_context()
    if ctx.settings.mode == "tunnel":
        return run_tunnel(ctx, args)
    if not local_alive(ctx.settings.shim_port):
        raise DriftError(
            what="the stack is not running",
            why=f"nothing answers on 127.0.0.1:{ctx.settings.shim_port}",
            fix="run `computer chatgpt start` first",
        )
    base = external_url(ctx.settings)
    server_url = f"{base}/mcp"
    ok, detail = challenge_ok(base)
    if not ok:
        print(
            f"warning: the public URL does not look ready ({detail}); ChatGPT would say it could not discover the OAuth settings"
        )
    print_instructions(ctx, args.name, server_url)
    if not args.no_copy:
        tool = copy_to_clipboard(server_url)
        print(f"server URL copied to the clipboard ({tool})" if tool else f"server URL (no clipboard tool found): {server_url}")
    if not args.no_open and not open_page(dialog.SETTINGS_PAGE):
        print(f"open {dialog.SETTINGS_PAGE} yourself (no browser opener found)")
    if args.no_wait:
        return 0
    try:
        checklist = watch(ctx, args.timeout)
    except KeyboardInterrupt:
        print("\nstopped waiting", file=sys.stderr)
        return 130
    if checklist.connected:
        print(
            "Connected: ChatGPT ran a tool on this PC. "
            "Smoke prompt for later: ask it to run `get_config` and report the platform."
        )
        return 0
    report_timeout(checklist)
    raise DriftError(
        what="ChatGPT did not connect in time",
        why="see the missing steps above",
        fix="run `computer chatgpt doctor`, then connect again",
    )


COMMAND = Command(
    name="connect", help="show ChatGPT's dialog fields and verify the connection live", run=run, configure=configure
)

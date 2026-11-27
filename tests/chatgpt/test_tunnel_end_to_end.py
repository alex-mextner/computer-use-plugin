"""The REAL tunnel-client (pinned build) relaying to a stdio MCP server, against a stand-in control plane.

Opt-in: COMPUTER_INTEGRATION=1 and COMPUTER_TUNNEL_BIN=<tunnel-client v0.0.17 binary>. This proves the part of the tunnel mode
that can be proven without OpenAI: the process definition we generate starts the real daemon, the daemon reads the key from its
0600 file, spawns the stdio server unmodified, forwards initialize / tools/list / tools/call and posts the answers, and our state
machine follows it.
It does NOT prove that ChatGPT can reach the tunnel: that needs a Platform key with Tunnels Read+Use and a tunnel linked to the
ChatGPT workspace, and no tool call from ChatGPT has gone through this mode.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Generator
from dataclasses import replace
from pathlib import Path

import pytest

from computerlib.targets.chatgpt.install import sha256_file
from computerlib.targets.chatgpt.tunnel import tunnel_client, write_key
from computerlib.targets.chatgpt.tunnel_health import read_counts, read_state
from tests.chatgpt.cli_helpers import free_port
from tests.chatgpt.fake_control_plane import ControlPlane, running_control_plane

TUNNEL_BIN = os.environ.get("COMPUTER_TUNNEL_BIN", "")
pytestmark = pytest.mark.skipif(
    not (os.environ.get("COMPUTER_INTEGRATION") == "1" and TUNNEL_BIN),
    reason="set COMPUTER_INTEGRATION=1 and COMPUTER_TUNNEL_BIN=<tunnel-client> to run the real-daemon proofs",
)
STUB = Path(__file__).with_name("stub_mcp.py")
TUNNEL_ID = "tunnel_abcdefghijklmnopqrstuvwxyz012345"
KEY = "sk-test-only-not-a-real-key"
EXPECTED_SHA256 = "e163ff89cfc3c8bd9d196fe3fd622ba39d009af9935e38432790f39eb9b6db01"  # tunnel-client v0.0.17 linux amd64


def _wait(condition: object, seconds: float = 20.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if callable(condition) and condition():
            return True
        time.sleep(0.2)
    return False


class Daemon:
    def __init__(self, plane: ControlPlane, key_text: str, tmp: Path) -> None:
        self.port = free_port()
        # A command WITH arguments, like the real `computer mcp serve`: proves tunnel-client splits --mcp.command into argv.
        mcp_command = (sys.executable, str(STUB), "--terminal-only")
        key_file = tmp / "tunnel-key"
        write_key(key_file, key_text)
        self.key_file = key_file
        process = tunnel_client(Path(TUNNEL_BIN), TUNNEL_ID, key_file, mcp_command, self.port, (("PATH", os.environ["PATH"]),))
        env = {**os.environ, **dict(process.env), "CONTROL_PLANE_BASE_URL": f"http://127.0.0.1:{plane.port}"}
        self.argv = list(process.argv)
        self.child = subprocess.Popen(self.argv, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def stop(self) -> None:
        self.child.terminate()
        try:
            self.child.wait(15)
        except subprocess.TimeoutExpired:
            self.child.kill()
            self.child.wait()


@pytest.fixture
def plane() -> Generator[ControlPlane]:
    with running_control_plane(TUNNEL_ID, KEY) as control_plane:
        yield control_plane


def test_the_pinned_binary_is_the_one_these_proofs_ran_against() -> None:
    if os.uname().machine == "x86_64":
        assert sha256_file(Path(TUNNEL_BIN)) == EXPECTED_SHA256


def test_discovery_and_a_tool_call_flow_through_the_real_daemon_and_the_state_machine_follows(
    plane: ControlPlane, tmp_path: Path
) -> None:
    daemon = Daemon(plane, KEY, tmp_path)
    try:
        assert _wait(lambda: read_state(daemon.port).state in ("awaiting_discovery", "awaiting_tool_call", "connected"))
        baseline = read_counts(daemon.port)
        assert read_state(daemon.port, baseline).state == "awaiting_discovery"
        hello = {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "0"}}
        plane.send("r1", {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": hello})
        plane.send("r2", {"jsonrpc": "2.0", "method": "notifications/initialized"})
        plane.send("r3", {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        listed = plane.response_to("r3")
        assert listed is not None and "get_config" in str(listed["resp_json"])
        initialized = plane.response_to("r1")
        assert initialized is not None and "0 --terminal-only" in str(initialized["resp_json"])
        assert _wait(lambda: read_state(daemon.port, baseline).state == "awaiting_tool_call")
        plane.send("r4", {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {"name": "get_config", "arguments": {}}})
        called = plane.response_to("r4")
        assert called is not None and "stub-config for get_config" in str(called["resp_json"])
        assert _wait(lambda: read_state(daemon.port, baseline).state == "connected")
    finally:
        daemon.stop()


def test_the_key_reaches_the_daemon_through_its_file_and_never_through_argv_or_environment(
    plane: ControlPlane, tmp_path: Path
) -> None:
    daemon = Daemon(plane, KEY, tmp_path)
    try:
        assert _wait(lambda: read_state(daemon.port).state != "unavailable")
        assert KEY not in " ".join(daemon.argv)
        pid = daemon.child.pid
        assert KEY.encode() not in Path(f"/proc/{pid}/cmdline").read_bytes()
        assert KEY.encode() not in Path(f"/proc/{pid}/environ").read_bytes()
        assert plane.unauthorized == 0
    finally:
        daemon.stop()


def test_a_key_the_control_plane_rejects_leaves_the_daemon_awaiting_the_poll(plane: ControlPlane, tmp_path: Path) -> None:
    daemon = Daemon(plane, "wrong-key", tmp_path)
    try:
        assert _wait(lambda: plane.unauthorized > 0)
        assert _wait(lambda: read_state(daemon.port).state in ("awaiting_poll", "not_ready"))
        assert read_state(daemon.port).state != "connected"
    finally:
        daemon.stop()


def test_the_generated_process_listens_only_on_the_configured_loopback_port() -> None:
    process = tunnel_client(Path("/bin/tunnel-client"), TUNNEL_ID, Path("/k"), ("/bin/computer", "mcp", "serve"), 8794, ())
    assert "--mcp.command" in process.argv and "/bin/computer mcp serve" in process.argv
    assert replace(process).argv[-2:] == ("--health.listen-addr", "127.0.0.1:8794")

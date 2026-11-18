"""The real stack on scratch ports: `computer chatgpt run` starts the real mcp-auth-proxy and our shim, with a stub MCP server
behind.

Opt-in (they start real processes): COMPUTER_INTEGRATION=1 and COMPUTER_PROXY_BIN=<path to the pinned mcp-auth-proxy v2.10.2>; the
binary's digest is checked against the pin table, so the proof is about the release the CLI installs.
Proves what ChatGPT's connector needs: a 401 that carries the protected-resource challenge, the full OAuth flow with PKCE and
dynamic client registration, initialize, tools/list, tools/call, the idle event stream, and a verified client label in the access
log.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from collections.abc import Generator
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlencode

import pytest

from computerlib.pins import binary_pin
from computerlib.targets.chatgpt.install import sha256_file
from computerlib.targets.chatgpt.password import ensure_password, read_password
from computerlib.targets.chatgpt.settings import Layout, Settings, save_settings
from tests.chatgpt.cli_helpers import free_ports, install_core_stand_in
from tests.chatgpt.oauth_driver import FORM, OPENAI_UA, Driver

PROXY_BIN = os.environ.get("COMPUTER_PROXY_BIN", "")
pytestmark = pytest.mark.skipif(
    not (os.environ.get("COMPUTER_INTEGRATION") == "1" and PROXY_BIN),
    reason="set COMPUTER_INTEGRATION=1 and COMPUTER_PROXY_BIN=<mcp-auth-proxy v2.10.2> to run the real-binary proofs",
)


@dataclass
class Stack:
    shim_port: int
    layout: Layout

    def password(self) -> str:
        return read_password(self.layout.password_file)

    def log(self) -> list[str]:
        path = self.layout.access_log
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not _complete_lines(path):
            time.sleep(0.05)
        return _complete_lines(path)


def _complete_lines(path: Path) -> list[str]:
    """The log's finished lines: the shim may be mid-write, and a torn last line would fail the label checks spuriously."""
    text = path.read_text() if path.exists() else ""
    return text[: text.rfind("\n") + 1].splitlines()


def _install_scratch_stack(settings: Settings, node_dir: Path) -> Layout:
    layout = Layout.of(settings)
    layout.bin_dir.mkdir(parents=True)
    shutil.copy(PROXY_BIN, layout.bin_dir / "mcp-auth-proxy")
    install_core_stand_in(node_dir)
    ensure_password(layout.password_file)
    return layout


def _wait_until_serving(port: int, process: subprocess.Popen[bytes]) -> None:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            if Driver(port).request("POST", "/mcp", "{}").status == 401:
                return
        except OSError:
            pass
        time.sleep(0.3)
    raise AssertionError(f"the stack did not come up: {_stderr_after_stopping(process)}")


def _stderr_after_stopping(process: subprocess.Popen[bytes]) -> str:
    """Stop `run` first: reading the pipe of a live process blocks until it writes or exits, hanging instead of failing.
    SIGTERM lets `run` stop its children too; every wait is bounded because an orphaned child could hold the pipe open."""
    process.terminate()
    try:
        _, err = process.communicate(timeout=20)
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            _, err = process.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            return "(no stderr: the pipe stayed open after the kill)"
    return err[-2000:].decode(errors="replace") if err else ""


@pytest.fixture
def stack(tmp_path: Path) -> Generator[Stack]:
    pin = binary_pin("mcp-auth-proxy")
    assert pin is not None
    assert sha256_file(Path(PROXY_BIN)) == pin.sha256, "COMPUTER_PROXY_BIN is not the pinned mcp-auth-proxy build"
    shim_port, proxy_port = free_ports(2)
    settings = Settings(scheme="http", hostname=f"127.0.0.1:{shim_port}", shim_port=shim_port, proxy_port=proxy_port)
    save_settings(settings)
    layout = _install_scratch_stack(settings, tmp_path)
    process = subprocess.Popen(
        [sys.executable, "-m", "computerlib.cli", "chatgpt", "run"], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    try:
        _wait_until_serving(shim_port, process)
        yield Stack(shim_port, layout)
    finally:
        process.terminate()
        try:
            process.wait(20)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    time.sleep(0.5)
    for port in (shim_port, proxy_port):
        with pytest.raises(OSError):
            Driver(port).request("GET", "/")


def test_an_unauthenticated_call_gets_the_protected_resource_challenge(stack: Stack) -> None:
    reply = Driver(stack.shim_port).request("POST", "/mcp", "{}")
    assert reply.status == 401
    assert reply.headers["www-authenticate"].startswith('Bearer resource_metadata="')
    assert reply.headers["www-authenticate"].endswith('/.well-known/oauth-protected-resource"')


def test_the_path_suffix_form_of_the_resource_metadata_is_served(stack: Stack) -> None:
    driver = Driver(stack.shim_port)
    plain = driver.request("GET", "/.well-known/oauth-protected-resource")
    suffixed = driver.request("GET", "/.well-known/oauth-protected-resource/mcp")
    assert plain.status == suffixed.status == 200
    assert plain.body == suffixed.body


def test_the_full_chatgpt_flow_reaches_a_tool_and_is_logged_with_a_verified_label(stack: Stack) -> None:
    driver = Driver(stack.shim_port)
    driver.authorize(stack.password())
    hello = {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "e2e", "version": "0"}}
    init = driver.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": hello})
    assert init.status == 200 and init.json()["result"]
    assert driver.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"}).status == 202
    listed = driver.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    assert "get_config" in listed.body.decode()
    call = {"name": "get_config", "arguments": {"secret_argument": "SECRET-ARG"}}
    called = driver.rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": call})
    assert "stub-config for get_config" in called.body.decode()
    lines = stack.log()
    text = "\n".join(lines)
    assert "client=openai-mcp rpc=tools/call tool=get_config" in text
    assert "client=openai-mcp" in next(line for line in lines if "/.idp/token" in line)
    for secret in ("SECRET-ARG", driver.token, stack.password(), "e2e-callback"):
        assert secret not in text


def test_a_token_of_a_foreign_client_and_a_forged_user_agent_are_both_unverified(stack: Stack) -> None:
    owner = Driver(stack.shim_port)
    owner.authorize(stack.password(), redirect="https://attacker.example/cb")  # the password holder registers a foreign client
    owner.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    forged = Driver(stack.shim_port, user_agent=OPENAI_UA)
    forged.token = "not-a-token"
    assert forged.rpc({"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).status == 401
    mcp_lines = [line for line in stack.log() if " /mcp " in line and "rpc=tools/list" in line]
    assert len(mcp_lines) == 2
    assert all("client=unverified" in line for line in mcp_lines)


def test_the_idle_event_stream_is_released_with_an_open_comment(stack: Stack) -> None:
    driver = Driver(stack.shim_port)
    driver.authorize(stack.password())
    head, elapsed = driver.open_event_stream()
    assert head.startswith(b"HTTP/1.1 200") and b"text/event-stream" in head
    assert elapsed < 2.0


def test_a_wrong_password_is_refused_without_a_bearer_challenge(stack: Stack) -> None:
    driver = Driver(stack.shim_port)
    driver.request(
        "GET",
        "/.idp/auth?response_type=code&client_id=x&redirect_uri=https://chatgpt.com/cb&code_challenge=abc&code_challenge_method=S256&state=state-12345",
    )
    reply = driver.request("POST", "/.auth/login", urlencode({"password": "wrong"}), FORM)
    assert reply.status == 400
    assert b"Invalid password" in reply.body
    assert "www-authenticate" not in reply.headers

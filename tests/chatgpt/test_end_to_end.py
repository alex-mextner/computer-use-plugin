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
import socket
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
from computerlib.targets.chatgpt.shim.rules import BODY_SECONDS
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
    run: subprocess.Popen[bytes]
    output: Path

    def password(self) -> str:
        return read_password(self.layout.password_file)

    def log(self) -> list[str]:
        path = self.layout.access_log
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not _complete_lines(path):
            time.sleep(0.05)
        return _complete_lines(path)

    def stop_and_read_journal(self) -> str:
        """Stop the stack, then return everything it wrote to stdout and stderr (what a unit's journal gets): stopped first so
        nothing is still in flight."""
        _stop(self.run)
        return self.output.read_text(errors="replace")


def _assert_the_journal_holds_no_request(stack: Stack) -> None:
    """The bridge's own output is what its unit writes to the journal: no per-request record, OAuth parameter or message copy."""
    journal = stack.stop_and_read_journal()
    leaks = ('"severity":"INFO"', "client_id=", "redirect_uri=", "code_challenge=", "state=", "SECRET-ARG", "stub-config")
    for leak in (*leaks, "tools/call"):
        assert leak not in journal, f"{leak!r} reached the journal:\n{journal[-2000:]}"


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


def _wait_until_serving(port: int, process: subprocess.Popen[bytes], output: Path) -> None:
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        try:
            if Driver(port).request("POST", "/mcp", "{}").status == 401:
                return
        except OSError:
            pass
        time.sleep(0.3)
    _stop(process)
    raise AssertionError(f"the stack did not come up: {output.read_text(errors='replace')[-2000:]}")


def _stop(process: subprocess.Popen[bytes]) -> None:
    """SIGTERM lets `run` stop its children too; the wait is bounded so a child that ignores it cannot hang the suite."""
    process.terminate()
    try:
        process.wait(20)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


@pytest.fixture
def stack(tmp_path: Path) -> Generator[Stack]:
    pin = binary_pin("mcp-auth-proxy")
    assert pin is not None
    assert sha256_file(Path(PROXY_BIN)) == pin.sha256, "COMPUTER_PROXY_BIN is not the pinned mcp-auth-proxy build"
    shim_port, proxy_port = free_ports(2)
    settings = Settings(scheme="http", hostname=f"127.0.0.1:{shim_port}", shim_port=shim_port, proxy_port=proxy_port)
    save_settings(settings)
    layout = _install_scratch_stack(settings, tmp_path)
    # A file, not a pipe: nobody reads while the stack runs, and a full pipe would block it.
    output = tmp_path / "run-output.log"
    with output.open("wb") as sink:
        process = subprocess.Popen([sys.executable, "-m", "computerlib.cli", "chatgpt", "run"], stdout=sink, stderr=sink)
    try:
        _wait_until_serving(shim_port, process, output)
        yield Stack(shim_port, layout, process, output)
    finally:
        _stop(process)
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
    # The trouble words make a copy of this request a candidate for the journal if the message-copy rule ever slips.
    call = {"name": "get_config", "arguments": {"secret_argument": "SECRET-ARG error timeout exited"}}
    called = driver.rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": call})
    assert "stub-config for get_config" in called.body.decode()
    lines = stack.log()
    text = "\n".join(lines)
    assert "client=openai-mcp rpc=tools/call tool=get_config" in text
    assert "client=openai-mcp" in next(line for line in lines if "/.idp/token" in line)
    for secret in ("SECRET-ARG", driver.token, stack.password(), "e2e-callback"):
        assert secret not in text
    _assert_the_journal_holds_no_request(stack)


def test_chatgpt_is_offered_no_feedback_tool_and_no_survey(stack: Stack) -> None:
    """The stand-in lists DC's feedback tool and adds DC's survey to every result (#24); through the stack ChatGPT sees neither,
    and a call of the tool is answered without reaching Desktop Commander."""
    driver = Driver(stack.shim_port)
    driver.authorize(stack.password())
    hello = {"protocolVersion": "2025-03-26", "capabilities": {}, "clientInfo": {"name": "openai-mcp", "version": "1.0.0"}}
    driver.rpc({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": hello})
    driver.rpc({"jsonrpc": "2.0", "method": "notifications/initialized"})
    listed = driver.rpc({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}).body.decode()
    feedback: dict[str, object] = {"name": "give_feedback_to_desktop_commander", "arguments": {}}
    withheld = driver.rpc({"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": feedback}).body.decode()
    called = driver.rpc({"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {"name": "get_config"}}).body.decode()
    assert "get_config" in listed and "give_feedback" not in listed
    # mcp-auth-proxy refuses a tool its tools/list did not show before serve sees the call; serve's own answer covers the rest.
    assert "tool not found" in withheld
    assert not (Path.home() / "feedback-tool-ran").exists(), "the feedback call reached Desktop Commander"
    assert "stub-config for get_config" in called and "SYSTEM INSTRUCTION" not in called and "feedback" not in called


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
    _assert_the_journal_holds_no_request(stack)  # a refused login is no reason to keep its query string


@pytest.mark.parametrize("path", ["/mcp", "/.idp/token"])
def test_a_paused_small_body_is_answered_by_the_body_deadline(stack: Stack, path: str) -> None:
    """The pinned proxy holds even its 401 to an unauthenticated /mcp until it has read a small body (Go's net/http drains up to
    256 KiB first), and its token endpoint reads the form whole: without the shim's deadline a client that sends one byte and
    pauses gets nothing for the 65 s idle timeout, holding a connection slot (#69)."""
    head = f"POST {path} HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: 1024\r\n\r\n{{"
    with socket.create_connection(("127.0.0.1", stack.shim_port), timeout=BODY_SECONDS + 5) as sock:
        sock.sendall(head.encode())
        started = time.monotonic()
        answer = sock.recv(4096)
    assert answer.startswith(b"HTTP/1.1 408 "), answer
    assert time.monotonic() - started < BODY_SECONDS + 3

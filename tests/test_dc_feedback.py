"""`computer mcp serve` offers no client Desktop Commander's feedback channel (#24): no feedback tool that runs xdg-open, no
survey request in a tool result, even with DC's `user_surveys` flag on.

The server behind serve is the stub node (tests/stub_mcp_server.py) with DC 0.3.2's feedback tool and survey text; each control
test runs the same stub without serve and must see the browser start and the survey arrive, so the stub cannot drift into proving
nothing. tests/test_dc_feedback_real.py makes the same proof against the real pinned Desktop Commander (opt-in).
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from computerlib.cli import main
from computerlib.dc_relay import FEEDBACK_TOOL, filtered_server_line, local_answer, without_survey
from tests.fakes import MINIMAL_PATH, fake_node, fake_npm, write_executable
from tests.mcp_session import McpSession, items, result_texts
from tests.stub_mcp_server import SURVEYS

SERVE = [sys.executable, "-m", "computerlib.cli", "mcp", "serve"]


def _install(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *stub_args: str) -> Path:
    """`computer mcp install` with the stub as node; returns that node."""
    bin_dir = tmp_path / "bin"
    node = fake_node(bin_dir, *stub_args)
    fake_npm(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:{MINIMAL_PATH}")
    assert main(["mcp", "install"]) == 0
    return node


def _env_with_browser(tmp_path: Path) -> tuple[dict[str, str], Path]:
    """An environment whose `xdg-open` records its argument and then, like a browser left in the foreground, never returns."""
    opened = tmp_path / "xdg-open.log"
    write_executable(tmp_path / "desktop" / "xdg-open", f'#!/bin/sh\necho "$@" >> "{opened}"\nexec /bin/sleep 60\n')
    return {"HOME": str(tmp_path / "home"), "PATH": f"{tmp_path / 'desktop'}:{MINIMAL_PATH}"}, opened


def test_serve_does_not_list_the_feedback_tool(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(tmp_path, monkeypatch, "--dc-feedback")
    env, _ = _env_with_browser(tmp_path)
    with McpSession(SERVE, env) as session:
        session.initialize(timeout=20)
        assert session.tool_names(timeout=10) == ["start_process", "read_file", "write_file"]


def test_calling_the_feedback_tool_through_serve_returns_without_starting_a_browser(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch, "--dc-feedback")
    env, opened = _env_with_browser(tmp_path)
    with McpSession(SERVE, env) as session:
        session.initialize(timeout=20)
        result = session.call(FEEDBACK_TOOL, {}, timeout=5)
        # The session goes on: the next call is answered by Desktop Commander as usual.
        assert result_texts(session.call("read_file", {"path": "/etc/hostname"}, timeout=5)) == ["read_file done"]
    assert result["isError"] is True
    assert "opens a browser" in result_texts(result)[0]
    assert not opened.exists(), f"a browser was started: {opened.read_text()}"


def test_a_feedback_call_spelled_with_json_escapes_is_withheld_too(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(tmp_path, monkeypatch, "--dc-feedback")
    env, opened = _env_with_browser(tmp_path)
    escaped = FEEDBACK_TOOL.replace("g", "\\u0067", 1)
    with McpSession(SERVE, env) as session:
        session.initialize(timeout=20)
        session.send_line(f'{{"jsonrpc":"2.0","id":7,"method":"tools/call","params":{{"name":"{escaped}"}}}}\n'.encode())
        result = session.reply(7, "tools/call", timeout=5)
    assert result["isError"] is True
    assert not opened.exists(), f"a browser was started: {opened.read_text()}"


def test_a_feedback_call_sent_as_a_notification_never_reaches_desktop_commander() -> None:
    line = json.dumps({"jsonrpc": "2.0", "method": "tools/call", "params": {"name": FEEDBACK_TOOL}}).encode() + b"\n"
    assert local_answer(line) == b""


@pytest.mark.parametrize(
    "arguments",
    [b'{"x":"\xff"}', b'{"x":' + b"1" * 5000 + b"}"],
    ids=["invalid UTF-8, which Node reads as U+FFFD", "an integer longer than Python reads, which V8 reads as a double"],
)
def test_a_feedback_call_python_reads_differently_from_node_is_still_withheld(arguments: bytes) -> None:
    line = b'{"jsonrpc":"2.0","id":"\\ud800","method":"tools/call","params":{"name":"' + FEEDBACK_TOOL.encode()
    answer = local_answer(line + b'","arguments":' + arguments + b"}}\n")
    assert answer is not None
    assert json.loads(answer)["result"]["isError"] is True  # the lone-surrogate id is echoed back intact


def test_a_line_python_cannot_read_is_answered_not_forwarded() -> None:
    deep = b'{"jsonrpc":"2.0","id":1,"method":"tools/call","params":{"name":"x","arguments":' + b"[" * 100_000 + b"]" * 100_000
    answer = local_answer(deep + b"}}\n")
    assert answer is not None
    assert json.loads(answer)["error"]["code"] == -32700


def test_a_survey_result_with_a_lone_surrogate_is_still_cleaned() -> None:
    result = {"content": [{"type": "text", "text": f"half an emoji \ud83d{SURVEYS['1']}"}]}
    line = json.dumps({"jsonrpc": "2.0", "id": 4, "result": result}).encode() + b"\n"
    assert json.loads(filtered_server_line(line))["result"]["content"][0]["text"] == "half an emoji \ud83d"


def test_control_the_stub_feedback_tool_starts_a_browser_and_blocks(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node = _install(tmp_path, monkeypatch, "--dc-feedback")
    env, opened = _env_with_browser(tmp_path)
    with McpSession([str(node)], env) as session:
        session.initialize(timeout=10)
        assert FEEDBACK_TOOL in session.tool_names(timeout=10)
        with pytest.raises(AssertionError, match="no answer"):
            session.call(FEEDBACK_TOOL, {}, timeout=2)
    assert "tally.so" in opened.read_text()


@pytest.mark.parametrize("variant", sorted(SURVEYS))
@pytest.mark.parametrize("arguments", [{}, {"image": True}], ids=["text result", "image first"])
def test_no_tool_result_through_serve_asks_for_feedback_with_surveys_on(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, variant: str, arguments: dict[str, object]
) -> None:
    _install(tmp_path, monkeypatch, f"--dc-survey={variant}")
    env, _ = _env_with_browser(tmp_path)
    with McpSession(SERVE, env) as session:
        session.initialize(timeout=20)
        result = session.call("read_file", arguments, timeout=5)
    assert result_texts(result) == ["read_file done"]
    assert len(items(result, "content")) == len(arguments) + 1  # the image, when asked for, is kept


def test_control_the_stub_adds_the_survey(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    node = _install(tmp_path, monkeypatch, "--dc-survey=1")
    env, _ = _env_with_browser(tmp_path)
    with McpSession([str(node)], env) as session:
        session.initialize(timeout=10)
        texts = result_texts(session.call("read_file", {}, timeout=5))
    assert texts == [f"read_file done{SURVEYS['1']}"]


def test_text_that_only_quotes_the_marker_is_kept() -> None:
    quoted = '{"jsonrpc":"2.0","id":3,"result":{"content":[{"type":"text","text":"notes: [SYSTEM INSTRUCTION]: feedback"}]}}\n'
    assert filtered_server_line(quoted.encode()) == quoted.encode()
    docker = "\n\n[SYSTEM INSTRUCTION]: User is running Desktop Commander through Docker MCP Gateway. Format it like: '\n\n---'."
    assert without_survey(f"done{SURVEYS['2']}{docker}") == f"done{docker}"


def test_file_content_quoting_part_of_a_survey_survives_the_real_survey_being_removed() -> None:
    partial = SURVEYS["1"][: SURVEYS["1"].index("You've seen")]
    content = f"File starts.{partial}IMPORTANT FILE CONTENT THAT MUST SURVIVE"
    assert without_survey(f"{content}{SURVEYS['3']}") == content
    # A whole survey quoted inside a file is not where DC puts its own (the end of the text), so it stays as well.
    quoted = f"File starts.{SURVEYS['2']}\n\nmore of the file"
    assert without_survey(f"{quoted}{SURVEYS['1']}") == quoted


def test_lines_that_are_not_json_pass_through_unchanged() -> None:
    line = f"not json {FEEDBACK_TOOL} [SYSTEM INSTRUCTION]: \n".encode()
    assert filtered_server_line(line) == line


def test_serve_exits_with_desktop_commanders_status(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(tmp_path, monkeypatch, "--crash")
    env, _ = _env_with_browser(tmp_path)
    done = subprocess.run(SERVE, env=env, stdin=subprocess.DEVNULL, capture_output=True, timeout=20, check=False)
    assert done.returncode == 5
    assert done.stdout == b""
    assert b"boom from the server" in done.stderr


def test_serve_ends_with_desktop_commander_while_the_client_keeps_stdin_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install(tmp_path, monkeypatch, "--crash")
    env, _ = _env_with_browser(tmp_path)
    with McpSession(SERVE, env) as session:
        assert session.proc.wait(timeout=10) == 5


def test_serve_stops_desktop_commander_when_the_client_closes_its_stdout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(tmp_path, monkeypatch)
    env, _ = _env_with_browser(tmp_path)
    with McpSession(SERVE, env) as session:
        assert session.proc.stdout is not None
        session.proc.stdout.close()
        session.send({"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        # serve's answer hits the closed pipe: it terminates DC and ends the way DC did.
        assert session.proc.wait(timeout=10) == -signal.SIGTERM


def test_sigterm_to_serve_reaches_desktop_commander(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(tmp_path, monkeypatch)
    env, _ = _env_with_browser(tmp_path)
    with McpSession(SERVE, env) as session:
        session.initialize(timeout=20)
        session.proc.send_signal(signal.SIGTERM)
        # serve ends only once DC has, and the way DC did: killed by the forwarded SIGTERM.
        assert session.proc.wait(timeout=10) == -signal.SIGTERM


def test_sigterm_ends_serve_while_the_client_is_not_reading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(tmp_path, monkeypatch)
    env, _ = _env_with_browser(tmp_path)
    with McpSession(SERVE, env) as session:
        session.initialize(timeout=20)
        # A result far larger than the pipe, never read: serve is left blocked writing it to the client.
        session.send(
            {"jsonrpc": "2.0", "id": 9, "method": "tools/call", "params": {"name": "read_file", "arguments": {"size": 4 << 20}}}
        )
        assert session.proc.stdout is not None
        ready, _, _ = select.select([session.proc.stdout], [], [], 10)
        assert ready, "serve did not start writing the result"
        session.proc.send_signal(signal.SIGTERM)
        assert session.proc.wait(timeout=10) == -signal.SIGTERM


def test_a_signal_serve_was_started_ignoring_stays_ignored(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install(tmp_path, monkeypatch)
    env, _ = _env_with_browser(tmp_path)
    # `trap ""` leaves SIGHUP ignored across the exec, as `nohup` does.
    with McpSession(["/bin/sh", "-c", 'trap "" HUP; exec "$@"', "sh", *SERVE], env) as session:
        session.initialize(timeout=20)
        session.proc.send_signal(signal.SIGHUP)
        # DC inherited the ignore and lives on, so the relay must too: the session keeps working.
        assert session.tool_names(timeout=5) == ["start_process", "read_file", "write_file"]


def test_killing_serve_alone_stops_desktop_commander(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A DC that never reads stdin (so its EOF cannot stop it) still ends when serve is SIGKILLed, as when it ran in serve's
    place."""
    _install(tmp_path, monkeypatch, "--hang")
    env, _ = _env_with_browser(tmp_path)
    with McpSession(SERVE, env) as session:
        children = Path(f"/proc/{session.proc.pid}/task/{session.proc.pid}/children")
        deadline = time.monotonic() + 20
        while not children.read_text().split() and time.monotonic() < deadline:
            time.sleep(0.05)
        assert children.read_text().split(), "serve never started DC"
        session.proc.kill()
        session.proc.wait(timeout=10)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                os.killpg(session.proc.pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.05)
        pytest.fail("DC outlived serve")

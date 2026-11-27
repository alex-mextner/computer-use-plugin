"""ChatGPT's terminal profile withholds file widgets and their stale calls before they reach Desktop Commander."""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path
from typing import Literal

import pytest

from computerlib.cli import main
from computerlib.dc_relay import filtered_server_line, local_answer
from computerlib.targets.chatgpt import dialog
from computerlib.targets.chatgpt.processes import processes
from computerlib.targets.chatgpt.settings import Layout, Settings
from tests.fakes import MINIMAL_PATH, fake_node, fake_npm
from tests.mcp_session import McpSession, result_texts

TERMINAL_NAMES = [
    "start_process",
    "read_process_output",
    "interact_with_process",
    "force_terminate",
    "list_sessions",
    "list_processes",
    "kill_process",
]
SERVE = [sys.executable, "-m", "computerlib.cli", "mcp", "serve"]


def _line(message: dict[str, object]) -> bytes:
    return json.dumps(message).encode() + b"\n"


def _call(name: str, origin: str | None = None) -> bytes:
    arguments: dict[str, object] = {"path": "/does-not-exist", "command": "must not execute"}
    if origin is not None:
        arguments["origin"] = origin
    return _line({"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": name, "arguments": arguments}})


@pytest.mark.parametrize("mode", ["public", "tunnel"])
def test_chatgpt_launchers_select_terminal_profile(mode: Literal["public", "tunnel"]) -> None:
    settings = Settings(mode=mode, hostname="host.example.test", tunnel_id="test")
    launched = processes(settings, Layout.of(settings), ("/opt/tools/computer",))[0]
    command = launched.argv if mode == "public" else tuple(launched.argv[launched.argv.index("--mcp.command") + 1].split())
    assert any(command[i : i + 3] == ("mcp", "serve", "--terminal-only") for i in range(len(command) - 2))


def test_terminal_catalog_preserves_command_contracts_and_removes_ui_templates() -> None:
    annotations = {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}
    schema = {"type": "object", "properties": {"command": {"type": "string"}}}
    ui = {
        "openai/outputTemplate": "ui://dc/preview",
        "openai/widgetAccessible": True,
        "ui/resourceUri": "ui://dc/preview",
        "ui": {"resourceUri": "ui://dc/preview"},
        "vendor/keep": 1,
        "securitySchemes": [{"type": "oauth2", "scopes": ["commands"]}],
        "mcp/www_authenticate": ['Bearer realm="commands"'],
    }
    listed = [
        {"name": name, "annotations": annotations, "inputSchema": schema, "description": "original command help", "_meta": ui}
        for name in TERMINAL_NAMES
    ]
    listed.extend([{"name": "read_file", "_meta": ui}, {"name": "future_widget"}, {"name": "give_feedback_to_desktop_commander"}])
    raw = _line({"jsonrpc": "2.0", "id": 2, "result": {"tools": listed}})
    result = json.loads(filtered_server_line(raw, terminal_only=True))["result"]
    assert [tool["name"] for tool in result["tools"]] == TERMINAL_NAMES
    for tool in result["tools"]:
        assert tool["annotations"] == {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True}
        assert tool["inputSchema"] == schema
        if tool["name"] == "start_process":
            assert tool["description"].startswith("original command help")
            assert "terminal/process tools only" in tool["description"]
        assert tool["_meta"] == {
            "vendor/keep": 1,
            "securitySchemes": [{"type": "oauth2", "scopes": ["commands"]}],
            "mcp/www_authenticate": ['Bearer realm="commands"'],
        }
    assert [tool["name"] for tool in json.loads(filtered_server_line(raw))["result"]["tools"]][-2:] == [
        "read_file",
        "future_widget",
    ]


@pytest.mark.parametrize(
    "name,origin",
    [
        ("read_file", None),
        ("read_file", "ui"),
        ("read_multiple_files", "llm"),
        ("write_file", None),
        ("future_widget", None),
        ("start_process", "ui"),
    ],
)
def test_hidden_and_ui_origin_calls_are_answered_locally(name: str, origin: str | None) -> None:
    answer = json.loads(local_answer(_call(name, origin), terminal_only=True) or b"null")
    assert answer["id"] == 7 and answer["result"]["isError"] is True
    text = answer["result"]["content"][0]["text"]
    assert "Do not retry" in text and "bounded shell commands" in text
    assert local_answer(_call(name, origin)) is None


def test_terminal_calls_without_ui_origin_are_forwarded_byte_for_byte() -> None:
    for name in TERMINAL_NAMES:
        assert local_answer(_call(name, "llm"), terminal_only=True) is None
    raw = b'{ "jsonrpc": "2.0", "id": 4, "result": {"content": [{"type":"text","text":"read_file ui://example"}]}}\n'
    assert filtered_server_line(raw, terminal_only=True) == raw


def test_hidden_tool_notifications_are_consumed_without_a_reply() -> None:
    message = json.loads(_call("read_file", "ui"))
    del message["id"]
    assert local_answer(_line(message), terminal_only=True) == b""


@pytest.mark.parametrize("method,key", [("resources/list", "resources"), ("resources/templates/list", "resourceTemplates")])
def test_terminal_resource_lists_are_empty_and_never_forwarded(method: str, key: str) -> None:
    answer = json.loads(local_answer(_line({"jsonrpc": "2.0", "id": 3, "method": method}), terminal_only=True) or b"null")
    assert answer == {"jsonrpc": "2.0", "id": 3, "result": {key: []}}


@pytest.mark.parametrize("method", ["resources/read", "resources/subscribe", "resources/unsubscribe"])
def test_old_widget_resources_are_refused_locally(method: str) -> None:
    raw = _line({"jsonrpc": "2.0", "id": 3, "method": method, "params": {"uri": "ui://desktop-commander/file-preview"}})
    answer = json.loads(local_answer(raw, terminal_only=True) or b"null")
    assert answer["error"]["code"] == -32601
    assert local_answer(raw) is None


def test_terminal_initialize_advertises_commands_and_no_resources() -> None:
    raw = _line(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {
                "serverInfo": {"name": "dc"},
                "capabilities": {"tools": {}, "resources": {}, "logging": {}},
                "instructions": "Use file previews",
            },
        }
    )
    result = json.loads(filtered_server_line(raw, terminal_only=True))["result"]
    assert result["capabilities"] == {"tools": {}, "logging": {}}
    assert "bounded shell commands" in result["instructions"] and "Do not retry" in result["instructions"]
    assert filtered_server_line(raw) == raw


def test_terminal_serve_rejects_stale_calls_without_reaching_child_and_keeps_session_usable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    received = tmp_path / "received.jsonl"
    fake_node(bin_dir, "--dc-feedback", "--dc-survey=1", f"--record-calls={received}")
    fake_npm(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:{MINIMAL_PATH}")
    assert main(["mcp", "install"]) == 0
    env = dict(os.environ)
    with McpSession([*SERVE, "--terminal-only"], env) as session:
        session.initialize(timeout=10)
        assert session.tool_names(timeout=5) == ["start_process"]
        calls: list[tuple[str, dict[str, object]]] = [
            ("read_file", {"path": str(tmp_path), "origin": "ui"}),
            ("start_process", {"command": "must not execute", "origin": "ui"}),
        ]
        for name, arguments in calls:
            result = session.call(name, arguments, timeout=5)
            assert result["isError"] is True
            assert "Do not retry" in result_texts(result)[0]
        assert result_texts(session.call("start_process", {"command": "printf ok"}, timeout=5)) == ["start_process done"]
    assert [json.loads(line) for line in received.read_text().splitlines()] == [
        {"name": "start_process", "arguments": {"command": "printf ok"}}
    ]


@pytest.mark.parametrize("name", ["read_\\u0066ile", "start_process"])
def test_json_escaped_ui_calls_cannot_reach_the_child(name: str) -> None:
    raw = (
        '{"jsonrpc":"2.0","id":8,"method":"tools/call","params":{"name":"' + name + '","arguments":{"origin":"\\u0075i"}}}\n'
    ).encode()
    answer = json.loads(local_answer(raw, terminal_only=True) or b"null")
    assert answer["id"] == 8 and answer["result"]["isError"] is True


def test_command_result_keeps_auth_challenge_but_cannot_attach_a_preview() -> None:
    raw = _line(
        {
            "jsonrpc": "2.0",
            "id": 9,
            "result": {
                "content": [{"type": "text", "text": "authorization needed"}],
                "_meta": {"openai/outputTemplate": "ui://dc/preview", "mcp/www_authenticate": ['Bearer realm="commands"']},
            },
        }
    )
    result = json.loads(filtered_server_line(raw, terminal_only=True))["result"]
    assert result["content"] == [{"type": "text", "text": "authorization needed"}]
    assert result["_meta"] == {"mcp/www_authenticate": ['Bearer realm="commands"']}


def test_resource_notifications_are_withheld_and_other_server_requests_are_preserved() -> None:
    raw = _line({"jsonrpc": "2.0", "method": "notifications/resources/updated", "params": {"uri": "ui://dc/preview"}})
    assert filtered_server_line(raw, terminal_only=True) == b""
    request = _line({"jsonrpc": "2.0", "id": 4, "method": "roots/list"})
    assert filtered_server_line(request, terminal_only=True) == request


def test_setup_smoke_prompt_names_a_tool_the_profile_can_execute() -> None:
    prompt = " ".join(dialog.tunnel_steps())
    match = re.search(r"ask it to run `([^`]+)`", prompt)
    assert match is not None
    assert local_answer(_call(match.group(1)), terminal_only=True) is None


def test_ui_origin_terminal_refusal_explains_how_model_calls_can_proceed() -> None:
    reply = json.loads(local_answer(_call("start_process", "ui"), terminal_only=True) or b"null")
    text = reply["result"]["content"][0]["text"]
    assert "origin='llm'" in text and "omit origin" in text
    assert "Do not retry this UI request" in text


@pytest.mark.parametrize("line", [b"\n", b" \t\n", b"startup message\n", b'{"id":2,"result":', b"[]\n"])
def test_terminal_profile_discards_non_message_child_output_without_inventing_a_reply(line: bytes) -> None:
    assert filtered_server_line(line, terminal_only=True) == b""
    assert filtered_server_line(line) == line


@pytest.mark.parametrize(
    "value", [[{"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "read_file"}}], [], 1, None]
)
def test_terminal_profile_refuses_non_object_requests_before_the_child(value: object) -> None:
    line = json.dumps(value).encode() + b"\n"
    reply = json.loads(local_answer(line, terminal_only=True) or b"null")
    assert reply["error"]["code"] == -32600
    assert local_answer(line) is None

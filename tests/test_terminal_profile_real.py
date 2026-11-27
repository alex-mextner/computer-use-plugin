"""Retained compatibility proof against the pinned Desktop Commander; uses only scratch homes and an existing install."""

from __future__ import annotations

import sys
from pathlib import Path

from computerlib.jsonvalue import as_dict
from tests.mcp_session import McpSession, items, result_texts
from tests.real_dc import ENTRY, REAL_DC, real_dc_home

pytestmark = REAL_DC
SERVE = [sys.executable, "-m", "computerlib.cli", "mcp", "serve", "--terminal-only"]
NAMES = {
    "start_process",
    "read_process_output",
    "interact_with_process",
    "force_terminate",
    "list_sessions",
    "list_processes",
    "kill_process",
}


def test_terminal_profile_matches_the_real_dc_contract_and_stops_stale_reads(tmp_path: Path) -> None:
    raw_home = real_dc_home(tmp_path / "raw", {})
    with McpSession([raw_home.node, str(ENTRY), "--no-onboarding"], raw_home.env) as session:
        session.initialize(timeout=15)
        raw = {str(as_dict(tool).get("name")): as_dict(tool) for tool in items(session.request("tools/list", {}, 10), "tools")}
    assert NAMES <= raw.keys()
    assert as_dict(raw["read_file"].get("_meta")).get("openai/outputTemplate"), "the control must expose a file preview"

    home = real_dc_home(tmp_path / "terminal", {})
    with McpSession(SERVE, home.env) as session:
        initialized = session.initialize(timeout=15)
        listed = {str(as_dict(tool).get("name")): as_dict(tool) for tool in items(session.request("tools/list", {}, 10), "tools")}
        assert listed.keys() == NAMES
        for name, tool in listed.items():
            assert tool.get("inputSchema") == raw[name].get("inputSchema")
            assert tool.get("annotations") == raw[name].get("annotations")
            assert "_meta" not in tool, "the pinned terminal tools have no UI metadata"
        assert "resources" not in as_dict(initialized.get("capabilities"))
        assert session.request("resources/list", {}, 5) == {"resources": []}
        assert session.request("resources/templates/list", {}, 5) == {"resourceTemplates": []}
        denied = session.call("read_file", {"path": str(tmp_path / "missing-sentinel"), "origin": "ui"}, 5)
        assert denied["isError"] is True and "Do not retry" in " ".join(result_texts(denied))
        output = session.call("start_process", {"command": "printf RDC_TERMINAL_PROFILE_OK", "timeout_ms": 1000}, 10)
        assert "RDC_TERMINAL_PROFILE_OK" in " ".join(result_texts(output))
        for item in items(output, "content"):
            block = as_dict(item)
            uri = as_dict(block.get("resource")).get("uri") if block.get("type") == "resource" else block.get("uri")
            assert not (isinstance(uri, str) and uri.startswith("ui://"))

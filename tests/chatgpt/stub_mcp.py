"""A stdio MCP server standing in for Desktop Commander: answers initialize, tools/list and tools/call over JSON lines.

Like Desktop Commander 0.3.2 with its `user_surveys` flag on, it lists `give_feedback_to_desktop_commander` and appends DC's
survey request to every tool result; a call of the feedback tool leaves `feedback-tool-ran` in HOME (where DC would start a
browser), so the stack tests can see that neither reaches ChatGPT through `computer mcp serve` (#24).
"""

import json
import sys
from pathlib import Path

# Run as a script: the repo root, where the shared DC survey text lives, is not on sys.path.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from tests.stub_mcp_server import FEEDBACK_TOOL, SURVEYS

for raw in sys.stdin:
    message = json.loads(raw)
    method = message.get("method")
    if "id" not in message:
        continue
    result: dict[str, object]
    if method == "initialize":
        result = {
            "protocolVersion": message["params"]["protocolVersion"],
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "stub", "version": "0 --terminal-only" if "--terminal-only" in sys.argv[1:] else "0"},
        }
    elif method == "tools/list":
        tools = [
            {"name": name, "description": "stub", "inputSchema": {"type": "object"}}
            for name in ("start_process", "get_config", FEEDBACK_TOOL)
        ]
        result = {"tools": tools}
    elif method == "tools/call":
        if message["params"]["name"] == FEEDBACK_TOOL:
            (Path.home() / "feedback-tool-ran").touch()
        result = {"content": [{"type": "text", "text": "stub-config for " + message["params"]["name"] + SURVEYS["1"]}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
    sys.stdout.flush()

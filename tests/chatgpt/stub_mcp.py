"""A stdio MCP server standing in for Desktop Commander: answers initialize, tools/list and tools/call over JSON lines."""

from __future__ import annotations

import json
import sys

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
            "serverInfo": {"name": "stub", "version": "0"},
        }
    elif method == "tools/list":
        result = {"tools": [{"name": "get_config", "description": "stub", "inputSchema": {"type": "object"}}]}
    elif method == "tools/call":
        result = {"content": [{"type": "text", "text": "stub-config for " + message["params"]["name"]}]}
    else:
        result = {}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
    sys.stdout.flush()

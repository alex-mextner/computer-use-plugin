"""Stub stdio MCP server: newline-delimited JSON-RPC, enough for initialize + tools/list.

argv[1:] is echoed as serverInfo.version so a test can see which entry file the launcher passed to node.
`--crash` exits 5 with a message on stderr before reading anything; `--hang` never answers; `--no-tools` lists nothing.
"""

from __future__ import annotations

import json
import sys
import time


def main() -> None:
    args = sys.argv[1:]
    if "--crash" in args:
        sys.stderr.write("boom from the server\n")
        sys.exit(5)
    if "--hang" in args:
        time.sleep(600)
    tools = [] if "--no-tools" in args else [{"name": "start_process"}, {"name": "read_file"}, {"name": "write_file"}]
    for line in sys.stdin:
        message = json.loads(line)
        method = message.get("method")
        result: dict[str, object]
        if method == "initialize":
            result = {
                "protocolVersion": "2024-11-05",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "stub-dc", "version": " ".join(args)},
            }
        elif method == "tools/list":
            result = {"tools": tools}
        else:
            continue
        sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": message["id"], "result": result}) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()

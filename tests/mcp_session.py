"""A test-side stdio MCP client that keeps a session open across several requests (mcp_probe.handshake stops after tools/list)."""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from types import TracebackType
from typing import Self, cast

from computerlib.jsonvalue import as_dict, is_object


class McpSession:
    def __init__(self, argv: Sequence[str], env: Mapping[str, str]) -> None:
        self.proc = subprocess.Popen(
            list(argv), env=dict(env), stdin=subprocess.PIPE, stdout=subprocess.PIPE, start_new_session=True
        )
        self._buffer = bytearray()
        self._next_id = 0

    def __enter__(self) -> Self:
        return self

    def __exit__(self, kind: type[BaseException] | None, error: BaseException | None, trace: TracebackType | None) -> None:
        with suppress(ProcessLookupError):
            os.killpg(self.proc.pid, signal.SIGKILL)
        self.proc.wait()
        for stream in (self.proc.stdin, self.proc.stdout):
            if stream is not None:
                with suppress(OSError):
                    stream.close()

    def send(self, message: dict[str, object]) -> None:
        self.send_line(json.dumps(message).encode() + b"\n")

    def send_line(self, line: bytes) -> None:
        assert self.proc.stdin is not None
        self.proc.stdin.write(line)
        self.proc.stdin.flush()

    def request(self, method: str, params: dict[str, object], timeout: float) -> dict[str, object]:
        """The `result` of the response to this request; fails the test when none arrives within `timeout` seconds."""
        self._next_id += 1
        self.send({"jsonrpc": "2.0", "id": self._next_id, "method": method, "params": params})
        return self.reply(self._next_id, method, timeout)

    def reply(self, request_id: int, method: str, timeout: float) -> dict[str, object]:
        """The `result` of the response to `request_id`, a request already sent (`method` names it in a failure)."""
        assert self.proc.stdout is not None
        fd = self.proc.stdout.fileno()
        deadline = time.monotonic() + timeout
        while True:
            while b"\n" in self._buffer:
                raw, _, rest = bytes(self._buffer).partition(b"\n")
                self._buffer[:] = rest
                with suppress(ValueError):
                    message = as_dict(json.loads(raw))
                    if message.get("id") == request_id and "method" not in message:
                        assert is_object(message.get("result")), f"{method} failed: {message}"
                        return as_dict(message["result"])
            remaining = deadline - time.monotonic()
            ready, _, _ = select.select([fd], [], [], max(remaining, 0))
            assert remaining > 0 and ready, f"no answer to {method} within {timeout} s"
            chunk = os.read(fd, 65536)
            assert chunk, f"the server exited before answering {method}"
            self._buffer.extend(chunk)

    def initialize(self, timeout: float) -> dict[str, object]:
        params: dict[str, object] = {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "openai-mcp", "version": "1.0.0"},
        }
        result = self.request("initialize", params, timeout)
        self.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return result

    def tool_names(self, timeout: float) -> list[str]:
        return [str(as_dict(tool).get("name")) for tool in items(self.request("tools/list", {}, timeout), "tools")]

    def call(self, name: str, arguments: dict[str, object], timeout: float) -> dict[str, object]:
        return self.request("tools/call", {"name": name, "arguments": arguments}, timeout)


def items(result: dict[str, object], key: str) -> list[object]:
    value = result.get(key)
    assert isinstance(value, list), f"no {key} list in {result}"
    return cast("list[object]", value)


def result_texts(result: dict[str, object]) -> list[str]:
    entries = [as_dict(item) for item in items(result, "content")]
    return [str(entry.get("text")) for entry in entries if entry.get("type") == "text"]

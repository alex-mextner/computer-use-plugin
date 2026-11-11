"""A real MCP handshake against a spawned stdio server: the only honest proof that a configured command works.

HOW IT IS REACHED: `computer claude status` (and any future adapter's status) calls `handshake(command, args, env, timeout)`.
INVARIANTS: sends `initialize`, `notifications/initialized`, `tools/list` as newline-delimited JSON-RPC; every wait in the
exchange is bounded by `timeout`; the child and its process group are always terminated (then force-stopped); non-JSON lines on
the server's stdout are ignored; the server's stderr goes to a temp file and only its last bytes are quoted in a failure (stdout
stays the protocol).
"""

from __future__ import annotations

import json
import os
import select
import signal
import subprocess
import tempfile
import time
from collections.abc import Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from typing import cast

from computerlib.jsonvalue import as_dict

PROTOCOL_VERSION = "2024-11-05"
_STDERR_TAIL_BYTES = 300


class HandshakeError(Exception):
    """The server did not complete the handshake; the message says why."""


@dataclass(frozen=True)
class HandshakeResult:
    server_name: str
    server_version: str
    tools: tuple[str, ...]


def _send(proc: subprocess.Popen[bytes], message: dict[str, object]) -> None:
    assert proc.stdin is not None
    try:
        proc.stdin.write((json.dumps(message) + "\n").encode())
        proc.stdin.flush()
    except OSError as exc:
        raise HandshakeError(f"server closed its input (exit {proc.poll()})") from exc


def _pop_reply(buffer: bytearray, request_id: int) -> dict[str, object] | None:
    """Consume complete lines from `buffer` until the response to `request_id` appears; noise and other messages are dropped."""
    while b"\n" in buffer:
        raw, _, rest = bytes(buffer).partition(b"\n")
        buffer[:] = rest
        try:
            loaded: object = json.loads(raw)
        except ValueError:
            continue
        message = as_dict(loaded)
        # A server-to-client request (roots/list, sampling) may reuse our id; only a response has no "method".
        if type(message.get("id")) is int and message["id"] == request_id and "method" not in message:
            return message
    return None


def _await_reply(proc: subprocess.Popen[bytes], buffer: bytearray, request_id: int, deadline: float) -> dict[str, object]:
    assert proc.stdout is not None
    fd = proc.stdout.fileno()
    while True:
        reply = _pop_reply(buffer, request_id)
        if reply is not None:
            return reply
        remaining = deadline - time.monotonic()
        ready, _, _ = select.select([fd], [], [], max(remaining, 0))
        if remaining <= 0 or not ready:
            raise HandshakeError("timed out waiting for the server")
        chunk = os.read(fd, 65536)
        if not chunk:
            raise HandshakeError(f"server exited before answering (exit {proc.poll()})")
        buffer.extend(chunk)


def _result(reply: dict[str, object], method: str) -> dict[str, object]:
    if not isinstance(reply.get("result"), dict):
        raise HandshakeError(f"{method} returned an error: {reply.get('error')}")
    return as_dict(reply["result"])


def _tool_names(listing: dict[str, object]) -> tuple[str, ...]:
    tools = listing.get("tools")
    if not isinstance(tools, list):
        return ()
    names = (as_dict(tool).get("name") for tool in cast("list[object]", tools))
    return tuple(name for name in names if isinstance(name, str))


def _stderr_tail(log: tempfile._TemporaryFileWrapper[bytes]) -> str:  # pyright: ignore[reportPrivateUsage]
    log.flush()
    log.seek(0, os.SEEK_END)
    size = log.tell()
    log.seek(max(0, size - _STDERR_TAIL_BYTES))
    return log.read().decode(errors="replace").strip()


def handshake(command: str, args: Sequence[str], env: Mapping[str, str], timeout: float) -> HandshakeResult:
    deadline = time.monotonic() + timeout
    with tempfile.NamedTemporaryFile() as log:
        try:
            proc = subprocess.Popen(
                [command, *args], env=dict(env), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=log, start_new_session=True
            )
        except OSError as exc:
            raise HandshakeError(f"cannot start {command}: {exc.strerror or exc}") from exc
        try:
            return _exchange(proc, deadline)
        except HandshakeError as exc:
            tail = _stderr_tail(log)
            raise HandshakeError(f"{exc}: {tail}" if tail else str(exc)) from exc
        finally:
            _stop(proc)
            for stream in (proc.stdin, proc.stdout):
                if stream is not None:
                    # A server that died early leaves unflushed bytes; closing re-raises that failed flush.
                    with suppress(OSError):
                        stream.close()


def _stop(proc: subprocess.Popen[bytes]) -> None:
    """Stop the server and anything it spawned (its own session), politely first."""
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        with suppress(ProcessLookupError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait()


def _exchange(proc: subprocess.Popen[bytes], deadline: float) -> HandshakeResult:
    buffer = bytearray()
    params: dict[str, object] = {
        "protocolVersion": PROTOCOL_VERSION,
        "capabilities": {},
        "clientInfo": {"name": "computer-status", "version": "1"},
    }
    _send(proc, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": params})
    init = _result(_await_reply(proc, buffer, 1, deadline), "initialize")
    _send(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    _send(proc, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}})
    listing = _result(_await_reply(proc, buffer, 2, deadline), "tools/list")
    info = as_dict(init.get("serverInfo"))
    return HandshakeResult(str(info.get("name", "")), str(info.get("version", "")), _tool_names(listing))

"""Streams through the shim: a client that leaves (also while the stream is idle) or stops reading must not pin threads, sockets
or memory.

Regressions of the review: after the idle `: open` comment the handler and the pump thread stayed blocked for ever once the client
disconnected, and the pump's queue was unbounded, so a client that stopped reading made the shim buffer a whole stream.
"""

from __future__ import annotations

import os
import select
import socket
import threading
import time
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler
from pathlib import Path

from tests.chatgpt.shim_harness import running_shim

REQUEST = b"GET /mcp HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer t\r\nAccept: text/event-stream\r\n\r\n"
POST_STREAM = b"POST /mcp HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer t\r\nContent-Length: 2\r\n\r\n{}"


class Probe:
    """An upstream whose streams record how many of them are still connected and how much they managed to send."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.open = 0
        self.sent = 0

    def _enter(self, handler: BaseHTTPRequestHandler) -> None:
        with self.lock:
            self.open += 1
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        handler.wfile.flush()

    def _leave(self) -> None:
        with self.lock:
            self.open -= 1

    def idle(self, handler: BaseHTTPRequestHandler) -> None:
        """Opens an event stream, says nothing, and ends only when the shim closes its connection."""
        self._enter(handler)
        try:
            while not _peer_closed(handler.connection):
                time.sleep(0.02)
        finally:
            self._leave()

    def endless(self, handler: BaseHTTPRequestHandler) -> None:
        """Sends 64 KiB chunks as fast as the connection takes them, until it breaks."""
        self._enter(handler)
        chunk = b"x" * 65536
        frame = f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n"
        try:
            while True:
                handler.wfile.write(frame)
                handler.wfile.flush()
                with self.lock:
                    self.sent += len(chunk)
        except OSError:
            pass
        finally:
            self._leave()

    def snapshot_sent(self) -> int:
        with self.lock:
            return self.sent


def _peer_closed(sock: socket.socket) -> bool:
    readable, _, _ = select.select([sock], [], [], 0)
    return bool(readable) and sock.recv(1, socket.MSG_PEEK) == b""


def _until(condition: Callable[[], bool], seconds: float = 6.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


def _connect(port: int, request: bytes) -> socket.socket:
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(request)
    return sock


def _read_until(sock: socket.socket, marker: bytes) -> bytes:
    data = b""
    while marker not in data:
        piece = sock.recv(4096)
        if not piece:
            break
        data += piece
    return data


def test_a_client_that_leaves_an_idle_stream_releases_the_upstream_and_every_thread(tmp_path: Path) -> None:
    probe = Probe()
    with running_shim(tmp_path) as shim:
        shim.upstream.streams["/mcp"] = probe.idle
        warm = _connect(shim.port, REQUEST)  # one full cycle first, so lazily created objects are in the baseline
        assert b": open" in _read_until(warm, b": open")
        warm.close()
        assert _until(lambda: probe.open == 0)
        threads, fds = threading.active_count(), _open_fds()
        for _ in range(25):
            sock = _connect(shim.port, REQUEST)
            assert b": open" in _read_until(sock, b": open")
            sock.close()
        assert _until(lambda: probe.open == 0), f"{probe.open} upstream stream(s) still connected after every client left"
        assert _until(lambda: threading.active_count() <= threads + 1), f"{threading.active_count()} threads, baseline {threads}"
        assert _until(lambda: _open_fds() <= fds + 2), f"{_open_fds()} descriptors, baseline {fds}"


def test_a_stalled_reader_makes_the_shim_hold_a_bounded_amount_and_leaving_ends_the_stream(tmp_path: Path) -> None:
    probe = Probe()
    with running_shim(tmp_path) as shim:
        shim.upstream.streams["/mcp"] = probe.endless
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.settimeout(5)
        sock.connect(("127.0.0.1", shim.port))
        sock.sendall(REQUEST)
        assert b"200" in _read_until(sock, b"\r\n\r\n")
        time.sleep(1.5)  # the client reads nothing; an unbounded queue would take gigabytes at loopback speed
        sent = probe.snapshot_sent()
        assert 0 < sent < 64 * 1024 * 1024, f"upstream sent {sent} bytes to a client that is not reading"
        sock.close()
        assert _until(lambda: probe.open == 0), "the upstream stream was not closed after the client left"


def test_a_stalled_reader_of_a_post_stream_is_bounded_too(tmp_path: Path) -> None:
    probe = Probe()
    with running_shim(tmp_path) as shim:
        shim.upstream.streams["/mcp"] = probe.endless
        sock = socket.socket()
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
        sock.settimeout(5)
        sock.connect(("127.0.0.1", shim.port))
        sock.sendall(POST_STREAM)
        assert b"200" in _read_until(sock, b"\r\n\r\n")
        time.sleep(1.5)
        assert 0 < probe.snapshot_sent() < 64 * 1024 * 1024
        sock.close()
        assert _until(lambda: probe.open == 0)


def test_a_client_that_keeps_reading_still_gets_every_byte_in_order(tmp_path: Path) -> None:
    pieces = [f"data: {n}\n\n".encode() for n in range(200)]

    def stream(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        for piece in pieces:
            handler.wfile.write(f"{len(piece):x}\r\n".encode() + piece + b"\r\n")
        handler.wfile.write(b"0\r\n\r\n")

    with running_shim(tmp_path) as shim:
        shim.upstream.streams["/mcp"] = stream
        status, _, body = shim.request("GET", "/mcp", headers={"Authorization": "Bearer t"})
    assert status == 200
    assert body == b"".join(pieces)

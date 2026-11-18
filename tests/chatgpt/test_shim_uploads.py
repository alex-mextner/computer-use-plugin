"""Request bodies through the shim: an unauthenticated upload must not make the public entry point hold memory (#29).

The shim used to read every body whole (up to 32 MiB) before mcp-auth-proxy could reject it, with one thread per connection and no
limit: 64 uploads that hold back their last bytes kept about 2 GiB. Bodies now stream to the upstream piece by piece and the
number of connections served at once is capped (more get a 503). Review regressions: a 401 the proxy sends before the body is
through must still reach the client at once (not a 502, and not only after a paused client sends more), and a refusal's journal
line on a gone stderr must not stop the accept loop.
"""

from __future__ import annotations

import http.client
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from computerlib.targets.chatgpt.shim import build_server, proxy
from computerlib.targets.chatgpt.shim.proxy import MAX_REQUEST_BYTES, ShimServer
from tests.chatgpt.shim_harness import EXTERNAL, running_shim

UPLOADS = 64
SENT_EACH = 4 * 1024 * 1024  # what each upload sends before it stalls; whole-body buffering would hold 64 x this = 256 MiB
RSS_GROWTH_BOUND = 64 * 1024 * 1024  # the shim's peak resident memory over its idle baseline while all 64 are in flight
PIECE = b"x" * (256 * 1024)

SHIM = """
import sys
from pathlib import Path
from computerlib.targets.chatgpt.shim import build_server
server = build_server(sys.argv[1], 0, int(sys.argv[2]), Path(sys.argv[3]))
print(server.server_address[1], flush=True)
server.serve_forever()
"""


class Sink:
    """An upstream that reads request bodies as they come and keeps only their byte count, so the test process measures nothing
    of its own."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.received = 0
        self.reading = 0

    def handler(self) -> type[BaseHTTPRequestHandler]:
        sink = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                remaining = int(self.headers.get("Content-Length") or 0)
                with sink.lock:
                    sink.reading += 1
                try:
                    while remaining and (piece := self.rfile.read1(min(remaining, 65536))):
                        remaining -= len(piece)
                        with sink.lock:
                            sink.received += len(piece)
                except OSError:
                    return
                finally:
                    with sink.lock:
                        sink.reading -= 1
                self.send_response(401)
                self.send_header("Content-Length", "0")
                self.end_headers()

        return Handler

    def counts(self) -> tuple[int, int]:
        with self.lock:
            return self.received, self.reading


def _until(condition: Callable[[], bool], seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.05)
    return condition()


def _status_kib(pid: int, field: str) -> int:
    for line in Path(f"/proc/{pid}/status").read_text().splitlines():
        if line.startswith(field + ":"):
            return int(line.split()[1]) * 1024
    raise AssertionError(f"{field} missing from /proc/{pid}/status")


@contextmanager
def shim_process(tmp_path: Path, upstream_port: int) -> Generator[tuple[subprocess.Popen[str], int]]:
    """The shim in a process of its own, so its resident memory is the shim's alone."""
    process = subprocess.Popen(
        [sys.executable, "-c", SHIM, EXTERNAL, str(upstream_port), str(tmp_path / "access.log")],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert process.stdout is not None
        port = int(process.stdout.readline())
        yield process, port
    finally:
        process.kill()
        process.wait(timeout=10)


def _upload_head(length: int) -> bytes:
    return (f"POST /mcp HTTP/1.1\r\nHost: x\r\nContent-Type: application/json\r\nContent-Length: {length}\r\n\r\n").encode()


def _send(sock: socket.socket, total: int) -> None:
    left = total
    while left:
        part = PIECE[: min(left, len(PIECE))]
        sock.sendall(part)
        left -= len(part)


class _SinkServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 128  # the default 5 overflows when 64 uploads connect at once, and the overflow ends in resets


@pytest.mark.skipif(not sys.platform.startswith("linux"), reason="reads the shim's resident memory from /proc")
def test_sixty_four_stalled_unauthenticated_uploads_keep_the_shims_memory_bounded(tmp_path: Path) -> None:
    sink = Sink()
    upstream = _SinkServer(("127.0.0.1", 0), sink.handler())
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    clients: list[socket.socket] = []
    try:
        with shim_process(tmp_path, upstream.server_address[1]) as (process, port):
            warm = socket.create_connection(("127.0.0.1", port), timeout=10)  # a full upload first: lazy state is in the baseline
            warm.sendall(_upload_head(SENT_EACH))
            _send(warm, SENT_EACH)
            assert b" 401 " in warm.recv(4096)
            warm.close()
            baseline = _status_kib(process.pid, "VmRSS")
            for _ in range(UPLOADS):
                sock = socket.create_connection(("127.0.0.1", port), timeout=10)
                sock.sendall(_upload_head(MAX_REQUEST_BYTES))  # at the limit, so the shim accepts it
                clients.append(sock)
            for sock in clients:
                _send(sock, SENT_EACH)  # and then hold back the rest
            expected = SENT_EACH * (UPLOADS + 1)
            relayed = _until(lambda: sink.counts()[0] >= expected, 30)
            peak = _status_kib(process.pid, "VmHWM")
            received, reading = sink.counts()
            assert relayed, (
                f"the upstream got {received} of {expected} bytes: the shim holds the bodies instead of streaming them"
            )
            assert reading == UPLOADS, f"{reading} of {UPLOADS} uploads reached the upstream at once"
            assert peak - baseline < RSS_GROWTH_BOUND, (
                f"the shim's resident memory grew by {(peak - baseline) >> 20} MiB with {UPLOADS} stalled uploads "
                f"(bound {RSS_GROWTH_BOUND >> 20} MiB)"
            )
    finally:
        for sock in clients:
            sock.close()
        upstream.shutdown()
        upstream.server_close()


def _hold(port: int) -> socket.socket:
    """A connection whose upload has started and stalls: it keeps one handler busy."""
    sock = socket.create_connection(("127.0.0.1", port), timeout=5)
    sock.sendall(_upload_head(1024) + b"{")
    return sock


class _GoneStream:
    """stderr after the journal went away: every write is EPIPE."""

    def write(self, text: str) -> int:
        raise BrokenPipeError(32, "Broken pipe")

    def flush(self) -> None:
        raise BrokenPipeError(32, "Broken pipe")


def test_connections_over_the_handler_cap_get_a_503_and_a_freed_slot_serves_again(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ShimServer, "max_handlers", 2)
    monkeypatch.setattr(sys, "stderr", _GoneStream())  # the refusal's journal line must not take the accept loop down with it
    with running_shim(tmp_path) as shim:
        held = [_hold(shim.port), _hold(shim.port)]  # the accept loop takes connections in order: these two hold both slots
        refused = socket.create_connection(("127.0.0.1", shim.port), timeout=5)
        try:
            answer = refused.recv(4096)
        finally:
            refused.close()
        assert answer.startswith(b"HTTP/1.1 503 "), answer
        for sock in held:
            sock.close()

        def served() -> bool:
            try:
                return shim.request("GET", "/x")[0] == 404
            except OSError:
                return False

        assert _until(served, 5), "a slot freed by a client that left was never reused"


class _Rejecting(BaseHTTPRequestHandler):
    """mcp-auth-proxy on an unauthenticated upload: a 401 at once, the body never read, the connection closed."""

    protocol_version = "HTTP/1.1"

    def log_message(self, format: str, *args: object) -> None:
        pass

    def do_POST(self) -> None:
        self.send_response(401)
        self.send_header("Content-Length", "0")
        self.send_header("Connection", "close")
        self.end_headers()


class _RejectingSlowly(_Rejecting):
    """A 401 whose chunked body follows its headers only after a while: the shim must relay it whole, not take the paused
    upload's client for gone."""

    def do_POST(self) -> None:
        self.send_response(401)
        self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()
        time.sleep(0.8)
        self.wfile.write(b"6\r\ndenied\r\n0\r\n\r\n")


def _read_head(sock: socket.socket) -> bytes:
    data = b""
    while b"\r\n\r\n" not in data and (piece := sock.recv(4096)):
        data += piece
    return data


@contextmanager
def rejecting_shim(tmp_path: Path, handler: type[BaseHTTPRequestHandler] = _Rejecting) -> Generator[int]:
    """A shim in front of an upstream that rejects every upload at once; yields the shim's port."""
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    upstream.daemon_threads = True
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    shim = build_server(EXTERNAL, 0, upstream.server_address[1], tmp_path / "access.log")
    threading.Thread(target=shim.serve_forever, daemon=True).start()
    try:
        yield shim.server_address[1]
    finally:
        for server in (shim, upstream):
            server.shutdown()
            server.server_close()


def test_a_slow_early_answer_to_a_paused_upload_is_relayed_whole(tmp_path: Path) -> None:
    with (
        rejecting_shim(tmp_path, _RejectingSlowly) as port,
        socket.create_connection(("127.0.0.1", port), timeout=5) as sock,
    ):
        sock.sendall(_upload_head(100) + b"{")  # and then nothing more
        data = b""
        while not data.endswith(b"0\r\n\r\n") and (piece := sock.recv(4096)):
            data += piece
    assert data.startswith(b"HTTP/1.1 401 "), data
    assert b"6\r\ndenied\r\n0\r\n\r\n" in data, data


def test_many_requests_on_one_keep_alive_connection_leave_no_threads_behind(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # each response otherwise waits ~40 ms on Nagle (#51); a fast burst is what piles up threads a request leaves behind
    monkeypatch.setattr("computerlib.targets.chatgpt.shim.proxy._Handler.disable_nagle_algorithm", True)
    with running_shim(tmp_path) as shim:
        shim.upstream.routes["/mcp"] = (200, {"Content-Type": "application/json"}, b"{}")
        connection = http.client.HTTPConnection("127.0.0.1", shim.port, timeout=5)
        try:
            connection.request("POST", "/mcp", body=b"{}")
            connection.getresponse().read()
            threads = threading.active_count()
            for _ in range(200):
                connection.request("POST", "/mcp", body=b"{}")
                connection.getresponse().read()
            after = threading.active_count()
        finally:
            connection.close()
    assert after <= threads + 2, f"{after} threads after 200 requests on one connection, {threads} after the first"


def test_an_upload_the_upstream_rejects_early_gets_its_401_with_the_challenge(tmp_path: Path) -> None:
    total = 512 * 1024
    with rejecting_shim(tmp_path) as port:
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)

        def upload() -> None:
            try:
                sock.sendall(_upload_head(total))
                for _ in range(total // 65536):
                    sock.sendall(b"x" * 65536)
                    time.sleep(0.05)  # paced: the 401 is back long before the body is through
            except OSError:
                pass  # the shim closes once it has answered

        sender = threading.Thread(target=upload, daemon=True)
        sender.start()
        try:
            head = _read_head(sock)
        finally:
            sender.join(timeout=5)
            sock.close()
    assert head.startswith(b"HTTP/1.1 401 "), head
    assert b"WWW-Authenticate: Bearer resource_metadata=" in head
    assert b"Connection: close" in head


@pytest.mark.parametrize("sent", [b"", b"{"], ids=["headers-only", "one-byte"])
def test_a_paused_upload_the_upstream_rejects_gets_its_401_at_once_and_frees_its_slot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sent: bytes
) -> None:
    monkeypatch.setattr(ShimServer, "max_handlers", 1)
    with rejecting_shim(tmp_path) as port:
        paused = socket.create_connection(("127.0.0.1", port), timeout=3)  # far below the 65 s idle timeout
        try:
            paused.sendall(_upload_head(MAX_REQUEST_BYTES) + sent)  # and then nothing more
            head = _read_head(paused)
            assert head.startswith(b"HTTP/1.1 401 "), head
            assert b"WWW-Authenticate: Bearer resource_metadata=" in head

            def next_upload_answered() -> bool:
                try:
                    with socket.create_connection(("127.0.0.1", port), timeout=3) as sock:
                        sock.sendall(_upload_head(MAX_REQUEST_BYTES) + sent)
                        return _read_head(sock).startswith(b"HTTP/1.1 401 ")
                except OSError:
                    return False

            # the paused client is still connected: only the shim itself can have given the one slot back
            assert _until(next_upload_answered, 3), "the rejected upload kept the only handler slot"
        finally:
            paused.close()


def test_a_chunked_upload_reaches_the_upstream_whole(tmp_path: Path) -> None:
    body = b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}'
    chunks = [body[:10], body[10:30], body[30:]]
    framed = b"".join(f"{len(c):x}\r\n".encode() + c + b"\r\n" for c in chunks) + b"0\r\n\r\n"
    with running_shim(tmp_path) as shim:
        shim.upstream.routes["/mcp"] = (200, {"Content-Type": "application/json"}, b"{}")
        sock = socket.create_connection(("127.0.0.1", shim.port), timeout=5)
        try:
            sock.sendall(b"POST /mcp HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n" + framed)
            assert sock.recv(4096).startswith(b"HTTP/1.1 200 ")
        finally:
            sock.close()
    assert [seen.body for seen in shim.upstream.seen] == [body]


def test_a_chunked_upload_over_the_limit_gets_a_413_and_never_reaches_the_upstream_whole(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proxy, "MAX_REQUEST_BYTES", 100)
    with running_shim(tmp_path) as shim:
        sock = socket.create_connection(("127.0.0.1", shim.port), timeout=5)
        try:
            sock.sendall(b"POST /mcp HTTP/1.1\r\nHost: x\r\nTransfer-Encoding: chunked\r\n\r\n" + b"40\r\n" + b"a" * 64 + b"\r\n")
            sock.sendall(b"40\r\n" + b"b" * 64 + b"\r\n0\r\n\r\n")
            assert sock.recv(4096).startswith(b"HTTP/1.1 413 ")
        finally:
            sock.close()
        time.sleep(0.2)
        assert shim.upstream.seen == []


def test_an_upload_whose_client_leaves_midway_is_never_forwarded_as_complete(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        sock = socket.create_connection(("127.0.0.1", shim.port), timeout=5)
        sock.sendall(_upload_head(100) + b"{" * 40)
        time.sleep(0.2)
        sock.close()
        time.sleep(0.3)
        assert all(len(seen.body) < int(seen.headers["content-length"]) for seen in shim.upstream.seen)

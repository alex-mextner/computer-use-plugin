"""Minimal streaming HTTP reverse proxy that runs the shim rules (stand-in for the agent-tools proxy engine).

WHAT: listens on one loopback port, forwards every request to one upstream (mcp-auth-proxy), streams the response back without
buffering (MCP's Server-Sent Events must not be held), and calls `ShimRules` at the points they care about.
INVARIANTS: an unframed (streamed) response is relayed through a bounded buffer: a client that stops reading stalls the upstream
instead of growing memory, and a client that goes away (also while the stream is idle) makes the shim shut the upstream connection
and end the helper thread. Hop-by-hop headers (RFC 9110 7.6.1) are not forwarded in either direction; rules never delay or change
what is forwarded except the three documented changes (challenge header, protected-resource path, idle SSE comment); a logging
failure never touches a request; an upstream failure is a 502 and one journal line per minute.
NOTE: a generic engine belongs in agent-tools/lib (L2 work); this is the target-specific minimum until it ships, kept to the same
hook points so the swap is local to this file.
"""

from __future__ import annotations

import http.client
import queue
import select
import socket
import sys
import threading
import time
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from computerlib.targets.chatgpt.shim.rules import CAPTURE_MAX_BYTES, OPEN_COMMENT_SECONDS, Exchange, ShimRules

HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}
)
MAX_REQUEST_BYTES = 32 * 1024 * 1024
ERROR_REPORT_SECONDS = 60.0
IDLE_TIMEOUT_SECONDS = 65.0
STREAM_CHUNK = 64 * 1024
STREAM_BUFFER_PIECES = 8  # x STREAM_CHUNK: the most a stalled client can make the shim hold for one stream
STREAM_POLL_SECONDS = 0.25  # how often an idle stream checks that its client is still there


class _UpstreamConnection(http.client.HTTPConnection):
    """Keeps the connected socket: `HTTPConnection.sock` is dropped once a response says `Connection: close`, and a thread blocked
    reading the body can only be woken by shutting that socket down."""

    stream_socket: socket.socket | None = None

    def connect(self) -> None:
        super().connect()
        self.stream_socket = self.sock

    def interrupt(self) -> None:
        """Wake a thread blocked reading the body (it sees EOF). The caller closes the connection after that thread has ended."""
        if self.stream_socket is not None:
            try:
                self.stream_socket.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def end_to_end(headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The headers without hop-by-hop ones and without those the Connection header names: they describe one TCP hop."""
    named = {
        token.strip().lower()
        for name, value in headers
        if name.lower() == "connection"
        for token in value.split(",")
        if token.strip()
    }
    return [(n, v) for n, v in headers if n.lower() not in HOP_BY_HOP and n.lower() not in named]


class ShimServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128

    def __init__(self, listen: tuple[str, int], upstream: tuple[str, int], rules: ShimRules) -> None:
        super().__init__(listen, _Handler)
        self.upstream = upstream
        self.rules = rules
        self._last_report = 0.0
        self._errors_since_report = 0
        self._report_lock = threading.Lock()

    def report_upstream_error(self, reason: str) -> None:
        with self._report_lock:
            self._errors_since_report += 1
            now = time.monotonic()
            if now - self._last_report < ERROR_REPORT_SECONDS:
                return
            print(
                f"computer shim: upstream {self.upstream[0]}:{self.upstream[1]} failed ({reason}); "
                f"{self._errors_since_report} request(s) since the last report",
                file=sys.stderr,
                flush=True,
            )
            self._last_report = now
            self._errors_since_report = 0


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = IDLE_TIMEOUT_SECONDS
    server: ShimServer  # pyright: ignore[reportIncompatibleVariableOverride]

    def log_message(self, format: str, *args: object) -> None:
        """The access log is the shim's own; the stdlib's stderr line per request would leak paths with queries."""

    def _handle(self) -> None:
        started = time.monotonic()
        path = self.path.partition("?")[0]
        rules = self.server.rules
        request_headers = {name.lower(): value for name, value in self.headers.items()}
        kind = rules.peek_kind(self.command, path, request_headers)
        body = self._read_body()
        if body is None:
            return
        exchange = Exchange(self.command, path, 0, 0, request_headers)
        if kind == "mcp":
            exchange.request_body = body
        elif kind == "learn" and len(body) <= CAPTURE_MAX_BYTES:
            exchange.request_body = body  # a truncated copy could hide a second client_id: bigger bodies are not captured
        try:
            exchange.status = self._forward(path, body, kind == "learn", exchange)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            exchange.status = 499
            self.close_connection = True
        exchange.millis = round((time.monotonic() - started) * 1000)
        rules.record(exchange)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle

    def _read_body(self) -> bytes | None:
        """The request body, decoded from chunked framing if needed; None after answering 4xx for an unusable one."""
        encoding = self.headers.get("Transfer-Encoding", "").lower()
        length = self.headers.get("Content-Length", "")
        if "chunked" in encoding:
            return self._read_chunked()
        if not length:
            return b""
        if not length.isdigit() or int(length) > MAX_REQUEST_BYTES:
            self._reply_error(400 if not length.isdigit() else 413)
            return None
        return self.rfile.read(int(length))

    def _read_chunked(self) -> bytes | None:
        chunks: list[bytes] = []
        total = 0
        while True:
            size_line = self.rfile.readline(1024).split(b";", 1)[0].strip()
            try:
                size = int(size_line, 16)
            except ValueError:
                self._reply_error(400)
                return None
            if size == 0:
                while self.rfile.readline(1024).strip():  # trailers
                    pass
                return b"".join(chunks)
            total += size
            if total > MAX_REQUEST_BYTES:
                self._reply_error(413)
                return None
            chunks.append(self.rfile.read(size))
            self.rfile.readline(8)

    def _reply_error(self, status: int, text: bytes = b"") -> None:
        self.send_response_only(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(text)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(text)
        self.close_connection = True

    def _forward(self, path: str, body: bytes, capture_response: bool, exchange: Exchange) -> int:
        rules = self.server.rules
        headers = end_to_end(list(self.headers.items()))
        headers = [(n, v) for n, v in headers if n.lower() not in ("content-length", "expect")]
        host, port = self.server.upstream
        connection = _UpstreamConnection(host, port, timeout=None)
        try:
            connection.putrequest(self.command, rules.upstream_target(self.path), skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                connection.putheader(name, value)
            if body or self.command in ("POST", "PUT", "PATCH"):
                connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            self.server.report_upstream_error(type(error).__name__)
            self._reply_error(502, b"bad gateway")
            return 502
        try:
            return self._relay(path, response, connection, capture_response, exchange)
        finally:
            response.close()
            connection.close()

    def _relay(
        self,
        path: str,
        response: http.client.HTTPResponse,
        connection: _UpstreamConnection,
        capture_response: bool,
        exchange: Exchange,
    ) -> int:
        rules = self.server.rules
        upstream_headers = end_to_end(response.getheaders())
        out = rules.response_headers(path, response.status, upstream_headers)
        framed = any(n.lower() == "content-length" for n, _ in out)
        bodiless = self.command == "HEAD" or response.status in (204, 304) or response.status < 200
        chunked = not framed and not bodiless
        self.send_response_only(response.status, response.reason)
        for name, value in out:
            self.send_header(name, value)
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()
        self.wfile.flush()
        if bodiless:
            return response.status
        open_comment = rules.wants_open_comment(self.command, path, upstream_headers)
        captured = self._stream(response, connection, chunked, open_comment, capture_response)
        if capture_response:
            exchange.response_body = captured
        return response.status

    def _write_chunk(self, data: bytes, chunked: bool) -> None:
        if chunked:
            self.wfile.write(f"{len(data):x}\r\n".encode() + data + b"\r\n")
        else:
            self.wfile.write(data)

    def _stream(
        self,
        response: http.client.HTTPResponse,
        connection: _UpstreamConnection,
        chunked: bool,
        open_comment: bool,
        capture: bool,
    ) -> bytes | None:
        """Copy the response body out as it arrives; the body up to CAPTURE_MAX_BYTES when asked, None when it was bigger."""
        kept: list[bytes] = []
        size = 0
        pieces = self._pieces(response, connection, open_comment) if chunked or open_comment else self._direct_pieces(response)
        try:
            for piece in pieces:
                self._write_chunk(piece, chunked)
                size += len(piece)
                if capture and size <= CAPTURE_MAX_BYTES:
                    kept.append(piece)
        finally:
            pieces.close()
        if chunked:
            self.wfile.write(b"0\r\n\r\n")
        return b"".join(kept) if capture and size <= CAPTURE_MAX_BYTES else None

    @staticmethod
    def _direct_pieces(response: http.client.HTTPResponse) -> Generator[bytes]:
        """A response with a known length ends by itself and the client's write backpressure paces the read: no helper thread."""
        while piece := response.read1(STREAM_CHUNK):
            yield piece

    def _client_gone(self) -> bool:
        """True when the peer closed its end: the socket is readable and a peek returns EOF. Pipelined bytes are not a
        disconnect."""
        try:
            readable, _, _ = select.select([self.connection], [], [], 0)
            return bool(readable) and self.connection.recv(1, socket.MSG_PEEK) == b""
        except OSError:
            return True

    def _pieces(
        self, response: http.client.HTTPResponse, connection: _UpstreamConnection, open_comment: bool
    ) -> Generator[bytes]:
        """Upstream body pieces of a stream, read by a helper thread into a bounded queue.

        With `open_comment` one SSE comment is sent when the first piece takes longer than the rules say: Cloudflare's edge holds
        an event stream's headers until the first body byte, so a client that opens the standalone stream of a server with nothing
        to say yet would wait for ever. A stream that speaks first is never touched.
        The consumer polls the client while waiting, so a client that leaves an idle stream ends it: closing this generator (also
        on a write error) sets `cancelled`, which the helper checks between reads and while the queue is full, and shuts the
        upstream socket so a helper blocked in `read` wakes at once.
        """
        pending: queue.Queue[bytes] = queue.Queue(maxsize=STREAM_BUFFER_PIECES)
        cancelled = threading.Event()

        def offer(item: bytes) -> None:
            while not cancelled.is_set():
                try:
                    pending.put(item, timeout=STREAM_POLL_SECONDS)
                    return
                except queue.Full:
                    continue

        def pump() -> None:
            try:
                while not cancelled.is_set() and (piece := response.read1(STREAM_CHUNK)):
                    offer(piece)
            except (OSError, http.client.HTTPException, ValueError):
                pass
            offer(b"")

        helper = threading.Thread(target=pump, daemon=True)
        helper.start()
        opened = not open_comment
        try:
            while True:
                try:
                    piece = pending.get(timeout=STREAM_POLL_SECONDS if opened else OPEN_COMMENT_SECONDS)
                except queue.Empty:
                    if not opened:
                        opened = True
                        yield b": open\n\n"
                    elif self._client_gone():
                        return
                    continue
                if not piece:
                    return
                yield piece
        finally:
            cancelled.set()
            connection.interrupt()
            helper.join(timeout=STREAM_POLL_SECONDS * 4)

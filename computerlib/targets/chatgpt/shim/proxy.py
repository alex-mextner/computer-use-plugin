"""Minimal streaming HTTP reverse proxy that runs the shim rules (stand-in for the agent-tools proxy engine).

WHAT: listens on one loopback port, forwards every request to one upstream (mcp-auth-proxy), streams the request body there and
the response back without buffering (MCP's Server-Sent Events must not be held), and calls `ShimRules` at the points they care
about.
INVARIANTS: the shim is the public entry point and the proxy authenticates only once it has the request, so nothing a client sends
before that may grow memory: a request body is passed on piece by piece (one STREAM_CHUNK at a time, plus the copy of at most
PEEK_MAX_BYTES the rules ask for); an upstream answer that comes before the body is through is relayed at once, also while the
client pauses; a body the upstream holds its answer for is held to the rules' `body_limit` (a size cap, and a deadline for the
part the upstream waits for, answered 408), so pausing inside one keeps a handler only until that deadline; and at most
MAX_HANDLERS connections are served at once, more are answered 503 from the accept loop without a thread. An unframed (streamed)
response is relayed through a bounded buffer: a client that stops reading stalls the upstream instead of growing memory, and a
client that goes away (also while the stream is idle) makes the shim shut the upstream connection and end the helper thread.
Hop-by-hop headers (RFC 9110 7.6.1) are not forwarded in either direction; rules never delay or change what is forwarded except
the three documented changes (challenge header, protected-resource path, idle SSE comment), and refuse a body only by its size
and how long it takes (the body limit); a logging failure never touches a request; an upstream failure is a 502 and one journal
line per minute, as is a refused connection. A request with more than one Authorization header is answered 400 and never
forwarded, so the label can only name the credential the proxy checked.
NOTE: a generic engine belongs in agent-tools/lib (L2 work); this is the target-specific minimum until it ships, kept to the same
hook points so the swap is local to this file.
"""

from __future__ import annotations

import http.client
import itertools
import queue
import select
import socket
import sys
import threading
import time
from collections.abc import Generator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BufferedReader

from computerlib.targets.chatgpt.shim.rules import (
    CAPTURE_MAX_BYTES,
    OPEN_COMMENT_SECONDS,
    PEEK_MAX_BYTES,
    BodyLimit,
    Exchange,
    ShimRules,
)

HOP_BY_HOP = frozenset(
    {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te", "trailer", "transfer-encoding", "upgrade"}
)
MAX_REQUEST_BYTES = 32 * 1024 * 1024
ERROR_REPORT_SECONDS = 60.0
IDLE_TIMEOUT_SECONDS = 65.0
STREAM_CHUNK = 64 * 1024
STREAM_BUFFER_PIECES = 8  # x STREAM_CHUNK: the most a stalled client can make the shim hold for one stream
STREAM_POLL_SECONDS = 0.25  # how often an idle stream checks that its client is still there
MAX_HANDLERS = 128  # connections served at once; cloudflared keeps up to 100 idle ones to its origin by default
BUSY_RESPONSE = b"HTTP/1.1 503 Service Unavailable\r\nContent-Length: 0\r\nRetry-After: 1\r\nConnection: close\r\n\r\n"

Request = socket.socket | tuple[bytes, socket.socket]  # socketserver's request: a TCP server always gets the socket


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

    def send_body(self, body: _RequestBody) -> bool:
        """Send a request body; False when the upstream answered or closed before it had all of it. mcp-auth-proxy rejects an
        unauthenticated upload without reading it: sending stops there, so `getresponse` relays that 401 instead of a reset."""
        if body.length == 0:
            return True
        body.upstream = self.stream_socket
        try:
            for piece in itertools.chain(body.pieces(), [b""] if body.length is None else []):
                self.send(f"{len(piece):x}\r\n".encode() + piece + b"\r\n" if body.length is None else piece)
        except (_UpstreamAnswered, OSError):
            return False  # an OSError here is the upstream's: its answer, or the error, comes out of getresponse
        return True


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


class _UpstreamAnswered(Exception):
    """The upstream answered while the shim was still reading the client's body for it."""


class _BodyError(Exception):
    """The client's request body cannot be forwarded whole; `status` is what the shim answers (400 bad framing, 408 not whole by
    its deadline, 413 too big) or 499 (the client went away or stalled past the idle timeout)."""

    def __init__(self, status: int) -> None:
        super().__init__(status)
        self.status = status


class _RequestBody:
    """A request body read from the client as the upstream takes it, so the shim never holds it whole (#29). `length` is None for
    a chunked body, which is passed on chunked. Keeps a copy only while the body is at most `keep` bytes, for the rules. A
    chunked body may grow to `max_bytes`; with a `deadline` (time.monotonic), more than `timed_bytes` of it (or all of it) must
    have come by then."""

    def __init__(
        self,
        rfile: BufferedReader,
        client: socket.socket,
        length: int | None,
        keep: int,
        max_bytes: int,
        deadline: float | None,
        timed_bytes: int,
    ) -> None:
        self.length = length
        self.upstream: socket.socket | None = None  # while set, an answer from it ends the body (see `_wait`)
        self._rfile = rfile
        self._client = client
        self._keep = keep
        self._max_bytes = max_bytes
        self._deadline = deadline
        self._timed_bytes = timed_bytes
        self._kept: list[bytes] = []
        self._size = 0

    def pieces(self) -> Generator[bytes]:
        """The body in pieces of at most STREAM_CHUNK; raises `_BodyError` when it cannot be forwarded whole and
        `_UpstreamAnswered` when the upstream answers first. A failed read of the client becomes `_BodyError(499)`, so an OSError
        out of the upstream connection is always the upstream's."""
        try:
            yield from self._read(self.length) if self.length is not None else self._chunked()
        except OSError as error:
            raise _BodyError(499) from error

    def kept(self) -> bytes | None:
        """The body when it was at most `keep` bytes, else None: a truncated copy could hide a second client_id."""
        return b"".join(self._kept) if self._size <= self._keep else None

    def _wait(self) -> None:
        """Return when the next read of the client cannot block. A client that pauses must not keep an early upstream answer
        (the 401 to an unauthenticated upload) waiting until the idle timeout, holding a handler slot: the upstream is polled
        together with the client, and its answer raises `_UpstreamAnswered`. Past the deadline, while the upstream still waits
        for the timed part, it raises `_BodyError(408)`, also for a client that keeps trickling bytes, which an idle timeout
        never catches (#69)."""
        if self.upstream is None:
            return
        deadline = self._deadline if self._size <= self._timed_bytes else None
        buffered = self._buffered()
        timeout, by_deadline = (0.0, False) if buffered else self._client_timeout(deadline)
        poller = select.poll()  # not select(): a busy shim may hold descriptors past FD_SETSIZE
        poller.register(self._client, select.POLLIN)
        poller.register(self.upstream, select.POLLIN)
        ready = {fd for fd, _ in poller.poll(None if timeout is None else timeout * 1000)}
        if self.upstream.fileno() in ready:
            raise _UpstreamAnswered
        if deadline is not None and time.monotonic() >= deadline:
            raise _BodyError(408)
        if not buffered and not ready:
            # the deadline's timeout also when poll's millisecond rounding woke it just short of it; otherwise idle past the
            # handler's timeout, as a blocking read would have timed out
            raise _BodyError(408 if by_deadline else 499)

    def _client_timeout(self, deadline: float | None) -> tuple[float | None, bool]:
        """How long to wait for the client's next bytes: the handler's idle timeout, or what is left before `deadline` when that
        is sooner (then True)."""
        idle = self._client.gettimeout()
        if deadline is None:
            return idle, False
        left = max(0.0, deadline - time.monotonic())
        return (left, True) if idle is None or left <= idle else (idle, False)

    def _buffered(self) -> bool:
        """True when `rfile` already holds client bytes (the socket then says nothing about them). Peeks with the socket
        non-blocking, so an empty buffer costs one read that returns at once."""
        timeout = self._client.gettimeout()
        self._client.setblocking(False)
        try:
            return bool(self._rfile.peek(1))
        finally:
            self._client.settimeout(timeout)

    def _readline(self, limit: int) -> bytes:
        """A framing line (chunk size, chunk end, trailer) of at most `limit` bytes, taken only as far as the client has sent it:
        a client that pauses inside one must not keep the upstream's early answer waiting either (#56). `readline` would block
        for the rest of the line once its first byte is buffered."""
        line = b""
        while len(line) < limit and not line.endswith(b"\n"):
            self._wait()
            piece = self._line_piece(limit - len(line))
            if not piece:
                break  # EOF: the caller sees a short line
            line += piece
        return line

    def _line_piece(self, limit: int) -> bytes:
        """What `rfile` holds of the current line, up to `limit` bytes. Reads the socket at most once, which `_wait` made sure
        does not block, and takes nothing past the newline: the bytes after it are the next frame's."""
        available = self._rfile.peek(1)
        newline = available.find(b"\n", 0, limit)
        return self._rfile.read(newline + 1 if newline >= 0 else min(limit, len(available)))

    def _read(self, count: int) -> Generator[bytes]:
        while count:
            self._wait()
            piece = self._rfile.read1(min(count, STREAM_CHUNK))
            if not piece:
                raise _BodyError(499)
            count -= len(piece)
            self._size += len(piece)
            if self._size <= self._keep:
                self._kept.append(piece)
            yield piece

    def _chunked(self) -> Generator[bytes]:
        while True:
            line = self._readline(1024)
            if not line:
                raise _BodyError(499)
            try:
                size = int(line.split(b";", 1)[0].strip(), 16)
            except ValueError:
                raise _BodyError(400) from None
            if size < 0:
                raise _BodyError(400)
            if size == 0:
                while self._readline(1024).strip():  # trailers
                    pass
                return
            if self._size + size > self._max_bytes:
                raise _BodyError(413)
            yield from self._read(size)
            self._readline(8)


class _ThrottledReport:
    """One journal line per ERROR_REPORT_SECONDS for an event that can repeat at network speed, with its count since the last."""

    def __init__(self, unit: str) -> None:
        self._unit = unit
        self._last = 0.0
        self._count = 0
        self._lock = threading.Lock()

    def note(self, message: str) -> None:
        with self._lock:
            self._count += 1
            now = time.monotonic()
            if now - self._last < ERROR_REPORT_SECONDS:
                return
            try:
                print(f"{message}; {self._count} {self._unit} since the last report", file=sys.stderr, flush=True)
            except (OSError, ValueError):
                pass  # a lost journal line must not end the request or the accept loop that reports it (stderr gone: EPIPE)
            self._last = now
            self._count = 0


class ShimServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True
    request_queue_size = 128
    max_handlers = MAX_HANDLERS

    def __init__(self, listen: tuple[str, int], upstream: tuple[str, int], rules: ShimRules) -> None:
        super().__init__(listen, _Handler)
        self.upstream = upstream
        self.rules = rules
        self._slots = threading.BoundedSemaphore(self.max_handlers)
        self._upstream_errors = _ThrottledReport("request(s)")
        self._refusals = _ThrottledReport("connection(s)")

    def report_upstream_error(self, reason: str) -> None:
        self._upstream_errors.note(f"computer shim: upstream {self.upstream[0]}:{self.upstream[1]} failed ({reason})")

    def process_request(self, request: Request, client_address: object) -> None:
        """A connection over the cap gets a 503 from the accept loop and never a thread, so threads (and what each holds) are
        bounded however many connections a stranger opens."""
        if not self._slots.acquire(blocking=False):
            self._refuse(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._slots.release()
            raise

    def process_request_thread(self, request: Request, client_address: object) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def _refuse(self, request: Request) -> None:
        if isinstance(request, socket.socket):
            try:
                request.setblocking(False)  # the accept loop must never wait on a client: a full send buffer drops the answer
                request.send(BUSY_RESPONSE)
            except OSError:
                pass
        self.shutdown_request(request)
        self._refusals.note(f"computer shim: {self.max_handlers} connections busy, refused one with 503")


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = IDLE_TIMEOUT_SECONDS
    # Headers and body go out in separate writes: with Nagle on, the body waits for the client's delayed ACK (~40 ms a response).
    disable_nagle_algorithm = True
    server: ShimServer  # pyright: ignore[reportIncompatibleVariableOverride]
    # rbufsize is -1, so the stream handler's makefile gives a BufferedReader: `_RequestBody` peeks into its buffer.
    rfile: BufferedReader  # pyright: ignore[reportIncompatibleVariableOverride]

    def log_message(self, format: str, *args: object) -> None:
        """The access log is the shim's own; the stdlib's stderr line per request would leak paths with queries."""

    def _handle(self) -> None:
        started = time.monotonic()
        path = self.path.partition("?")[0]
        rules = self.server.rules
        if len(self.headers.get_all("Authorization") or ()) > 1:
            # The proxy authenticates one of them (Go's Header.Get: the first) and the label would name the client of whichever
            # the rules read. Collapsing to the proxy's pick would tie the shim to that detail; no client sends two of a
            # singleton field (RFC 9110 11.6.2), so the request is refused before it is forwarded or logged.
            self._reply_error(400)
            return
        request_headers = {name.lower(): value for name, value in self.headers.items()}
        kind = rules.peek_kind(self.command, path, request_headers)
        keep = PEEK_MAX_BYTES if kind == "mcp" else CAPTURE_MAX_BYTES if kind == "learn" else 0
        body = self._request_body(rules.body_limit(path), keep)
        if body is None:
            return
        exchange = Exchange(self.command, path, 0, 0, request_headers)
        try:
            exchange.status = self._forward(path, body, kind == "learn", exchange)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            exchange.status = 499
            self.close_connection = True
        if kind:
            exchange.request_body = body.kept()
        exchange.millis = round((time.monotonic() - started) * 1000)
        rules.record(exchange)

    do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = do_OPTIONS = _handle

    def _request_body(self, limit: BodyLimit, keep: int) -> _RequestBody | None:
        """The request body to stream upstream; None after answering 4xx for one whose framing is unusable or declared too big."""
        max_bytes = MAX_REQUEST_BYTES if limit.max_bytes is None else limit.max_bytes
        declared = self.headers.get("Content-Length", "")
        length: int | None
        if "chunked" in self.headers.get("Transfer-Encoding", "").lower():
            length = None
        elif not declared:
            length = 0
        elif not declared.isdigit() or int(declared) > max_bytes:
            self._reply_error(400 if not declared.isdigit() else 413)
            return None
        else:
            length = int(declared)
        timed = length is None or length < limit.timed_bytes
        deadline = time.monotonic() + limit.seconds if timed else None
        return _RequestBody(self.rfile, self.connection, length, keep, max_bytes, deadline, limit.timed_bytes)

    def _reply_error(self, status: int, text: bytes = b"") -> None:
        self.send_response_only(status)
        self.send_header("Content-Type", "text/plain")
        self.send_header("Content-Length", str(len(text)))
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(text)
        self.close_connection = True

    def _forward(self, path: str, body: _RequestBody, capture_response: bool, exchange: Exchange) -> int:
        rules = self.server.rules
        headers = end_to_end(list(self.headers.items()))
        headers = [(n, v) for n, v in headers if n.lower() not in ("content-length", "expect")]
        host, port = self.server.upstream
        connection = _UpstreamConnection(host, port, timeout=None)
        try:
            connection.putrequest(self.command, rules.upstream_target(self.path), skip_host=True, skip_accept_encoding=True)
            for name, value in headers:
                connection.putheader(name, value)
            if body.length is None:
                connection.putheader("Transfer-Encoding", "chunked")
            elif body.length or self.command in ("POST", "PUT", "PATCH"):
                connection.putheader("Content-Length", str(body.length))
            connection.endheaders()
            whole = connection.send_body(body)
            response = connection.getresponse()
        except _BodyError as error:
            connection.close()  # the upstream got an incomplete request: closing it is how it learns that
            if error.status == 499:
                self.close_connection = True
            else:
                self._reply_error(error.status)
            return error.status
        except (OSError, http.client.HTTPException) as error:
            connection.close()
            self.server.report_upstream_error(type(error).__name__)
            self._reply_error(502, b"bad gateway")
            return 502
        if not whole:
            self.close_connection = True  # the rest of the client's body is still unread on this connection
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
        if self.close_connection:
            self.send_header("Connection", "close")  # a client must not send its next request on a connection the shim drops
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

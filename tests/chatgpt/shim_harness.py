"""A stub upstream and a running shim on loopback ports, for behavioural tests of the shim."""

from __future__ import annotations

import base64
import http.client
import json
import threading
import time
from collections.abc import Callable, Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import BufferedIOBase
from pathlib import Path

from computerlib.targets.chatgpt.shim import build_server

Route = tuple[int, dict[str, str], bytes]
EXTERNAL = "https://mcp.example.test"


@dataclass
class Seen:
    method: str
    target: str
    headers: dict[str, str]
    body: bytes


@dataclass
class Upstream:
    routes: dict[str, Route] = field(default_factory=lambda: dict[str, Route]())
    streams: dict[str, Callable[[BaseHTTPRequestHandler], None]] = field(
        default_factory=lambda: dict[str, Callable[[BaseHTTPRequestHandler], None]]()
    )
    seen: list[Seen] = field(default_factory=lambda: list[Seen]())


def _read_body(handler: BaseHTTPRequestHandler) -> bytes | None:
    """The request body as sent (chunked framing decoded); None when the connection ended inside chunked framing, so a body the
    shim cut short is never recorded as whole. A short Content-Length body is recorded short."""
    if "chunked" in handler.headers.get("Transfer-Encoding", "").lower():
        return _read_chunked(handler.rfile)
    length = int(handler.headers.get("Content-Length") or 0)
    return handler.rfile.read(length) if length else b""


def _read_chunked(rfile: BufferedIOBase) -> bytes | None:
    parts: list[bytes] = []
    while line := rfile.readline():
        size = int(line.split(b";")[0].strip(), 16)
        if size == 0:
            while (trailer := rfile.readline()).strip():
                pass
            return b"".join(parts) if trailer else None
        part = rfile.read(size)
        if len(part) < size or not rfile.readline():
            return None
        parts.append(part)
    return None


def _handler(upstream: Upstream) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            pass

        def _serve(self) -> None:
            body = _read_body(self)
            if body is None:
                return
            path = self.path.partition("?")[0]
            upstream.seen.append(Seen(self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
            if len(body) < int(self.headers.get("Content-Length") or 0):
                return  # the shim cut the request short: nobody is left to answer
            if path in upstream.streams:
                upstream.streams[path](self)
                return
            status, headers, payload = upstream.routes.get(path, (404, {}, b"not found"))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(payload)

        do_GET = do_POST = do_HEAD = _serve

    return Handler


@dataclass
class Running:
    upstream: Upstream
    port: int
    upstream_port: int
    log_file: Path

    def request(
        self, method: str, path: str, headers: dict[str, str] | None = None, body: bytes | None = None
    ) -> tuple[int, dict[str, str], bytes]:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            connection.request(method, path, body=body, headers=headers or {})
            response = connection.getresponse()
            return response.status, {k.lower(): v for k, v in response.getheaders()}, response.read()
        finally:
            connection.close()

    def log_lines(self, at_least: int = 1) -> list[str]:
        deadline = time.monotonic() + 3
        lines: list[str] = []
        while time.monotonic() < deadline:
            lines = self.log_file.read_text().splitlines() if self.log_file.exists() else []
            if len(lines) >= at_least:
                break
            time.sleep(0.02)
        return lines


@contextmanager
def running_shim(tmp: Path, log_file: Path | None = None) -> Generator[Running]:
    upstream = Upstream()
    stub = ThreadingHTTPServer(("127.0.0.1", 0), _handler(upstream))
    stub.daemon_threads = True
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    target = log_file or tmp / "state" / "access.log"
    shim = build_server(EXTERNAL, 0, stub.server_address[1], target)
    threading.Thread(target=shim.serve_forever, daemon=True).start()
    try:
        yield Running(upstream, shim.server_address[1], stub.server_address[1], target)
    finally:
        shim.shutdown()
        shim.server_close()
        stub.shutdown()
        stub.server_close()


def jwt(jti: str) -> str:
    def part(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part({'jti': jti, 'aud': EXTERNAL})}.sig"

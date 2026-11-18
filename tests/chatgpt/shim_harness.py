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


def _handler(upstream: Upstream) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            pass

        def _serve(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            path = self.path.partition("?")[0]
            upstream.seen.append(Seen(self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
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
def running_shim(tmp: Path, log_file: Path | None = None, oauth_db: Path | None = None) -> Generator[Running]:
    upstream = Upstream()
    stub = ThreadingHTTPServer(("127.0.0.1", 0), _handler(upstream))
    stub.daemon_threads = True
    threading.Thread(target=stub.serve_forever, daemon=True).start()
    target = log_file or tmp / "state" / "access.log"
    shim = build_server(EXTERNAL, 0, stub.server_address[1], target, oauth_db=oauth_db)
    threading.Thread(target=shim.serve_forever, daemon=True).start()
    try:
        yield Running(upstream, shim.server_address[1], stub.server_address[1], target)
    finally:
        shim.shutdown()
        shim.server_close()
        stub.shutdown()
        stub.server_close()


def jwt(jti: str, signature: str = "sig") -> str:
    def part(data: dict[str, object]) -> str:
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()

    return f"{part({'alg': 'none'})}.{part({'jti': jti, 'aud': EXTERNAL})}.{signature}"

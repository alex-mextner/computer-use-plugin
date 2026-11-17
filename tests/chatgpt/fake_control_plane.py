"""A stand-in for OpenAI's tunnel control plane (the wire protocol of tunnel-client's docs/protocol.md), for running the REAL
tunnel-client.

It serves the metadata, poll and response routes of one tunnel, hands out queued JSON-RPC commands and records every response. It
never talks to the internet and accepts only the bearer key the test gave it.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


@dataclass
class ControlPlane:
    tunnel_id: str
    key: str
    port: int = 0
    queued: list[dict[str, object]] = field(default_factory=lambda: list[dict[str, object]]())
    responses: list[dict[str, object]] = field(default_factory=lambda: list[dict[str, object]]())
    unauthorized: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def send(self, request_id: str, message: dict[str, object]) -> None:
        with self._lock:
            self.queued.append(
                {
                    "request_id": request_id,
                    "shard_token": f"shard-{request_id}",
                    "command_type": "jsonrpc",
                    "channel": "main",
                    "jsonrpc": message,
                }
            )

    def take(self) -> dict[str, object] | None:
        with self._lock:
            return self.queued.pop(0) if self.queued else None

    def response_to(self, request_id: str, wait: float = 15.0) -> dict[str, object] | None:
        deadline = time.monotonic() + wait
        while time.monotonic() < deadline:
            for response in list(self.responses):
                if response.get("request_id") == request_id:
                    return response
            time.sleep(0.05)
        return None


def _handler(plane: ControlPlane) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, format: str, *args: object) -> None:
            pass

        def _reply(self, status: int, body: bytes = b"") -> None:
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _authorized(self) -> bool:
            if self.headers.get("Authorization") == f"Bearer {plane.key}":
                return True
            plane.unauthorized += 1
            self._reply(401, b'{"error":"unauthorized"}')
            return False

        def do_GET(self) -> None:
            if not self._authorized():
                return
            path = self.path.partition("?")[0]
            if path.endswith("/poll"):
                command = plane.take()
                if command is None:
                    time.sleep(0.5)
                    self._reply(204)
                else:
                    self._reply(200, json.dumps({"commands": [command]}).encode())
            else:
                self._reply(200, json.dumps({"id": plane.tunnel_id}).encode())

        def do_POST(self) -> None:
            body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            if not self._authorized():
                return
            plane.responses.append(json.loads(body))
            self._reply(200, b"{}")

    return Handler


@contextmanager
def running_control_plane(tunnel_id: str, key: str) -> Generator[ControlPlane]:
    plane = ControlPlane(tunnel_id, key)
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(plane))
    server.daemon_threads = True
    plane.port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield plane
    finally:
        server.shutdown()
        server.server_close()

"""A stand-in for tunnel-client's loopback operator endpoints (/readyz, /health, /metrics) with the shapes of 0.0.17.

The shapes were captured from the real daemon (see test_tunnel_end_to_end.py for the opt-in run against the real binary). Tests
change the `Daemon` fields while it runs to walk the state machine through its states.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

METRIC = (
    'command_end_to_end_latency_milliseconds_count{channel="main",request_method="%s",'
    'tunnel_id="tunnel_x",tunnel_service_status="200"} %d'
)


@dataclass
class Daemon:
    ready: int = 200
    polling: bool = True
    failures: int = 0
    counts: dict[str, int] = field(default_factory=lambda: dict[str, int]())

    def health(self) -> dict[str, object]:
        state = "polling" if self.polling else "failing"
        return {
            "ready": self.ready == 200,
            "components": {
                "control-plane": {
                    "state": state,
                    "details": {
                        "last_success": "2026-11-08T23:40:20Z" if self.polling else "",
                        "consecutive_failures": self.failures,
                    },
                }
            },
        }

    def metrics(self) -> str:
        lines = ["# HELP command_end_to_end_latency_milliseconds latency"]
        lines += [METRIC % (method, n) for method, n in self.counts.items()]
        lines.append('command_end_to_end_latency_milliseconds_sum{request_method="tools/call"} 19')
        return "\n".join(lines) + "\n"


@contextmanager
def running_daemon(daemon: Daemon) -> Generator[int]:
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            path = self.path.partition("?")[0]
            if path == "/readyz":
                body, status = (b"ready" if daemon.ready == 200 else b"not ready"), daemon.ready
            elif path == "/health":
                body, status = json.dumps(daemon.health()).encode(), 200
            elif path == "/metrics":
                body, status = daemon.metrics().encode(), 200
            else:
                body, status = b"{}", 404
            self.send_response(status)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()

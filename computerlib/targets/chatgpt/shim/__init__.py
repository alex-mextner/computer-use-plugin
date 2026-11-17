"""The shim: a loopback reverse proxy in front of mcp-auth-proxy (see rules.py for what it adds and logs)."""

from __future__ import annotations

import signal
import threading
from pathlib import Path

from computerlib.targets.chatgpt.shim.accesslog import RotatingLog
from computerlib.targets.chatgpt.shim.clients import ClientTable
from computerlib.targets.chatgpt.shim.proxy import ShimServer
from computerlib.targets.chatgpt.shim.rules import ShimRules

LOG_MAX_BYTES = 2048 * 1024
LOG_KEEP = 3


def build_server(
    external_url: str, listen_port: int, upstream_port: int, log_file: Path | None, upstream_host: str = "127.0.0.1"
) -> ShimServer:
    log = RotatingLog(log_file, LOG_MAX_BYTES, LOG_KEEP) if log_file else None
    clients = ClientTable(log_file.parent / "clients.json" if log_file else None)
    rules = ShimRules(external_url, log, clients)
    return ShimServer(("127.0.0.1", listen_port), (upstream_host, upstream_port), rules)


def serve_until_signalled(server: ShimServer) -> None:
    """Serve on this thread until SIGTERM/SIGINT, then stop cleanly (systemd stop must not hang on a stuck request)."""
    stop = threading.Event()

    def request_stop(*_: object) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    stop.wait()
    server.shutdown()
    server.server_close()

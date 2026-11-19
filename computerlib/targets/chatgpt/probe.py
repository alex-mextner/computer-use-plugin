"""HTTP probes of the stack: any HTTP answer from a local process proves it is alive; the public URL is probed for the 401
challenge."""

from __future__ import annotations

import contextlib
import http.client
import socket
import ssl
import threading
from dataclasses import dataclass
from urllib.parse import urlsplit

PRM_PATH = "/.well-known/oauth-protected-resource"


@dataclass(frozen=True)
class Answer:
    status: int
    www_authenticate: str


def request(url: str, method: str = "GET", body: bytes | None = None, timeout: float = 5.0) -> Answer | None:
    """One request; None when nothing answered within `timeout` (refused, timed out, TLS or DNS failure).

    Once connected, `timeout` is one deadline for the request, headers and body, not a per-read limit: a socket timeout restarts
    on every byte, so a server that trickles its headers or never ends a chunked body would hold the caller (and heal's lock)
    for as long as it keeps sending. Connecting (TCP, the TLS handshake) is bounded per step by the socket timeout and is not
    charged to the deadline, so a slow resolver or a lost SYN does not fail a server that then answers at once; the DNS lookup
    itself has no timeout (getaddrinfo takes none)."""
    parts = urlsplit(url)
    host = parts.hostname or ""
    expired = threading.Event()
    try:
        if parts.scheme == "https":
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                host, parts.port or 443, timeout=timeout, context=ssl.create_default_context()
            )
        else:
            connection = http.client.HTTPConnection(host, parts.port or 80, timeout=timeout)
        try:
            connection.connect()
            sock = connection.sock

            def expire() -> None:
                expired.set()
                # shutdown wakes the recv blocked in the probing thread; close from another thread would not. The plain socket
                # method, because SSLSocket.shutdown unwraps the TLS state under the reader, which then raises outside
                # the errors caught below.
                with contextlib.suppress(OSError):
                    socket.socket.shutdown(sock, socket.SHUT_RDWR)

            timer = threading.Timer(timeout, expire)
            timer.daemon = True
            timer.start()
            try:
                target = parts.path or "/"
                connection.request(
                    method,
                    target + (f"?{parts.query}" if parts.query else ""),
                    body=body,
                    headers={"Content-Type": "application/json"} if body else {},
                )
                response = connection.getresponse()
                response.read(1024)
            finally:
                timer.cancel()
            if expired.is_set():
                return None
            return Answer(response.status, response.getheader("WWW-Authenticate") or "")
        finally:
            connection.close()
    except (TimeoutError, OSError, http.client.HTTPException, ssl.SSLError):
        return None


def local_alive(port: int, timeout: float = 5.0) -> bool:
    return request(f"http://127.0.0.1:{port}{PRM_PATH}", timeout=timeout) is not None


def challenge_ok(base_url: str, timeout: float = 8.0) -> tuple[bool, str]:
    """POST /mcp without credentials must be 401 with a Bearer resource_metadata challenge (what ChatGPT's OAuth discovery
    needs)."""
    answer = request(f"{base_url}/mcp", "POST", b"{}", timeout)
    if answer is None:
        return False, "no answer"
    if answer.status != 401:
        return False, f"HTTP {answer.status}, expected 401"
    if "resource_metadata=" not in answer.www_authenticate:
        return False, "401 without a WWW-Authenticate resource_metadata challenge"
    return True, "401 with the protected-resource challenge"

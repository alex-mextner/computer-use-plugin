"""The shim's rules: what the chatgpt target needs on top of a plain reverse proxy.

Engine-independent: this module decides, the proxy engine (`proxy.py`) moves bytes. Two jobs:
A. what ChatGPT's connector setup needs and mcp-auth-proxy lacks (RFC 9728 protected-resource discovery, as the MCP authorization
   spec asks):
   1. every 401 that carries no challenge gets
        WWW-Authenticate: Bearer resource_metadata="<external>/.well-known/oauth-protected-resource"
      except under /.auth/ and /.idp/ (the proxy's login pages answer 401 for a wrong password; a Bearer challenge there would
      confuse the browser flow);
   2. /.well-known/oauth-protected-resource/<anything> (the path-suffix form of RFC 9728) is rewritten to the root form the proxy
      serves.
   3. Cloudflare's edge holds an event stream's response headers until the first body byte, so the idle standalone stream (GET
      /mcp) gets an SSE comment `: open` after OPEN_COMMENT_SECONDS of upstream silence (the engine asks `wants_open_comment`).
B. the access log (see accesslog.py), including the verified client label (see clients.py).
Which requests are logged: every OAuth step (/.idp/*, /.auth/*), every /mcp call that carries an Authorization header, every 5xx.
Everything else (discovery polling, scanners, unauthenticated calls) is collapsed to one line per method+path+status per minute
with suppressed=<n>; /favicon.ico is never logged. For POST /mcp only the JSON-RPC `method` and, for tools/call, the tool `name`
are read from the body, and only when Content-Length is at most PEEK_MAX_BYTES; a bigger or unframed body is logged as rpc=?.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass

from computerlib.targets.chatgpt.shim.accesslog import Collapser, Entry, RotatingLog, now_iso, safe_path
from computerlib.targets.chatgpt.shim.clients import ClientClass, ClientTable, label
from computerlib.targets.chatgpt.shim.jsonish import as_list, as_object

PRM = "/.well-known/oauth-protected-resource"
REGISTER_PATH = "/.idp/register"
TOKEN_PATH = "/.idp/token"
PEEK_MAX_BYTES = 64 * 1024
CAPTURE_MAX_BYTES = 16 * 1024
OPEN_COMMENT_SECONDS = 0.25
SAFE_TOKEN = re.compile(r"^[A-Za-z0-9_./:-]{1,64}$")
KNOWN_PATHS = {"/mcp", PRM, "/.well-known/oauth-authorization-server", "/.well-known/openid-configuration"}


def _token(value: object) -> str:
    return value if isinstance(value, str) and SAFE_TOKEN.match(value) else "?"


def _describe(item: object) -> tuple[str, str]:
    fields = as_object(item)
    method = _token(fields.get("method"))
    if method != "tools/call":
        return method, ""
    return method, _token(as_object(fields.get("params")).get("name"))


def describe_rpc(body: bytes) -> tuple[str, str]:
    """(rpc methods, tool names) of a JSON-RPC body, comma joined; `?` for anything that does not parse."""
    try:
        parsed: object = json.loads(body)
    except ValueError:
        return "?", ""
    listed = as_list(parsed)
    items = listed[:4] if listed else [parsed]
    described = [_describe(item) for item in items]
    return ",".join(m for m, _ in described), ",".join(t for _, t in described if t)


def _collapse_name(path: str) -> str:
    if path.startswith(PRM + "/"):
        return PRM
    return path if path in KNOWN_PATHS else "(other)"


@dataclass
class Exchange:
    """What the engine reports about a finished request. `request_body`/`response_body` are only filled when `peek` asked for
    them."""

    method: str
    path: str  # without query
    status: int
    millis: int
    request_headers: Mapping[str, str]  # lower-case names
    request_body: bytes | None = None
    response_body: bytes | None = None


class ShimRules:
    def __init__(self, external_url: str, log: RotatingLog | None, clients: ClientTable) -> None:
        self.external_url = external_url.rstrip("/")
        self._challenge = f'Bearer resource_metadata="{self.external_url}{PRM}"'
        self._log = log
        self._clients = clients
        self._collapser = Collapser()

    def upstream_target(self, target: str) -> str:
        """The request target to send upstream: the path-suffix form of the protected-resource metadata becomes the root form."""
        path, sep, query = target.partition("?")
        return PRM + sep + query if path.startswith(PRM + "/") else target

    def response_headers(self, path: str, status: int, headers: list[tuple[str, str]]) -> list[tuple[str, str]]:
        exempt = path.startswith(("/.auth/", "/.idp/"))
        if status == 401 and not exempt and not any(name.lower() == "www-authenticate" for name, _ in headers):
            return [*headers, ("WWW-Authenticate", self._challenge)]
        return headers

    def wants_open_comment(self, method: str, path: str, headers: list[tuple[str, str]]) -> bool:
        if method != "GET" or path != "/mcp":
            return False
        lowered = {name.lower(): value for name, value in headers}
        content_type = lowered.get("content-type", "").split(";")[0].strip().lower()
        return content_type == "text/event-stream" and "content-length" not in lowered and "content-encoding" not in lowered

    def peek_kind(self, method: str, path: str, request_headers: Mapping[str, str]) -> str:
        """What the engine must copy for `record`: `mcp` (small request body), `learn` (both bodies, capped) or empty."""
        if method != "POST":
            return ""
        if path in (REGISTER_PATH, TOKEN_PATH):
            return "learn"
        if path == "/mcp" and self._declared_small(request_headers):
            return "mcp"
        return ""

    @staticmethod
    def _declared_small(headers: Mapping[str, str]) -> bool:
        length = headers.get("content-length", "")
        return length.isdigit() and int(length) <= PEEK_MAX_BYTES

    def record(self, exchange: Exchange) -> None:
        """Log one finished request (or not) and teach the client table from register/token exchanges."""
        signal = self._learn(exchange)
        if self._log is None or exchange.path == "/favicon.ico":
            return
        suppressed = self._suppressed(exchange)
        if suppressed < 0:
            return
        rpc, tool = ("", "")
        if exchange.method == "POST" and exchange.path == "/mcp":
            rpc, tool = describe_rpc(exchange.request_body) if exchange.request_body is not None else ("?", "")
        headers = exchange.request_headers
        authorization = headers.get("authorization", "")
        if exchange.path == "/mcp":
            signal = self._clients.class_of_token(authorization, accepted=200 <= exchange.status < 400)
        entry = Entry(
            time=now_iso(),
            status=exchange.status,
            method=exchange.method,
            path=safe_path(exchange.path),
            millis=exchange.millis,
            client=label(headers.get("user-agent", ""), exchange.status, signal),
            rpc=rpc,
            tool=tool,
            suppressed=suppressed,
        )
        self._log.write(entry.render())

    def _learn(self, exchange: Exchange) -> ClientClass | None:
        ok = 200 <= exchange.status < 300 and exchange.response_body is not None
        if exchange.method != "POST" or not ok or exchange.response_body is None:
            return None
        if exchange.path == REGISTER_PATH:
            self._clients.learn_registration(exchange.response_body)
        elif exchange.path == TOKEN_PATH:
            return self._clients.learn_token(
                exchange.request_headers.get("authorization", ""),
                exchange.request_headers.get("content-type", ""),
                exchange.request_body,
                exchange.response_body,
            )
        return None

    def _suppressed(self, exchange: Exchange) -> int:
        oauth_step = exchange.path.startswith(("/.idp/", "/.auth/"))
        authenticated = exchange.path == "/mcp" and bool(exchange.request_headers.get("authorization"))
        if oauth_step or authenticated or exchange.status >= 500:
            return 0
        return self._collapser.check(f"{exchange.method} {_collapse_name(exchange.path)} {exchange.status}", time.monotonic())

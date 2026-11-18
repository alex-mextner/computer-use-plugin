"""A minimal MCP client with ChatGPT's OAuth flow (DCR, PKCE S256, password login, consent, token) for end-to-end proofs.

Stands in for the agent-tools smoke client until that library ships. Plain http.client: no dependencies.
"""

from __future__ import annotations

import base64
import hashlib
import http.client
import json
import secrets
import socket
import time
from dataclasses import dataclass, field
from typing import cast
from urllib.parse import parse_qs, urlencode, urlsplit

CHATGPT_REDIRECT = "https://chatgpt.com/connector/oauth/e2e-callback"
OPENAI_UA = "openai-mcp/1.0.0"
FORM = {"Content-Type": "application/x-www-form-urlencoded"}


@dataclass
class Reply:
    status: int
    headers: dict[str, str]
    body: bytes

    def json(self) -> dict[str, object]:
        parsed: object = json.loads(self.body)
        assert isinstance(parsed, dict)
        return cast("dict[str, object]", parsed)


@dataclass
class Driver:
    port: int
    user_agent: str = OPENAI_UA
    cookies: dict[str, str] = field(default_factory=lambda: dict[str, str]())
    token: str = ""

    def request(self, method: str, path: str, body: bytes | str | None = None, headers: dict[str, str] | None = None) -> Reply:
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        sent = {"User-Agent": self.user_agent, **(headers or {})}
        if self.cookies:
            sent["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        try:
            connection.request(method, path, body=body, headers=sent)
            response = connection.getresponse()
            data = response.read()
            for name, value in response.getheaders():
                if name.lower() == "set-cookie":
                    key, _, rest = value.split(";", 1)[0].partition("=")
                    self.cookies[key] = rest
            return Reply(response.status, {k.lower(): v for k, v in response.getheaders()}, data)
        finally:
            connection.close()

    def _register(self, redirect: str) -> str:
        registration = {
            "redirect_uris": [redirect],
            "client_name": "e2e",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
        }
        reply = self.request("POST", "/.idp/register", json.dumps(registration), {"Content-Type": "application/json"})
        assert reply.status == 201, reply.body
        return str(reply.json()["client_id"])

    def _follow_to_login(self, first: str) -> None:
        location = first
        while "/.auth/login" not in location:
            location = self.request("GET", location).headers["location"]
        self.request("GET", location)

    def authorize(self, password: str, redirect: str = CHATGPT_REDIRECT) -> str:
        """Register, log in and exchange the code like ChatGPT does; returns and keeps the access token."""
        client_id = self._register(redirect)
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": redirect,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "state": "state-e2e-12345",
                "scope": "",
            }
        )
        self._follow_to_login(self.request("GET", f"/.idp/auth?{query}").headers["location"])
        login = self.request("POST", "/.auth/login", urlencode({"password": password}), FORM)
        assert login.status in (302, 303), login.status
        done = self.request("POST", login.headers["location"], "", FORM)
        code = parse_qs(urlsplit(done.headers["location"]).query)["code"][0]
        exchange = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect,
                "client_id": client_id,
                "code_verifier": verifier,
            }
        )
        token = self.request("POST", "/.idp/token", exchange, FORM)
        assert token.status == 200, token.body
        self.token = str(token.json()["access_token"])
        return self.token

    def rpc(self, message: dict[str, object]) -> Reply:
        headers = {
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        return self.request("POST", "/mcp", json.dumps(message), headers)

    def open_event_stream(self) -> tuple[bytes, float]:
        """GET /mcp: the response head, and how long it took to arrive."""
        sock = socket.create_connection(("127.0.0.1", self.port), timeout=10)
        try:
            request = (
                f"GET /mcp HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {self.token}\r\nAccept: text/event-stream\r\n"
                f"User-Agent: {self.user_agent}\r\n\r\n"
            )
            sock.sendall(request.encode())
            started = time.monotonic()
            head = sock.recv(4096)
            return head, time.monotonic() - started
        finally:
            sock.close()

"""Verified client label for the access log (ticket #149 design of the dc-mcp shim, ported).

The User-Agent is whatever the caller sends, so it alone proves nothing. A log line gets its User-Agent kind (openai-mcp, ...)
only when ALL of these hold, otherwise `client=unverified`:
  1. the User-Agent names an OpenAI agent (product token openai* or chatgpt*);
  2. the request carries a credential the proxy accepted: POST /mcp with a Bearer token, or POST /.idp/token with an authorization
     code, and the upstream status is 2xx or 3xx (mcp-auth-proxy does ALL authentication; this shim authenticates nothing);
  3. that credential belongs to an OAuth client registered (POST /.idp/register) with only https redirect URIs under chatgpt.com
     or openai.com. For /mcp the client comes from the token: /.idp/token responses are watched and the `jti` claim of each issued
     access token (read by base64 decoding the JWT payload, never trusted: it is looked up in the table, a forged value matches
     nothing) is remembered with the client_id the token request named.
A token request names its client only as a public client (ChatGPT registers with token_endpoint_auth_method none): an
application/x-www-form-urlencoded body, no Authorization header, no leading `?`, no more than one `client_id`, and a body that was
captured whole. Anything else names NO client and the token is attributed to nobody: the shim does not guess how the proxy's own
parser (Go's url.ParseQuery) reads what it cannot read with certainty. PAST BUG: a body `?client_id=<openai>&client_id=<local>` is
`client_id=<openai>` for a URLSearchParams-style parser but `?client_id=<openai>` + `client_id=<local>` for Go's, so a token the
proxy issued to the local client could be credited to the OpenAI-registered one.
Trust assumption: the redirect URI a client registers is where the proxy sends the authorization code, and a code is only issued
after the owner's password login. So a token of a chatgpt.com-registered client was handed to chatgpt.com unless the password
holder scripted the flow. DCR is open to anyone, so the registration step itself is never verified, nor are unauthenticated lines.
Headers that reach the shim are no signal: Cloudflare overwrites cf-* at its edge, but a caller that skips the tunnel sets them
freely.
Remembered (clients.json, mode 0600, next to the access log): client_id -> class in two tables, jti -> client_id, oldest dropped
first. Registration is open to anyone, so the table of merely registered clients (MAX_CLIENTS) is evictable by a flood of
registrations; a client that completed a token exchange (which needs the owner's password login) moves to a second table
(MAX_EXCHANGED) that registrations cannot touch, so a flood never takes the classification of a connected ChatGPT away while its
tokens are valid. A flood between a client's registration and its first exchange (the length of one password login) still evicts
it.
No secrets: no redirect URIs, tokens or codes. A token issued before the table existed is `unverified` until its
client's next /.idp/token exchange (the token lifetime, about a day).
"""

from __future__ import annotations

import base64
import binascii
import json
import os
import re
import threading
from collections import OrderedDict
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlsplit

from computerlib.targets.chatgpt.shim.jsonish import as_list, as_object, parse_object

MAX_CLIENTS = 64
MAX_EXCHANGED = 64
MAX_TOKENS = 256
OPENAI_HOSTS = ("chatgpt.com", "openai.com")
SAFE_ID = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")
BEARER_JWT = re.compile(r"^Bearer ([\w-]+)\.([\w-]+)\.[\w-]+$", re.IGNORECASE)
BASIC = re.compile(r"^Basic ([A-Za-z0-9+/=]+)$")
UA_KIND = re.compile(r"^(openai|chatgpt)[a-z0-9._-]*$")

ClientClass = Literal["openai", "other"]
UNVERIFIED = "unverified"


def user_agent_kind(user_agent: str) -> str:
    """A short, non-identifying kind: the product token of an OpenAI agent as it is, else a family."""
    ua = user_agent.strip()
    product = re.split(r"[\s/]", ua, maxsplit=1)[0].lower() if ua else ""
    if UA_KIND.match(product):
        return product[:32]
    if product == "curl":
        return "curl"
    if re.search(r"mozilla|chrome|safari|firefox|edg", ua, re.IGNORECASE):
        return "browser"
    return "other"


def _is_openai_redirect(uri: object) -> bool:
    if not isinstance(uri, str):
        return False
    try:
        parts = urlsplit(uri)
    except ValueError:
        return False
    host = parts.hostname or ""
    return parts.scheme == "https" and any(host == h or host.endswith(f".{h}") for h in OPENAI_HOSTS)


def redirect_class(uris: object) -> ClientClass:
    listed = as_list(uris)
    return "openai" if 0 < len(listed) <= 8 and all(_is_openai_redirect(u) for u in listed) else "other"


def token_id(authorization: str) -> str:
    """The jti of a JWT access token, by decoding its payload. Not a verification: the signature is the proxy's business."""
    match = BEARER_JWT.match(authorization)
    if not match:
        return ""
    payload = match.group(2)
    try:
        claims = parse_object(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except (ValueError, binascii.Error):
        return ""
    jti = claims.get("jti")
    return jti if isinstance(jti, str) and SAFE_ID.match(jti) else ""


FORM_CONTENT_TYPE = re.compile(r"^application/x-www-form-urlencoded\s*(;|$)", re.IGNORECASE)


def token_request_client(authorization: str, content_type: str, body: bytes | None) -> str:
    """The client_id a token request names, for an unambiguous public-client request only; empty for anything else (see the module
    docs)."""
    if authorization or body is None or not FORM_CONTENT_TYPE.match(content_type.strip()):
        return ""
    text = body.decode("utf-8", "replace")
    if text.startswith("?"):
        return ""
    named = [value for key, value in parse_qsl(text, keep_blank_values=True, separator="&") if key == "client_id"]
    return named[0] if len(named) == 1 and SAFE_ID.match(named[0]) else ""


def _remember[V](table: OrderedDict[str, V], key: str, value: V, limit: int) -> None:
    table.pop(key, None)
    table[key] = value
    while len(table) > limit:
        table.popitem(last=False)


class ClientTable:
    def __init__(self, path: Path | None) -> None:
        self._path = path
        self._clients: OrderedDict[str, ClientClass] = OrderedDict()  # registered, not yet proven by a token exchange
        self._exchanged: OrderedDict[str, ClientClass] = OrderedDict()
        self._tokens: OrderedDict[str, str] = OrderedDict()
        self._lock = threading.Lock()
        self._load()

    def _load(self) -> None:
        if self._path is None:
            return
        try:
            saved = parse_object(self._path.read_bytes())
        except OSError:
            return
        for key, value in as_object(saved.get("clients")).items():
            if SAFE_ID.match(key) and value in ("openai", "other"):
                self._clients[key] = value
        for key, value in as_object(saved.get("exchanged")).items():
            if SAFE_ID.match(key) and value in ("openai", "other"):
                self._exchanged[key] = value
        for key, value in as_object(saved.get("tokens")).items():
            if SAFE_ID.match(key) and isinstance(value, str) and SAFE_ID.match(value):
                self._tokens[key] = value

    def _save(self) -> None:
        if self._path is None:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            temp = self._path.with_suffix(".tmp")
            fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as handle:
                json.dump({"clients": self._clients, "exchanged": self._exchanged, "tokens": self._tokens}, handle)
            temp.replace(self._path)
        except OSError:
            pass  # lines then read `unverified` after a restart; nothing else changes

    def learn_registration(self, response_body: bytes) -> None:
        answer = parse_object(response_body)
        client_id = answer.get("client_id")
        if isinstance(client_id, str) and SAFE_ID.match(client_id):
            with self._lock:
                _remember(self._clients, client_id, redirect_class(answer.get("redirect_uris")), MAX_CLIENTS)
                self._save()

    def _class_of_client(self, client_id: str) -> ClientClass | None:
        return self._exchanged.get(client_id) or self._clients.get(client_id)

    def learn_token(
        self, authorization: str, content_type: str, request_body: bytes | None, response_body: bytes
    ) -> ClientClass | None:
        """Remember which client the issued access token belongs to; returns that client's class for the token step itself.

        `request_body` is None when it was too big to capture whole: a truncated body cannot prove it names one client_id only.
        """
        client_id = token_request_client(authorization, content_type, request_body)
        access = parse_object(response_body).get("access_token")
        jti = token_id(f"Bearer {access}") if isinstance(access, str) else ""
        with self._lock:
            known = self._class_of_client(client_id) if client_id else None
            if client_id and jti:
                _remember(self._tokens, jti, client_id, MAX_TOKENS)
                if known is not None:
                    _remember(self._exchanged, client_id, known, MAX_EXCHANGED)
                    self._clients.pop(client_id, None)
                self._save()
            return known

    def class_of_token(self, authorization: str) -> ClientClass | None:
        with self._lock:
            client_id = self._tokens.get(token_id(authorization), "")
            return self._class_of_client(client_id) if client_id else None


def label(user_agent: str, status: int, signal: ClientClass | None) -> str:
    """The client= value: the User-Agent kind only when the second signal agrees, else `unverified`."""
    kind = user_agent_kind(user_agent)
    if not 200 <= status < 400 or not UA_KIND.match(kind):
        return UNVERIFIED
    return kind if signal == "openai" else UNVERIFIED

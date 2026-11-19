"""The verified client label: a User-Agent alone never earns it, and a token request that the proxy might read differently than
the shim names no client (regressions of the #149 review: `?client_id=` parsing, duplicates, truncated bodies, and the flood of
anonymous registrations; #60: duplicate Authorization headers)."""

from __future__ import annotations

import base64
import http.client
import json
from pathlib import Path
from urllib.parse import urlencode

import pytest

from computerlib.targets.chatgpt.shim.clients import MAX_CLIENTS, token_request_client
from tests.chatgpt.shim_harness import Running, jwt, running_shim

OPENAI_UA = "openai-mcp/1.0.0"
CHATGPT = "cid-chatgpt"
LOCAL = "cid-local"
FORM = "application/x-www-form-urlencoded"
REDIRECTS = {CHATGPT: "https://chatgpt.com/connector/oauth/abc123", LOCAL: "http://localhost:9/cb"}


def _json(payload: dict[str, object]) -> bytes:
    return json.dumps(payload).encode()


def _register(shim: Running, client_id: str, redirect: str, user_agent: str = OPENAI_UA) -> None:
    shim.upstream.routes["/.idp/register"] = (
        201,
        {"Content-Type": "application/json"},
        _json({"client_id": client_id, "redirect_uris": [redirect]}),
    )
    shim.request("POST", "/.idp/register", headers={"User-Agent": user_agent, "Content-Type": "application/json"}, body=_json({}))


def _token_step(
    shim: Running,
    jti: str,
    body: bytes,
    content_type: str = FORM,
    extra: dict[str, str] | None = None,
    user_agent: str = OPENAI_UA,
) -> str:
    """POST /.idp/token with the given request, answered with an access token `jti`; returns the token."""
    shim.upstream.routes["/.idp/token"] = (
        200,
        {"Content-Type": "application/json"},
        _json({"access_token": jwt(jti), "token_type": "Bearer"}),
    )
    shim.request(
        "POST", "/.idp/token", headers={"User-Agent": user_agent, "Content-Type": content_type, **(extra or {})}, body=body
    )
    return jwt(jti)


def _mcp_call(shim: Running, token: str, user_agent: str = OPENAI_UA) -> None:
    shim.upstream.routes["/mcp"] = (200, {"Content-Type": "application/json"}, b"{}")
    shim.request(
        "POST", "/mcp", headers={"Authorization": f"Bearer {token}", "User-Agent": user_agent}, body=b'{"method":"tools/list"}'
    )


def _labels(shim: Running, at_least: int) -> list[str]:
    return [next(part for part in line.split() if part.startswith("client=")) for line in shim.log_lines(at_least)]


def _both_clients(shim: Running) -> None:
    _register(shim, CHATGPT, REDIRECTS[CHATGPT])
    _register(shim, LOCAL, REDIRECTS[LOCAL])


def test_a_plain_public_client_exchange_is_attributed_and_labelled(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        _both_clients(shim)
        token = _token_step(
            shim, "jti-ok", urlencode({"grant_type": "authorization_code", "code": "c", "client_id": CHATGPT}).encode()
        )
        _mcp_call(shim, token)
        labels = _labels(shim, 4)
    assert labels == ["client=unverified", "client=unverified", "client=openai-mcp", "client=openai-mcp"]


AMBIGUOUS = [
    pytest.param(
        f"?client_id={CHATGPT}&client_id={LOCAL}&grant_type=authorization_code".encode(),
        FORM,
        {},
        id="leading-question-mark-then-duplicate",
    ),
    pytest.param(f"?client_id={CHATGPT}&grant_type=authorization_code".encode(), FORM, {}, id="leading-question-mark"),
    pytest.param(f"client_id={CHATGPT}&client_id={LOCAL}".encode(), FORM, {}, id="duplicate-openai-first"),
    pytest.param(f"client_id={LOCAL}&client_id={CHATGPT}".encode(), FORM, {}, id="duplicate-local-first"),
    pytest.param(f"client_id={CHATGPT}&client%5Fid={LOCAL}".encode(), FORM, {}, id="duplicate-through-an-escaped-key"),
    pytest.param(
        f"client_id={CHATGPT}&pad={'x' * 20000}&client_id={LOCAL}".encode(), FORM, {}, id="duplicate-beyond-the-captured-prefix"
    ),
    pytest.param(f"client_id={CHATGPT}&pad={'x' * 20000}".encode(), FORM, {}, id="body-too-big-to-capture-whole"),
    pytest.param(f"client_id={CHATGPT}".encode(), "application/json", {}, id="not-a-form"),
    pytest.param(f"client_id={CHATGPT}".encode(), "text/plain", {}, id="plain-text"),
    pytest.param(
        f"client_id={CHATGPT}".encode(),
        FORM,
        {"Authorization": f"Basic {base64.b64encode(f'{LOCAL}:'.encode()).decode()}"},
        id="basic-header",
    ),
    pytest.param(
        f"client_id={CHATGPT}".encode(),
        FORM,
        {"Authorization": f"Basic {base64.b64encode(f'{CHATGPT}:'.encode()).decode()}"},
        id="basic-header-agreeing",
    ),
]


@pytest.mark.parametrize(("body", "content_type", "extra"), AMBIGUOUS)
def test_a_token_request_the_proxy_might_read_differently_names_no_client(
    tmp_path: Path, body: bytes, content_type: str, extra: dict[str, str]
) -> None:
    with running_shim(tmp_path) as shim:
        _both_clients(shim)
        token = _token_step(shim, "jti-x", body, content_type, extra)
        _mcp_call(shim, token)
        labels = _labels(shim, 4)
    assert labels == ["client=unverified"] * 4


@pytest.mark.parametrize(
    ("authorization", "content_type", "body", "expected"),
    [
        ("", FORM, b"client_id=abc&grant_type=authorization_code", "abc"),
        ("", "application/x-www-form-urlencoded; charset=UTF-8", b"client_id=abc", "abc"),
        ("", FORM, b"client_id=a&client_id=b", ""),
        ("", FORM, b"client_id=a&client_id=a", ""),
        ("", FORM, b"?client_id=a", ""),
        ("", FORM, b"a=1&?client_id=a", ""),
        ("", FORM, b"client_id=a%20b", ""),
        ("", FORM, b"client_id=", ""),
        ("", FORM, b"", ""),
        ("", FORM, None, ""),
        ("Basic Zm9vOg==", FORM, b"client_id=a", ""),
        ("", "multipart/form-data; boundary=b", b"client_id=a", ""),
        ("", "", b"client_id=a", ""),
    ],
)
def test_token_request_client_reads_only_an_unambiguous_public_client_form(
    authorization: str, content_type: str, body: bytes | None, expected: str
) -> None:
    assert token_request_client(authorization, content_type, body) == expected


def test_a_forged_user_agent_from_curl_is_unverified_on_every_endpoint(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        _both_clients(shim)
        token = _token_step(shim, "jti-real", f"client_id={CHATGPT}".encode(), user_agent="curl/8.5")
        _mcp_call(shim, token, user_agent="curl/8.5")
        shim.request(
            "POST", "/mcp", headers={"Authorization": f"Bearer {jwt('never-issued')}", "User-Agent": OPENAI_UA}, body=b"{}"
        )
        labels = _labels(shim, 5)
    assert labels == ["client=unverified"] * 5


def test_a_forged_user_agent_with_a_foreign_registration_is_unverified(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        _both_clients(shim)
        token = _token_step(shim, "jti-local", f"client_id={LOCAL}".encode())
        _mcp_call(shim, token)
        labels = _labels(shim, 4)
    assert labels == ["client=unverified"] * 4


def test_a_rejected_call_never_gets_a_verified_label(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        _both_clients(shim)
        token = _token_step(shim, "jti-ok", f"client_id={CHATGPT}".encode())
        shim.upstream.routes["/mcp"] = (401, {}, b"no")
        shim.request("POST", "/mcp", headers={"Authorization": f"Bearer {token}", "User-Agent": OPENAI_UA}, body=b"{}")
        labels = _labels(shim, 4)
    assert labels == ["client=unverified", "client=unverified", "client=openai-mcp", "client=unverified"]


@pytest.mark.parametrize(
    "names_and_order",
    [
        pytest.param((("Authorization", "forged"), ("Authorization", "valid")), id="forged-first"),
        pytest.param((("Authorization", "valid"), ("Authorization", "forged")), id="forged-last"),
        pytest.param((("authorization", "forged"), ("AUTHORIZATION", "valid")), id="names-differing-in-case"),
    ],
)
def test_a_request_with_two_authorization_headers_is_refused_and_never_labelled(
    tmp_path: Path, names_and_order: tuple[tuple[str, str], ...]
) -> None:
    """The proxy authenticates one of the two (Go's Header.Get: the first) and the shim would label by another: a direct
    connection to the shim could then earn a forged token the label of a valid one. Authorization is a singleton field (RFC 9110
    11.6.2), so the shim refuses the request before it reaches the proxy or the access log."""
    with running_shim(tmp_path) as shim:
        _register(shim, CHATGPT, REDIRECTS[CHATGPT])
        tokens = {"valid": _token_step(shim, "jti-ok", f"client_id={CHATGPT}".encode()), "forged": jwt("jti-forged")}
        shim.upstream.routes["/mcp"] = (200, {"Content-Type": "application/json"}, b"{}")
        body = b'{"method":"tools/list"}'
        connection = http.client.HTTPConnection("127.0.0.1", shim.port, timeout=10)
        try:
            connection.putrequest("POST", "/mcp")
            for name, which in names_and_order:
                connection.putheader(name, f"Bearer {tokens[which]}")
            connection.putheader("User-Agent", OPENAI_UA)
            connection.putheader("Content-Length", str(len(body)))
            connection.endheaders(body)
            response = connection.getresponse()
            response.read()
        finally:
            connection.close()
        assert response.status == 400
        _mcp_call(shim, tokens["valid"])  # the next line in the log is this call's, so the refused one wrote none
        labels = _labels(shim, 3)
    assert labels == ["client=unverified", "client=openai-mcp", "client=openai-mcp"]
    assert [seen.target for seen in shim.upstream.seen].count("/mcp") == 1


def test_a_flood_of_anonymous_registrations_cannot_evict_a_connected_chatgpt(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        _register(shim, CHATGPT, REDIRECTS[CHATGPT])
        token = _token_step(shim, "jti-real", f"client_id={CHATGPT}".encode())
        for n in range(MAX_CLIENTS * 2):
            _register(shim, f"anon-{n}", "http://localhost:1/x", user_agent="curl/8")
        _mcp_call(shim, token)
        labels = _labels(shim, 3 + MAX_CLIENTS * 2)
    assert labels == ["client=unverified", "client=openai-mcp", *["client=unverified"] * (MAX_CLIENTS * 2), "client=openai-mcp"]


def test_the_exchanged_client_survives_a_flood_and_a_restart(tmp_path: Path) -> None:
    log_file = tmp_path / "state" / "access.log"
    with running_shim(tmp_path, log_file) as shim:
        _register(shim, CHATGPT, REDIRECTS[CHATGPT])
        token = _token_step(shim, "jti-real", f"client_id={CHATGPT}".encode())
        for n in range(MAX_CLIENTS + 5):
            _register(shim, f"anon-{n}", "http://localhost:1/x", user_agent="curl/8")
    saved = json.loads((tmp_path / "state" / "clients.json").read_text())
    assert saved["exchanged"] == {CHATGPT: "openai"}
    with running_shim(tmp_path, log_file) as shim:
        _mcp_call(shim, token)
        labels = _labels(shim, 2 + MAX_CLIENTS + 5 + 1)
    assert labels == ["client=unverified", "client=openai-mcp", *["client=unverified"] * (MAX_CLIENTS + 5), "client=openai-mcp"]


def test_a_client_that_only_registered_is_still_evictable_and_never_verified_by_a_flood(tmp_path: Path) -> None:
    """Registration is open to anyone, so a registered-only client is not trusted state worth protecting: the flood bound still
    holds."""
    with running_shim(tmp_path) as shim:
        _register(shim, CHATGPT, REDIRECTS[CHATGPT])
        for n in range(MAX_CLIENTS + 1):
            _register(shim, f"anon-{n}", "http://localhost:1/x", user_agent="curl/8")
        token = _token_step(shim, "jti-late", f"client_id={CHATGPT}".encode())
        _mcp_call(shim, token)
        labels = _labels(shim, 3 + MAX_CLIENTS + 1)
    assert labels == ["client=unverified"] * (3 + MAX_CLIENTS + 1)

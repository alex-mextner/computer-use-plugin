"""The shim's behaviour as a client sees it: challenge header, path rewrite, streaming, and the access log with its client
label."""

from __future__ import annotations

import json
import socket
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import urlencode

from tests.chatgpt.shim_harness import EXTERNAL, Running, jwt, running_shim

CHALLENGE = f'Bearer resource_metadata="{EXTERNAL}/.well-known/oauth-protected-resource"'
OPENAI_UA = "openai-mcp/1.0.0"
CHATGPT_REDIRECT = "https://chatgpt.com/connector/oauth/abc123"


def test_a_bare_401_gets_the_protected_resource_challenge(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        shim.upstream.routes["/mcp"] = (401, {"Content-Type": "application/json"}, b"{}")
        status, headers, body = shim.request("POST", "/mcp", body=b"{}")
    assert status == 401
    assert headers["www-authenticate"] == CHALLENGE
    assert body == b"{}"


def test_a_401_that_already_challenges_is_left_alone(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        shim.upstream.routes["/mcp"] = (401, {"WWW-Authenticate": 'Bearer realm="own"'}, b"")
        _, headers, _ = shim.request("POST", "/mcp", body=b"{}")
    assert headers["www-authenticate"] == 'Bearer realm="own"'


def test_the_proxys_login_pages_never_get_a_bearer_challenge(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        shim.upstream.routes["/.auth/login"] = (401, {}, b"wrong password")
        status, headers, _ = shim.request("POST", "/.auth/login", body=b"password=x")
    assert status == 401
    assert "www-authenticate" not in headers


def test_the_path_suffix_form_of_protected_resource_metadata_reaches_the_root_form(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        shim.upstream.routes["/.well-known/oauth-protected-resource"] = (
            200,
            {"Content-Type": "application/json"},
            b'{"resource":"x"}',
        )
        status, _, body = shim.request("GET", "/.well-known/oauth-protected-resource/mcp?x=1")
        assert shim.upstream.seen[-1].target == "/.well-known/oauth-protected-resource?x=1"
    assert (status, body) == (200, b'{"resource":"x"}')


def test_a_request_body_and_headers_arrive_unchanged(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        shim.upstream.routes["/mcp"] = (200, {"Content-Type": "application/json"}, b"{}")
        shim.request(
            "POST", "/mcp", headers={"Authorization": "Bearer t", "X-Custom": "1"}, body=b'{"jsonrpc":"2.0","method":"ping"}'
        )
        seen = shim.upstream.seen[-1]
    assert seen.body == b'{"jsonrpc":"2.0","method":"ping"}'
    assert seen.headers["x-custom"] == "1"
    assert seen.headers["authorization"] == "Bearer t"


def _event_stream(wait: float, first: bytes):
    def stream(handler: BaseHTTPRequestHandler) -> None:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Transfer-Encoding", "chunked")
        handler.end_headers()
        time.sleep(wait)
        handler.wfile.write(f"{len(first):x}\r\n".encode() + first + b"\r\n0\r\n\r\n")

    return stream


def test_an_idle_event_stream_gets_an_open_comment_before_the_first_event(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        shim.upstream.streams["/mcp"] = _event_stream(0.8, b"data: hi\n\n")
        _, _, body = shim.request("GET", "/mcp", headers={"Authorization": "Bearer t", "Accept": "text/event-stream"})
    assert body == b": open\n\ndata: hi\n\n"


def test_a_stream_that_speaks_first_is_not_touched(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        shim.upstream.streams["/mcp"] = _event_stream(0.0, b"data: hi\n\n")
        _, _, body = shim.request("GET", "/mcp", headers={"Authorization": "Bearer t"})
    assert body == b"data: hi\n\n"


def test_headers_of_an_idle_stream_arrive_before_its_first_byte(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        shim.upstream.streams["/mcp"] = _event_stream(1.5, b"data: late\n\n")
        sock = socket.create_connection(("127.0.0.1", shim.port), timeout=5)
        sock.sendall(b"GET /mcp HTTP/1.1\r\nHost: x\r\nConnection: close\r\nAuthorization: Bearer t\r\n\r\n")
        started = time.monotonic()
        head = sock.recv(4096)
        elapsed = time.monotonic() - started
        sock.close()
    assert head.startswith(b"HTTP/1.1 200")
    assert elapsed < 1.0


def test_an_unreachable_upstream_is_a_502_not_a_hang(tmp_path: Path) -> None:
    import threading

    from computerlib.targets.chatgpt.shim import build_server

    dead = socket.socket()
    dead.bind(("127.0.0.1", 0))
    dead_port = dead.getsockname()[1]
    dead.close()
    shim = build_server(EXTERNAL, 0, dead_port, tmp_path / "access.log")
    threading.Thread(target=shim.serve_forever, daemon=True).start()
    try:
        connection_port = shim.server_address[1]
        import http.client

        connection = http.client.HTTPConnection("127.0.0.1", connection_port, timeout=5)
        connection.request("GET", "/mcp")
        assert connection.getresponse().status == 502
    finally:
        shim.shutdown()
        shim.server_close()


def test_a_tool_call_is_logged_with_method_and_tool_but_never_arguments_or_query(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        shim.upstream.routes["/mcp"] = (200, {"Content-Type": "application/json"}, b"{}")
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "start_process", "arguments": {"command": "SECRET-ARG"}},
            }
        )
        shim.request(
            "POST",
            "/mcp?code=SECRET-QUERY",
            headers={"Authorization": "Bearer SECRET-TOKEN", "User-Agent": "curl/8"},
            body=body.encode(),
        )
        text = "\n".join(shim.log_lines())
    assert "POST /mcp " in text
    assert "rpc=tools/call" in text
    assert "tool=start_process" in text
    for secret in ("SECRET-ARG", "SECRET-QUERY", "SECRET-TOKEN", "curl/8"):
        assert secret not in text


def test_a_forged_openai_user_agent_from_curl_is_logged_unverified(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        shim.upstream.routes["/mcp"] = (200, {}, b"{}")
        shim.request(
            "POST",
            "/mcp",
            headers={"Authorization": f"Bearer {jwt('forged')}", "User-Agent": OPENAI_UA},
            body=b'{"method":"tools/list"}',
        )
        (line,) = shim.log_lines()
    assert "client=unverified" in line
    assert "openai-mcp" not in line


def _real_flow(shim: Running, redirect: str, client_id: str = "c1", jti: str = "jti-1") -> str:
    shim.upstream.routes["/.idp/register"] = (
        201,
        {"Content-Type": "application/json"},
        json.dumps({"client_id": client_id, "redirect_uris": [redirect]}).encode(),
    )
    shim.upstream.routes["/.idp/token"] = (
        200,
        {"Content-Type": "application/json"},
        json.dumps({"access_token": jwt(jti), "token_type": "Bearer"}).encode(),
    )
    shim.upstream.routes["/mcp"] = (200, {"Content-Type": "application/json"}, b"{}")
    shim.request(
        "POST",
        "/.idp/register",
        headers={"User-Agent": OPENAI_UA, "Content-Type": "application/json"},
        body=json.dumps({"redirect_uris": [redirect]}).encode(),
    )
    form = urlencode({"grant_type": "authorization_code", "code": "ory_ac_x", "client_id": client_id}).encode()
    shim.request(
        "POST", "/.idp/token", headers={"User-Agent": OPENAI_UA, "Content-Type": "application/x-www-form-urlencoded"}, body=form
    )
    shim.request(
        "POST",
        "/mcp",
        headers={"Authorization": f"Bearer {jwt(jti)}", "User-Agent": OPENAI_UA},
        body=b'{"method":"tools/call","params":{"name":"get_config"}}',
    )
    return "\n".join(shim.log_lines(at_least=3))


def test_real_chatgpt_traffic_keeps_its_client_label(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        text = _real_flow(shim, CHATGPT_REDIRECT)
    mcp = next(line for line in text.splitlines() if " /mcp " in line)
    token = next(line for line in text.splitlines() if "/.idp/token" in line)
    assert "client=openai-mcp" in mcp
    assert "client=openai-mcp" in token


def test_a_client_registered_with_a_foreign_redirect_never_gets_the_label(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        text = _real_flow(shim, "https://evil.example/cb")
    assert "openai-mcp" not in text


def test_the_label_survives_a_restart_of_the_shim(tmp_path: Path) -> None:
    log_file = tmp_path / "state" / "access.log"
    with running_shim(tmp_path, log_file) as shim:
        _real_flow(shim, CHATGPT_REDIRECT)
    with running_shim(tmp_path, log_file) as shim:
        shim.upstream.routes["/mcp"] = (200, {}, b"{}")
        shim.request(
            "POST",
            "/mcp",
            headers={"Authorization": f"Bearer {jwt('jti-1')}", "User-Agent": OPENAI_UA},
            body=b'{"method":"tools/list"}',
        )
        last = shim.log_lines(at_least=4)[-1]
    assert "client=openai-mcp" in last and "rpc=tools/list" in last


def test_unauthenticated_polling_is_collapsed_to_one_line_per_minute(tmp_path: Path) -> None:
    with running_shim(tmp_path) as shim:
        shim.upstream.routes["/.well-known/oauth-protected-resource"] = (200, {}, b"{}")
        for _ in range(5):
            shim.request("GET", "/.well-known/oauth-protected-resource")
        lines = shim.log_lines()
    assert len(lines) == 1


def test_a_log_that_cannot_be_written_never_breaks_requests(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the log directory should be")
    with running_shim(tmp_path, blocked / "access.log") as shim:
        shim.upstream.routes["/mcp"] = (200, {}, b"{}")
        status, _, _ = shim.request("POST", "/mcp", headers={"Authorization": "Bearer t"}, body=b"{}")
    assert status == 200

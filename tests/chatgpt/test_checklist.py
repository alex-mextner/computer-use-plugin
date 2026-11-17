"""The connect checklist as the access log drives it: what ticks a step, what only warns, and the only thing that means
connected."""

from __future__ import annotations

from computerlib.targets.chatgpt.accessview import parse_line
from computerlib.targets.chatgpt.checklist import STEPS, Checklist

T = "2026-11-08T10:00:00.000Z"


def feed(checklist: Checklist, *lines: str) -> list[str]:
    keys: list[str] = []
    for line in lines:
        record = parse_line(line)
        assert record is not None, line
        keys += [e.key for e in checklist.feed(record)]
    return keys


def test_the_whole_happy_path_ticks_every_step_and_ends_connected() -> None:
    checklist = Checklist()
    keys = feed(
        checklist,
        f"{T} 401 POST /mcp 1ms client=unverified",
        f"{T} 201 POST /.idp/register 1ms client=unverified",
        f"{T} 302 POST /.auth/login 40ms client=unverified",
        f"{T} 200 POST /.idp/token 2ms client=openai-mcp",
        f"{T} 200 POST /mcp 1ms client=openai-mcp rpc=initialize",
        f"{T} 200 POST /mcp 1ms client=openai-mcp rpc=tools/list",
        f"{T} 200 POST /mcp 9ms client=openai-mcp rpc=tools/call tool=get_config",
    )
    assert keys == [key for key, _ in STEPS]
    assert checklist.connected


def test_nothing_but_a_verified_tool_call_counts_as_connected() -> None:
    checklist = Checklist()
    feed(
        checklist,
        f"{T} 200 POST /.idp/token 2ms client=openai-mcp",
        f"{T} 200 POST /mcp 1ms client=openai-mcp rpc=initialize",
        f"{T} 200 POST /mcp 1ms client=openai-mcp rpc=tools/list",
    )
    assert not checklist.connected


def test_a_tool_call_from_an_unverified_client_does_not_count_and_says_why() -> None:
    checklist = Checklist()
    events = [
        e
        for line in (f"{T} 200 POST /mcp 1ms client=unverified rpc=tools/call tool=start_process",)
        for e in checklist.feed(parse_line(line))  # pyright: ignore[reportArgumentType]
    ]
    assert not checklist.connected
    assert events and events[0].warning and "unverified" in events[0].text


def test_a_wrong_password_is_called_out_once() -> None:
    checklist = Checklist()
    keys = feed(
        checklist, f"{T} 401 POST /.auth/login 40ms client=unverified", f"{T} 401 POST /.auth/login 40ms client=unverified"
    )
    assert keys == ["login-failed"]
    assert "login" not in checklist.done


def test_a_refused_registration_is_a_warning_not_a_step() -> None:
    checklist = Checklist()
    assert feed(checklist, f"{T} 400 POST /.idp/register 1ms client=unverified") == ["registration-failed"]
    assert "registration" not in checklist.done


def test_a_repeated_step_is_reported_once() -> None:
    checklist = Checklist()
    assert feed(checklist, f"{T} 200 GET /.well-known/oauth-protected-resource 1ms client=unverified") == ["discovery"]
    assert feed(checklist, f"{T} 200 GET /.well-known/oauth-protected-resource 1ms client=unverified") == []


def test_a_refused_login_of_any_4xx_kind_is_a_warning() -> None:
    checklist = Checklist()
    assert feed(checklist, f"{T} 400 POST /.auth/login 40ms client=unverified") == ["login-failed"]

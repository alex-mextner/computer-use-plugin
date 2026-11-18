"""Machine identity: the system hostname as a DNS label, the derived public hostname and how saved settings and flags resolve."""

from __future__ import annotations

import pytest
from agenttools_errors import UsageError

from computerlib.targets.chatgpt import machine
from computerlib.targets.chatgpt.machine import (
    MACHINE_MAX,
    default_machine_name,
    identify,
    public_hostname,
    require_machine_name,
    sanitize_label,
)
from computerlib.targets.chatgpt.settings import Settings


@pytest.mark.parametrize(
    ("raw", "label"),
    [
        ("ubuntu-pc", "ubuntu-pc"),
        ("Ubuntu_PC", "ubuntu-pc"),  # uppercase and underscores
        ("my__odd..name", "my-odd-name"),  # runs of anything else become one dash
        ("-edge-", "edge"),
        ("Ödroid N2", "droid-n2"),  # non-ASCII is not a DNS label character
        ("___", ""),
    ],
)
def test_sanitize_label(raw: str, label: str) -> None:
    assert sanitize_label(raw) == label


def test_a_long_name_leaves_room_for_the_suffix_in_one_63_character_label() -> None:
    label = sanitize_label("a" * 58 + "-b" * 10)
    assert len(label) <= MACHINE_MAX == 59 and not label.endswith("-")
    first = public_hostname(label, "example.com").split(".")[0]
    assert first == f"{label}-mcp" and len(first) <= 63


@pytest.mark.parametrize(("system", "name"), [("Ubuntu_PC", "ubuntu-pc"), ("odroid.lan", "odroid"), ("BOX.example.com", "box")])
def test_the_default_machine_name_is_the_short_system_hostname(monkeypatch: pytest.MonkeyPatch, system: str, name: str) -> None:
    monkeypatch.setattr(machine, "system_hostname", lambda: system)
    assert default_machine_name() == name


def test_a_system_hostname_without_letters_or_digits_asks_for_machine_name(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(machine, "system_hostname", lambda: "___")
    with pytest.raises(UsageError) as caught:
        default_machine_name()
    assert "--machine-name" in caught.value.fix


def test_the_fix_names_the_option_of_the_running_command(monkeypatch: pytest.MonkeyPatch) -> None:
    """`connect` and `login` have no --machine-name: their error points at --name and --tunnel-name."""
    monkeypatch.setattr(machine, "system_hostname", lambda: "___")
    for flag in ("--name", "--tunnel-name"):
        with pytest.raises(UsageError) as caught:
            default_machine_name(flag)
        assert flag in caught.value.fix and "--machine-name" not in caught.value.fix


def test_an_explicit_machine_name_must_already_be_a_label_and_the_error_suggests_one() -> None:
    assert require_machine_name("odroid-n2") == "odroid-n2"
    with pytest.raises(UsageError) as caught:
        require_machine_name("Odroid N2")
    assert "--machine-name odroid-n2" in caught.value.fix


def _identify(settings: Settings, **flags: str | None) -> Settings:
    passed: dict[str, str | None] = {"hostname": None, "domain": None, "machine_name": None, "tunnel_name": None}
    passed.update(flags)
    return identify(settings, cloudflare=True, **passed)


UUIDS = ["550e8400-e29b-41d4-a716-446655440000", "550e8400e29b41d4a716446655440000"]


@pytest.mark.parametrize("uuid", UUIDS)
def test_a_tunnel_named_after_a_uuid_machine_name_is_refused(monkeypatch: pytest.MonkeyPatch, uuid: str) -> None:
    """cloudflared refuses a tunnel name that reads as a UUID: refuse it before anything is written, naming the way out."""
    monkeypatch.setattr(machine, "system_hostname", lambda: uuid.upper())
    with pytest.raises(UsageError) as from_hostname:
        _identify(Settings(), domain="example.com")
    assert "--machine-name" in from_hostname.value.fix
    with pytest.raises(UsageError, match="cannot name the Cloudflare tunnel"):
        _identify(Settings(), domain="example.com", machine_name=uuid)
    assert _identify(Settings(), domain="example.com", machine_name=f"box-{uuid}").tunnel_name == f"box-{uuid}"


@pytest.mark.parametrize("uuid", UUIDS)
def test_a_uuid_host_keeps_setups_whose_tunnel_is_not_named_after_it(monkeypatch: pytest.MonkeyPatch, uuid: str) -> None:
    monkeypatch.setattr(machine, "system_hostname", lambda: uuid)
    legacy = Settings(hostname="computer.example.com", tunnel_name="computer-box")
    assert _identify(legacy).tunnel_name == "computer-box"
    assert _identify(Settings(), domain="example.com", tunnel_name="mine").tunnel_name == "mine"
    no_tunnel = identify(Settings(), hostname=None, domain=None, machine_name=None, tunnel_name=None, cloudflare=False)
    assert no_tunnel.tunnel_name == ""  # tunnel mode, or a hostname reached without Cloudflare


def test_domain_derives_the_hostname_and_the_tunnel_from_the_machine_name() -> None:
    resolved = _identify(Settings(), domain="Example.COM.")
    assert (resolved.hostname, resolved.domain, resolved.machine_name, resolved.tunnel_name) == (
        "test-box-mcp.example.com",
        "example.com",
        "test-box",
        "test-box",
    )


def test_a_saved_domain_and_machine_name_resolve_the_same_without_flags() -> None:
    first = _identify(Settings(), domain="example.com")
    assert _identify(first) == first


def test_a_saved_machine_name_wins_over_a_changed_system_hostname(monkeypatch: pytest.MonkeyPatch) -> None:
    first = _identify(Settings(), domain="example.com")
    monkeypatch.setattr(machine, "system_hostname", lambda: "renamed")
    assert _identify(first) == first


def test_machine_name_moves_the_hostname_and_the_tunnel() -> None:
    first = _identify(Settings(), domain="example.com")
    moved = _identify(first, machine_name="laptop")
    assert (moved.hostname, moved.tunnel_name) == ("laptop-mcp.example.com", "laptop")


def test_hostname_overrides_and_clears_the_domain() -> None:
    first = _identify(Settings(), domain="example.com")
    custom = _identify(first, hostname="dc.example.com")
    assert (custom.hostname, custom.domain, custom.tunnel_name) == ("dc.example.com", "", "test-box")
    assert _identify(custom).hostname == "dc.example.com"


def test_an_existing_hostname_only_config_keeps_its_hostname_and_tunnel() -> None:
    legacy = Settings(hostname="dc-mcp.example.com", tunnel_name="dc-local")
    resolved = _identify(legacy)
    assert (resolved.hostname, resolved.domain, resolved.tunnel_name) == ("dc-mcp.example.com", "", "dc-local")


@pytest.mark.parametrize("machine_name", ["ubuntu-pc", "laptop"])
def test_machine_name_never_moves_the_tunnel_of_a_fixed_hostname(machine_name: str) -> None:
    """The cutover layout: `install --machine-name ... --yes` would otherwise create a new tunnel, re-point the live record of the
    hostname to it and orphan `dc-local`. The same name again, or a new one, only names the connector."""
    cutover = Settings(hostname="dc-mcp.example.com", tunnel_name="dc-local", machine_name="ubuntu-pc")
    resolved = _identify(cutover, machine_name=machine_name)
    assert (resolved.hostname, resolved.tunnel_name, resolved.machine_name) == ("dc-mcp.example.com", "dc-local", machine_name)


def test_the_same_machine_name_again_keeps_an_adopted_tunnel() -> None:
    adopted = Settings(hostname="box-mcp.example.com", domain="example.com", machine_name="box", tunnel_name="old-tunnel")
    assert _identify(adopted, machine_name="box").tunnel_name == "old-tunnel"


def test_a_0_2_config_spelling_out_the_system_name_keeps_its_tunnel() -> None:
    """0.2.x saved no machine name; `--machine-name <the system name>` is the name it already had, not a rename."""
    old = Settings(hostname="dc.example.com", tunnel_name="computer-dc-example-com")
    spelled = _identify(old, domain="example.com", machine_name="test-box")
    implicit = _identify(old, domain="example.com")
    assert spelled.tunnel_name == implicit.tunnel_name == "computer-dc-example-com"


def test_a_too_long_derived_hostname_asks_for_a_shorter_machine_name() -> None:
    with pytest.raises(UsageError) as caught:
        public_hostname("a" * 59, ".".join(["b" * 60] * 4))
    assert "--machine-name" in caught.value.fix


def test_hostname_and_domain_together_are_refused() -> None:
    with pytest.raises(UsageError):
        _identify(Settings(), hostname="a.example.com", domain="example.com")


def test_without_cloudflare_or_a_domain_no_tunnel_is_named() -> None:
    resolved = identify(
        Settings(hostname="h.example.com"), hostname=None, domain=None, machine_name=None, tunnel_name=None, cloudflare=False
    )
    assert resolved.tunnel_name == ""

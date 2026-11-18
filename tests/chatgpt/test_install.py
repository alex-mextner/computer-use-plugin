"""The pinned release downloads of the chatgpt target (Desktop Commander is the shared core's, tested in tests/test_mcp.py)."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest
from agenttools_errors import AgentToolError, NetworkError

from computerlib.pins import BinaryPin, ZipMemberPin, binary_pin, pinned_machines, zip_member_pin
from computerlib.targets.chatgpt.install import ensure_binary, ensure_zip_member


def _pin(payload: bytes) -> BinaryPin:
    return BinaryPin("tool", "v1", "x86_64", hashlib.sha256(payload).hexdigest(), "https://example.invalid/tool")


def test_a_verified_download_is_installed_executable(tmp_path: Path) -> None:
    payload = b"#!/bin/sh\necho hi\n"
    result = ensure_binary(_pin(payload), tmp_path / "bin", fetch=lambda url: payload)
    assert result.changed
    assert result.path.read_bytes() == payload
    assert result.path.stat().st_mode & 0o111


def test_a_second_install_downloads_nothing(tmp_path: Path) -> None:
    payload = b"binary"
    pin = _pin(payload)
    ensure_binary(pin, tmp_path, fetch=lambda url: payload)

    def forbidden(url: str) -> bytes:
        raise AssertionError("downloaded again")

    assert not ensure_binary(pin, tmp_path, fetch=forbidden).changed


def test_a_download_with_the_wrong_digest_is_refused_and_the_installed_file_is_untouched(tmp_path: Path) -> None:
    good = b"good"
    pin = _pin(good)
    target = tmp_path / "bin"
    ensure_binary(pin, target, fetch=lambda url: good)
    other_pin = BinaryPin("tool", "v2", "x86_64", hashlib.sha256(b"v2").hexdigest(), "https://example.invalid/tool")
    with pytest.raises(AgentToolError) as caught:
        ensure_binary(other_pin, target, fetch=lambda url: b"tampered")
    assert "sha256 mismatch" in caught.value.what
    assert (target / "tool").read_bytes() == good
    assert [p.name for p in target.iterdir()] == ["tool"]


def test_a_failed_download_leaves_the_installed_file_untouched(tmp_path: Path) -> None:
    ensure_binary(_pin(b"old"), tmp_path, fetch=lambda url: b"old")
    newer = BinaryPin("tool", "v2", "x86_64", hashlib.sha256(b"new").hexdigest(), "https://example.invalid/tool")

    def offline(url: str) -> bytes:
        raise NetworkError(what="offline")

    with pytest.raises(NetworkError):
        ensure_binary(newer, tmp_path, fetch=offline)
    assert (tmp_path / "tool").read_bytes() == b"old"


def test_every_supported_architecture_has_a_pin_for_each_binary() -> None:
    for name in ("mcp-auth-proxy", "cloudflared"):
        for machine in ("x86_64", "aarch64"):
            pin = binary_pin(name, machine)
            assert pin is not None and len(pin.sha256) == 64 and pin.url.startswith("https://")
    assert binary_pin("mcp-auth-proxy", "riscv64") is None


def test_arm64_pins_are_the_published_arm64_assets_and_32_bit_arm_has_none() -> None:
    """aarch64 boards (an Odroid on a 64-bit OS) get the arm64 builds; armv7l has no mcp-auth-proxy build upstream, so no pin."""
    proxy, tunnel = binary_pin("mcp-auth-proxy", "aarch64"), binary_pin("cloudflared", "aarch64")
    assert proxy is not None and proxy.url.endswith("/mcp-auth-proxy-linux-arm64")
    assert tunnel is not None and tunnel.url.endswith("/cloudflared-linux-arm64")
    for name in ("mcp-auth-proxy", "cloudflared"):
        assert binary_pin(name, "armv7l") is None
        assert pinned_machines(name) == ("x86_64", "aarch64")


MEMBER = b"#!/bin/sh\necho tunnel-client\n"


def _zip(entries: dict[str, bytes], extra_names: tuple[str, ...] = ()) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, content in entries.items():
            archive.writestr(name, content)
        for name in extra_names:
            archive.writestr(name, b"extra")
    return buffer.getvalue()


def _zip_pin(archive: bytes, member: bytes = MEMBER) -> ZipMemberPin:
    return ZipMemberPin(
        "tunnel-client",
        "v1",
        "x86_64",
        hashlib.sha256(archive).hexdigest(),
        "tunnel-client",
        hashlib.sha256(member).hexdigest(),
        "https://example.invalid/tc.zip",
    )


def test_the_pinned_member_of_a_verified_zip_is_installed_executable_and_nothing_else_is(tmp_path: Path) -> None:
    archive = _zip({"tunnel-client": MEMBER, "cloudflared": b"bundled", "LICENSE": b"mit"})
    result = ensure_zip_member(_zip_pin(archive), tmp_path / "bin", fetch=lambda url: archive)
    assert result.changed
    assert result.path.read_bytes() == MEMBER
    assert result.path.stat().st_mode & 0o111
    assert [p.name for p in (tmp_path / "bin").iterdir()] == ["tunnel-client"]


def test_a_zip_with_the_wrong_digest_is_refused_before_it_is_opened_and_nothing_is_installed(tmp_path: Path) -> None:
    pin = _zip_pin(_zip({"tunnel-client": MEMBER}))
    tampered = _zip({"tunnel-client": b"evil"})
    with pytest.raises(AgentToolError) as caught:
        ensure_zip_member(pin, tmp_path / "bin", fetch=lambda url: tampered)
    assert "the zip" in caught.value.what and "sha256 mismatch" in caught.value.what
    assert not (tmp_path / "bin").exists()
    with pytest.raises(
        AgentToolError
    ):  # not even a zip: the digest is checked first, so this is a digest error and not a zip error
        ensure_zip_member(pin, tmp_path / "bin", fetch=lambda url: b"not a zip at all")
    assert not (tmp_path / "bin").exists()


def test_a_member_with_the_wrong_digest_is_refused_even_inside_a_zip_with_the_pinned_digest(tmp_path: Path) -> None:
    archive = _zip({"tunnel-client": b"swapped member"})
    pin = _zip_pin(archive, member=MEMBER)  # the zip digest matches, the member digest does not
    with pytest.raises(AgentToolError) as caught:
        ensure_zip_member(pin, tmp_path / "bin", fetch=lambda url: archive)
    assert "member" in caught.value.what and "sha256 mismatch" in caught.value.what
    assert not (tmp_path / "bin").exists()


def test_a_refused_zip_leaves_an_installed_member_untouched(tmp_path: Path) -> None:
    good = _zip({"tunnel-client": MEMBER})
    ensure_zip_member(_zip_pin(good), tmp_path / "bin", fetch=lambda url: good)
    newer = _zip_pin(_zip({"tunnel-client": b"v2"}), member=b"v2")
    with pytest.raises(AgentToolError):
        ensure_zip_member(newer, tmp_path / "bin", fetch=lambda url: _zip({"tunnel-client": b"tampered"}))
    assert (tmp_path / "bin" / "tunnel-client").read_bytes() == MEMBER
    assert [p.name for p in (tmp_path / "bin").iterdir()] == ["tunnel-client"]


def test_traversal_and_absolute_member_names_cannot_write_anywhere_and_do_not_stand_in_for_the_member(tmp_path: Path) -> None:
    bin_dir = tmp_path / "deep" / "bin"
    archive = _zip(
        {
            "tunnel-client": MEMBER,
            "../escaped": b"x",
            "/tmp/escaped-absolute": b"x",
            "sub/../../escaped2": b"x",
            "x/tunnel-client": b"y",
        }
    )
    result = ensure_zip_member(_zip_pin(archive), bin_dir, fetch=lambda url: archive)
    assert result.path.read_bytes() == MEMBER
    assert sorted(p.name for p in tmp_path.rglob("*") if p.is_file()) == ["tunnel-client"]
    assert not (tmp_path / "escaped").exists() and not (tmp_path / "deep" / "escaped").exists()


def test_a_zip_whose_only_candidate_has_a_traversal_name_has_no_member_and_is_refused(tmp_path: Path) -> None:
    archive = _zip({"../tunnel-client": MEMBER, "x/tunnel-client": MEMBER})
    with pytest.raises(AgentToolError) as caught:
        ensure_zip_member(_zip_pin(archive), tmp_path / "bin", fetch=lambda url: archive)
    assert "0 entries" in caught.value.what
    assert not (tmp_path / "bin").exists()


def test_two_entries_with_the_member_name_are_refused(tmp_path: Path) -> None:
    with pytest.warns(UserWarning, match="Duplicate name"):
        archive = _zip({"tunnel-client": MEMBER}, extra_names=("tunnel-client",))
    with pytest.raises(AgentToolError) as caught:
        ensure_zip_member(_zip_pin(archive), tmp_path / "bin", fetch=lambda url: archive)
    assert "2 entries" in caught.value.what
    assert not (tmp_path / "bin").exists()


def test_a_second_zip_install_downloads_nothing_and_changes_nothing(tmp_path: Path) -> None:
    archive = _zip({"tunnel-client": MEMBER})
    pin = _zip_pin(archive)
    first = ensure_zip_member(pin, tmp_path, fetch=lambda url: archive)
    stamp = first.path.stat().st_mtime_ns

    def forbidden(url: str) -> bytes:
        raise AssertionError("downloaded again")

    again = ensure_zip_member(pin, tmp_path, fetch=forbidden)
    assert not again.changed
    assert again.path.stat().st_mtime_ns == stamp


def test_a_damaged_member_is_replaced_from_a_verified_zip(tmp_path: Path) -> None:
    archive = _zip({"tunnel-client": MEMBER})
    pin = _zip_pin(archive)
    ensure_zip_member(pin, tmp_path, fetch=lambda url: archive)
    (tmp_path / "tunnel-client").write_bytes(b"corrupted")
    assert ensure_zip_member(pin, tmp_path, fetch=lambda url: archive).changed
    assert (tmp_path / "tunnel-client").read_bytes() == MEMBER


def test_the_tunnel_client_pin_covers_both_architectures_with_the_published_release_urls() -> None:
    for machine, arch in (("x86_64", "amd64"), ("aarch64", "arm64")):
        pin = zip_member_pin("tunnel-client", machine)
        assert pin is not None and len(pin.zip_sha256) == 64 and len(pin.member_sha256) == 64
        assert (
            pin.url == f"https://github.com/openai/tunnel-client/releases/download/v0.0.17/tunnel-client-v0.0.17-linux-{arch}.zip"
        )
        assert pin.member == "tunnel-client"
    assert zip_member_pin("tunnel-client", "riscv64") is None

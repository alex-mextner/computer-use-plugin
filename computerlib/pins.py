"""The pin table: every external thing `computer` installs is named here with an exact version, nowhere else.

Desktop Commander (npm): 0.3.2 is both the version that was already in the local npx cache when this pin was chosen and the latest
stable on npm. The ChatGPT target's release binaries (mcp-auth-proxy, cloudflared, OpenAI tunnel-client) carry the sha256 of their
release assets: nothing is fetched by "latest" and every download is verified before it can run.
HOW TO BUMP a binary: change version, url and digest together. Release digests are the `digest` field GitHub publishes per asset
(`gh api repos/<owner>/<repo>/releases/tags/<tag>`); the x86_64 ones were also compared with the binaries running on the author's
PC.
"""

from __future__ import annotations

import platform
from dataclasses import dataclass

DESKTOP_COMMANDER_PACKAGE = "@wonderwhy-er/desktop-commander"
DESKTOP_COMMANDER_VERSION = "0.3.2"


@dataclass(frozen=True)
class BinaryPin:
    name: str
    version: str
    machine: str
    sha256: str
    url: str


BINARY_PINS: tuple[BinaryPin, ...] = (
    BinaryPin(
        "mcp-auth-proxy",
        "v2.10.2",
        "x86_64",
        "1c76b230fdb6c536a0912c059232cba2229acbcf1f02a11a5d0664781326bcd6",
        "https://github.com/sigbit/mcp-auth-proxy/releases/download/v2.10.2/mcp-auth-proxy-linux-amd64",
    ),
    BinaryPin(
        "mcp-auth-proxy",
        "v2.10.2",
        "aarch64",
        "924c5335e5f684eb22c681cca1daff86d96fa35f644950cbd8f4c8f3e290bc3d",
        "https://github.com/sigbit/mcp-auth-proxy/releases/download/v2.10.2/mcp-auth-proxy-linux-arm64",
    ),
    BinaryPin(
        "cloudflared",
        "2026.10.7",
        "x86_64",
        "1f99fb518b83dec2ba8d870a7cc991d82a77e6196295a3d2754ba17ab9f91872",
        "https://github.com/cloudflare/cloudflared/releases/download/2026.10.7/cloudflared-linux-amd64",
    ),
    BinaryPin(
        "cloudflared",
        "2026.10.7",
        "aarch64",
        "3df43aedc9ff291d1735b5cb576cb97e0b157488796db2e32b28aecabb445775",
        "https://github.com/cloudflare/cloudflared/releases/download/2026.10.7/cloudflared-linux-arm64",
    ),
)


@dataclass(frozen=True)
class ZipMemberPin:
    """A release published as a zip: the zip is pinned, and so is the one member that gets installed (the zip also carries other
    files)."""

    name: str
    version: str
    machine: str
    zip_sha256: str
    member: str
    member_sha256: str
    url: str


_TUNNEL_CLIENT_URL = "https://github.com/openai/tunnel-client/releases/download/v0.0.17/tunnel-client-v0.0.17-linux-{arch}.zip"

# Both digests were recomputed from the official v0.0.17 release assets (gh release download + sha256sum, then the extracted
# member).
# The zip also holds a bundled cloudflared, LICENSE and NOTICE: only `tunnel-client` is installed, by exact member name.
ZIP_MEMBER_PINS: tuple[ZipMemberPin, ...] = (
    ZipMemberPin(
        "tunnel-client",
        "v0.0.17",
        "x86_64",
        "bd6a57c6c0511644267cafadd13ffdb53ece0d753e0b88857f957e78a8fbc793",
        "tunnel-client",
        "e163ff89cfc3c8bd9d196fe3fd622ba39d009af9935e38432790f39eb9b6db01",
        _TUNNEL_CLIENT_URL.format(arch="amd64"),
    ),
    ZipMemberPin(
        "tunnel-client",
        "v0.0.17",
        "aarch64",
        "f5060d0b63663268aab3ca310cb6110fa14867981db2b0657f679d8836a2daa0",
        "tunnel-client",
        "683afd34c71fca51670cf3d79d47abd5c717ecb3a52d721f3449c3ed41f36c60",
        _TUNNEL_CLIENT_URL.format(arch="arm64"),
    ),
)


def binary_pin(name: str, machine: str | None = None) -> BinaryPin | None:
    """The pin of `name` for this machine architecture, or None when no asset is pinned for it."""
    wanted = machine or platform.machine()
    for pin in BINARY_PINS:
        if pin.name == name and pin.machine == wanted:
            return pin
    return None


def zip_member_pin(name: str, machine: str | None = None) -> ZipMemberPin | None:
    """The zip-member pin of `name` for this machine architecture, or None when no asset is pinned for it."""
    wanted = machine or platform.machine()
    for pin in ZIP_MEMBER_PINS:
        if pin.name == name and pin.machine == wanted:
            return pin
    return None

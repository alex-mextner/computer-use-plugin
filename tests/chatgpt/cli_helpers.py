"""Run the real CLI in-process against the scratch HOME and fake machine of a test."""

from __future__ import annotations

import hashlib
import io
import json
import socket
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

import pytest

from computerlib import desktop_commander
from computerlib.cli import main
from computerlib.pins import DESKTOP_COMMANDER_PACKAGE, DESKTOP_COMMANDER_VERSION, BinaryPin, ZipMemberPin
from computerlib.targets.chatgpt import install
from computerlib.targets.chatgpt.commands import doctor as doctor_command
from computerlib.targets.chatgpt.commands import init as init_command
from computerlib.targets.chatgpt.settings import Layout, load_settings
from tests.fakes import write_executable

STUB = Path(__file__).with_name("stub_mcp.py")

FAKE_PROXY = b"#!/bin/sh\necho fake mcp-auth-proxy\n"
FAKE_TUNNEL_CLIENT = b"#!/bin/sh\necho fake tunnel-client\n"
TUNNEL_ID = "tunnel_abcdefghijklmnopqrstuvwxyz012345"
RUNTIME_KEY = "sk-test-only-runtime-key-0123456789"


def fake_release_zip(member: bytes = FAKE_TUNNEL_CLIENT) -> bytes:
    """A zip shaped like the real release: the member we install plus the bundled cloudflared and licence files."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("tunnel-client", member)
        archive.writestr("cloudflared", b"#!/bin/sh\necho bundled cloudflared\n")
        archive.writestr("LICENSE", b"license")
    return buffer.getvalue()


@dataclass
class Result:
    code: int
    out: str
    err: str


def run_cli(capsys: pytest.CaptureFixture[str], *args: str) -> Result:
    capsys.readouterr()
    code = main(["chatgpt", *args])
    captured = capsys.readouterr()
    return Result(code, captured.out, captured.err)


def stand_in_for_the_network(monkeypatch: pytest.MonkeyPatch, binaries: dict[str, bytes] | None = None) -> None:
    """Downloads return a fake binary whose digest the stand-in pin carries (the real pins are digests of real releases).
    `binaries` replaces the payload of a name (a working fake cloudflared, for example); the default is an inert script."""
    payloads = {"mcp-auth-proxy": FAKE_PROXY, "cloudflared": FAKE_PROXY, **(binaries or {})}

    def pin_for(name: str, machine: str | None = None) -> BinaryPin:
        digest = hashlib.sha256(payloads[name]).hexdigest()
        return BinaryPin(name, "vTEST", machine or "x86_64", digest, f"https://example.invalid/{name}")

    release = fake_release_zip()

    def zip_pin_for(name: str, machine: str | None = None) -> ZipMemberPin:
        return ZipMemberPin(
            name,
            "vTEST",
            machine or "x86_64",
            hashlib.sha256(release).hexdigest(),
            name,
            hashlib.sha256(FAKE_TUNNEL_CLIENT).hexdigest(),
            f"https://example.invalid/{name}.zip",
        )

    monkeypatch.setattr(init_command, "binary_pin", pin_for)
    monkeypatch.setattr(init_command, "zip_member_pin", zip_pin_for)
    monkeypatch.setattr(doctor_command, "binary_pin", pin_for)
    monkeypatch.setattr(doctor_command, "zip_member_pin", zip_pin_for)

    def download(url: str) -> bytes:
        return release if url.endswith(".zip") else payloads[url.rsplit("/", 1)[1]]

    monkeypatch.setattr(install, "urllib_fetch", download)


def free_port() -> int:
    """A loopback port nothing listens on (tests must never touch the real stack's ports)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


def init_args(**extra: str) -> list[str]:
    args = [
        "init",
        "--hostname",
        "dc.example.test",
        "--lang",
        "en",
        "--proxy-port",
        str(free_port()),
        "--shim-port",
        str(free_port()),
    ]
    for key, value in extra.items():
        args += [f"--{key.replace('_', '-')}", value]
    return args


def tunnel_init_args(**extra: str) -> list[str]:
    args = ["init", "--mode", "tunnel", "--tunnel-id", TUNNEL_ID, "--lang", "en", "--tunnel-health-port", str(free_port())]
    for key, value in extra.items():
        args += [f"--{key.replace('_', '-')}", value]
    return args


def layout() -> Layout:
    return Layout.of(load_settings())


def read_log(path: Path) -> list[str]:
    return path.read_text().splitlines() if path.exists() else []


def install_core_stand_in(node_dir: Path) -> Path:
    """A managed Desktop Commander install as `computer mcp install` leaves it, whose recorded `node` is the stub MCP server.
    `computer mcp serve` then execs the stub, so the chain runs the real core launcher end to end. Returns the stand-in node."""
    root = desktop_commander.install_dir()
    entry = root / "node_modules" / DESKTOP_COMMANDER_PACKAGE / "dist" / "index.js"
    entry.parent.mkdir(parents=True, exist_ok=True)
    entry.write_text("// stand-in: the recorded node below ignores it\n")
    node = write_executable(node_dir / "node", f'#!/bin/sh\nexec "{sys.executable}" "{STUB}"\n')
    record = {"package": DESKTOP_COMMANDER_PACKAGE, "version": DESKTOP_COMMANDER_VERSION, "node": str(node)}
    (root / "install.json").write_text(json.dumps(record))
    return node

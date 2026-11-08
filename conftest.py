"""Hermetic defaults for every test: scratch HOME/XDG, no credentials in the environment, no network."""

from __future__ import annotations

import socket
from collections.abc import Iterator
from pathlib import Path

import pytest

CREDENTIAL_ENV = ("PASSWORD", "CONTROL_PLANE_API_KEY", "OPENAI_API_KEY", "CLOUDFLARE_API_TOKEN", "TUNNEL_TOKEN")


@pytest.fixture(autouse=True)
def hermetic_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    for name in ("XDG_CONFIG_HOME", "XDG_STATE_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_RUNTIME_DIR"):
        monkeypatch.delenv(name, raising=False)
    for name in CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture(autouse=True)
def no_external_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Loopback only: a test that reaches the internet fails loudly instead of depending on it."""
    real_connect = socket.socket.connect

    def guarded(self: socket.socket, address: object) -> None:
        host = address[0] if isinstance(address, tuple) else address  # pyright: ignore[reportUnknownVariableType]
        if self.family in (socket.AF_INET, socket.AF_INET6) and host not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError(f"network blocked in unit tests: {host!r}")
        real_connect(self, address)  # pyright: ignore[reportArgumentType]

    monkeypatch.setattr(socket.socket, "connect", guarded)

from __future__ import annotations

from pathlib import Path
from typing import IO

import pytest

from computerlib.targets.chatgpt.settings import Layout, Settings
from computerlib.targets.chatgpt.stack import Process, stack
from computerlib.targets.chatgpt.units import exec_quote, managed_prefixes, render_unit, unit_name

LAUNCHER = ("/opt/tools/computer",)


def _units(settings: Settings) -> dict[str, str]:
    layout = Layout.of(settings)
    return {
        unit_name(settings.unit_prefix, p.name): render_unit(p, settings.unit_prefix) for p in stack(settings, layout, LAUNCHER)
    }


def test_the_stack_without_a_tunnel_is_bridge_and_shim_only() -> None:
    assert list(_units(Settings(hostname="h.example.test"))) == [
        "computer-chatgpt-bridge.service",
        "computer-chatgpt-shim.service",
    ]


def test_a_tunnel_name_adds_the_cloudflared_unit_last() -> None:
    units = _units(Settings(hostname="h.example.test", tunnel_name="dc-local"))
    assert list(units)[-1] == "computer-chatgpt-tunnel.service"
    assert "--url http://127.0.0.1:8767 dc-local" in units["computer-chatgpt-tunnel.service"]


def test_cloudflared_runs_from_this_targets_own_directory_never_from_dot_cloudflared() -> None:
    settings = Settings(hostname="h.example.test", tunnel_name="dc-local")
    own = Layout.of(settings).cloudflared_dir
    text = _units(settings)["computer-chatgpt-tunnel.service"]
    assert f"--config {own}/config.yml" in text
    assert f"--origincert {own}/cert.pem" in text
    assert f"--credentials-file {own}/dc-local.json" in text
    assert ".cloudflared" not in text


def test_the_bridge_runs_the_shared_core_desktop_commander_and_never_npx() -> None:
    bridge = _units(Settings(hostname="h.example.test"))["computer-chatgpt-bridge.service"]
    exec_line = next(line for line in bridge.splitlines() if line.startswith("ExecStart="))
    assert exec_line.endswith(" -- /opt/tools/computer mcp serve --terminal-only")
    assert "npx" not in bridge and "@latest" not in bridge


def test_a_custom_data_home_travels_into_the_bridge_unit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    bridge = _units(Settings(hostname="h.example.test"))["computer-chatgpt-bridge.service"]
    assert f'Environment="XDG_DATA_HOME={tmp_path / "data"}"' in bridge


def test_every_unit_keeps_retrying_for_ever_and_starts_at_boot() -> None:
    for text in _units(Settings(hostname="h.example.test", tunnel_name="t")).values():
        assert "Restart=always" in text
        assert "StartLimitIntervalSec=0" in text
        assert "WantedBy=default.target" in text


def test_units_are_ordered_bridge_then_shim_then_tunnel() -> None:
    units = _units(Settings(hostname="h.example.test", tunnel_name="t", unit_prefix="x"))
    assert "After=" not in units["x-bridge.service"]
    assert "After=x-bridge.service" in units["x-shim.service"]
    assert "After=x-shim.service" in units["x-tunnel.service"]


def test_the_password_reaches_the_bridge_only_through_its_env_file() -> None:
    settings = Settings(hostname="h.example.test")
    bridge = _units(settings)["computer-chatgpt-bridge.service"]
    assert f"EnvironmentFile={Layout.of(settings).password_file}" in bridge
    assert "--password" not in bridge


def test_the_bridge_listens_on_loopback_and_serves_the_configured_public_url() -> None:
    bridge = _units(Settings(hostname="h.example.test", proxy_port=9100))["computer-chatgpt-bridge.service"]
    assert "--listen 127.0.0.1:9100" in bridge
    assert "--external-url https://h.example.test" in bridge


def test_arguments_with_spaces_and_systemd_specials_are_quoted_and_escaped() -> None:
    assert exec_quote("/plain/path-1.2") == "/plain/path-1.2"
    assert exec_quote("a b") == '"a b"'
    assert exec_quote("100%") == '"100%%"'
    assert exec_quote("$HOME") == '"$$HOME"'
    assert exec_quote('say "hi"') == '"say \\"hi\\""'
    assert exec_quote("") == '""'


def test_a_process_environment_is_rendered_quoted() -> None:
    text = render_unit(Process("p", "d", ("/bin/true",), env=(("PATH", "/a b:/c"),)), "x")
    assert 'Environment="PATH=/a b:/c"' in text


def test_managed_prefixes_skips_a_unit_file_that_vanishes_while_it_is_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Between the directory listing and the read the file can be deleted: not ours to prove, so skipped, never raised."""
    ours = tmp_path / unit_name("kept", "shim")
    ours.write_text(render_unit(Process(name="shim", description="d", argv=("x",)), "kept"))
    gone = tmp_path / unit_name("gone", "shim")
    gone.write_text("x")
    real_open = Path.open

    def vanishing_open(self: Path, mode: str = "r") -> IO[bytes]:
        if self == gone:
            raise FileNotFoundError(self)
        return real_open(self, mode)  # pyright: ignore[reportReturnType]

    monkeypatch.setattr(Path, "open", vanishing_open)
    assert managed_prefixes(tmp_path, ["shim"]) == ["kept"]

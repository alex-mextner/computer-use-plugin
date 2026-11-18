from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from agenttools_errors import ConfigError

from computerlib.targets.chatgpt.settings import Layout, Settings, load_settings, parse_settings, render_settings, save_settings


def test_the_default_mode_is_public() -> None:
    assert Settings().mode == "public"
    assert load_settings().mode == "public"


def test_settings_survive_a_save_and_load() -> None:
    chosen = Settings(hostname="dc.example.test", tunnel_name="dc-local", proxy_port=9001, shim_port=9002, scheme="http")
    save_settings(chosen)
    assert load_settings() == chosen


def test_an_unknown_key_in_the_file_is_rejected_with_the_known_ones_named() -> None:
    with pytest.raises(ConfigError) as caught:
        parse_settings('mood = "happy"\n')
    assert "mood" in caught.value.what
    assert "hostname" in caught.value.why


def test_a_value_of_the_wrong_type_is_rejected() -> None:
    with pytest.raises(ConfigError):
        parse_settings('proxy_port = "8766"\n')


def test_an_unknown_mode_is_rejected() -> None:
    with pytest.raises(ConfigError):
        parse_settings('mode = "carrier-pigeon"\n')


def test_the_proxy_and_the_shim_cannot_share_a_port() -> None:
    with pytest.raises(ConfigError):
        parse_settings("proxy_port = 9000\nshim_port = 9000\n")


def test_rendered_settings_with_quotes_parse_back_unchanged() -> None:
    odd = Settings(hostname='we"ird\\host')
    assert parse_settings(render_settings(odd)) == odd


def test_a_path_with_control_and_non_bmp_characters_survives_a_save_and_load() -> None:
    odd = Settings(state_dir="/tmp/del\x7f-esc\x1b-tab\t-nl\n-emoji\U0001f600")
    path = save_settings(odd)
    assert tomllib.loads(path.read_text(encoding="utf-8"))["state_dir"] == odd.state_dir
    assert load_settings() == odd


def test_the_layout_follows_xdg_variables_and_falls_back_to_home(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    home = tmp_path / "home"
    layout = Layout.of(Settings())
    assert layout.state_dir == home / ".local" / "state" / "computer" / "chatgpt"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg-state"))
    assert Layout.of(Settings()).state_dir == tmp_path / "xdg-state" / "computer" / "chatgpt"


def test_path_overrides_win_so_an_existing_install_can_be_adopted(tmp_path: Path) -> None:
    layout = Layout.of(Settings(state_dir=str(tmp_path / "old-state"), data_dir=str(tmp_path / "old-oauth")))
    assert layout.access_log == tmp_path / "old-state" / "access.log"
    assert layout.data_dir == tmp_path / "old-oauth"

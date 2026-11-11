from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

from computerlib.cli import main
from tests.fakes import fake_computer, fake_node, fake_npm, write_executable

CLAUDE_BIN = "/usr/lib/claude-desktop/claude-desktop"


@pytest.fixture
def home(tmp_path: Path) -> Path:
    return tmp_path / "home"


@pytest.fixture
def config_path(home: Path) -> Path:
    return home / ".config/Claude/claude_desktop_config.json"


@pytest.fixture
def autostart_path(home: Path) -> Path:
    return home / ".config/autostart/claude-desktop.desktop"


@pytest.fixture
def computer_bin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`computer` as an installed command (fake node/npm for the core install, stub server as the executable)."""
    bin_dir = tmp_path / "bin"
    fake_node(bin_dir)
    fake_npm(bin_dir)
    computer = fake_computer(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr(sys, "argv", [str(computer), "claude"])
    return computer


@pytest.fixture
def claude_binary(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    binary = write_executable(tmp_path / "usr/lib/claude-desktop/claude-desktop", "#!/bin/sh\n")
    monkeypatch.setattr("computerlib.targets.claude.autostart.CLAUDE_DESKTOP_BINARIES", (str(binary),))
    return binary


def seed_config(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def entry(config_path: Path) -> object:
    return json.loads(config_path.read_text())["mcpServers"]["desktop-commander"]


def test_install_creates_config_entry_with_absolute_command(computer_bin: Path, claude_binary: Path, config_path: Path) -> None:
    assert main(["claude", "install"]) == 0
    assert entry(config_path) == {"command": str(computer_bin), "args": ["mcp", "serve"]}


def test_install_preserves_every_other_key(computer_bin: Path, claude_binary: Path, config_path: Path) -> None:
    other = {"globalShortcut": "Ctrl+Space", "preferences": {"a": [1, 2, {"b": None}]}, "mcpServers": {"other": {"command": "x"}}}
    seed_config(config_path, other)
    assert main(["claude", "install"]) == 0
    result = json.loads(config_path.read_text())
    assert result["globalShortcut"] == "Ctrl+Space"
    assert result["preferences"] == other["preferences"]
    assert result["mcpServers"]["other"] == {"command": "x"}


def test_install_is_idempotent_and_backs_up_only_real_changes(computer_bin: Path, claude_binary: Path, config_path: Path) -> None:
    seed_config(config_path, {"keep": 1})
    assert main(["claude", "install"]) == 0
    backups = list(config_path.parent.glob("claude_desktop_config.json.bak-*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == {"keep": 1}
    before = config_path.read_text()
    assert main(["claude", "install"]) == 0
    assert config_path.read_text() == before
    assert len(list(config_path.parent.glob("claude_desktop_config.json.bak-*"))) == 1


def test_install_rewrites_a_stale_entry(computer_bin: Path, claude_binary: Path, config_path: Path) -> None:
    seed_config(config_path, {"mcpServers": {"desktop-commander": {"command": "/opt/linuxbrew/bin/npx", "args": ["-y", "x"]}}})
    assert main(["claude", "install"]) == 0
    assert entry(config_path) == {"command": str(computer_bin), "args": ["mcp", "serve"]}


def test_install_refuses_invalid_json_and_leaves_file_untouched(
    computer_bin: Path, claude_binary: Path, config_path: Path
) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{ not json")
    assert main(["claude", "install"]) == 2
    assert config_path.read_text() == "{ not json"
    assert not list(config_path.parent.glob("*.bak-*"))


def test_install_refuses_non_object_servers(computer_bin: Path, claude_binary: Path, config_path: Path) -> None:
    seed_config(config_path, {"mcpServers": []})
    assert main(["claude", "install"]) == 2


def test_install_runs_the_core_install(computer_bin: Path, claude_binary: Path, home: Path) -> None:
    assert main(["claude", "install"]) == 0
    assert (home / ".local/share/computer/desktop-commander/install.json").is_file()


def test_install_keeps_file_mode(computer_bin: Path, claude_binary: Path, config_path: Path) -> None:
    seed_config(config_path, {})
    config_path.chmod(0o600)
    assert main(["claude", "install"]) == 0
    assert config_path.stat().st_mode & 0o777 == 0o600


def test_install_creates_missing_autostart(computer_bin: Path, claude_binary: Path, autostart_path: Path) -> None:
    assert main(["claude", "install"]) == 0
    text = autostart_path.read_text()
    assert f'Exec="{claude_binary}" --startup' in text
    assert "X-GNOME-Autostart-enabled=true" in text


def test_install_leaves_enabled_autostart_alone(computer_bin: Path, claude_binary: Path, autostart_path: Path) -> None:
    autostart_path.parent.mkdir(parents=True)
    original = '[Desktop Entry]\nType=Application\nExec="/somewhere/else" --startup\nX-GNOME-Autostart-enabled=true\n'
    autostart_path.write_text(original)
    assert main(["claude", "install"]) == 0
    assert autostart_path.read_text() == original


def test_install_reenables_disabled_autostart(computer_bin: Path, claude_binary: Path, autostart_path: Path) -> None:
    autostart_path.parent.mkdir(parents=True)
    autostart_path.write_text(
        '[Desktop Entry]\nType=Application\nExec="/x" --startup\nHidden=true\nX-GNOME-Autostart-enabled=false\n'
    )
    assert main(["claude", "install"]) == 0
    text = autostart_path.read_text()
    assert "X-GNOME-Autostart-enabled=true" in text
    assert "Hidden=true" not in text
    assert 'Exec="/x" --startup' in text


def test_install_tells_the_user_to_restart_claude_desktop(
    computer_bin: Path, claude_binary: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["claude", "install"]) == 0
    assert "restart" in capsys.readouterr().out.lower()


def test_uninstall_removes_only_our_entry(computer_bin: Path, claude_binary: Path, config_path: Path) -> None:
    seed_config(config_path, {"keep": 1, "mcpServers": {"desktop-commander": {"command": "x"}, "other": {"command": "y"}}})
    assert main(["claude", "uninstall"]) == 0
    assert json.loads(config_path.read_text()) == {"keep": 1, "mcpServers": {"other": {"command": "y"}}}
    assert len(list(config_path.parent.glob("*.bak-*"))) == 1


def test_uninstall_without_config_or_entry_is_a_noop(computer_bin: Path, config_path: Path) -> None:
    assert main(["claude", "uninstall"]) == 0
    seed_config(config_path, {"mcpServers": {}})
    before = config_path.read_text()
    assert main(["claude", "uninstall"]) == 0
    assert config_path.read_text() == before


def test_uninstall_leaves_managed_install_and_autostart(
    computer_bin: Path, claude_binary: Path, home: Path, autostart_path: Path
) -> None:
    assert main(["claude", "install"]) == 0
    assert main(["claude", "uninstall"]) == 0
    assert (home / ".local/share/computer/desktop-commander/install.json").is_file()
    assert autostart_path.is_file()


def test_status_healthy_after_install(computer_bin: Path, claude_binary: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["claude", "install"]) == 0
    capsys.readouterr()
    assert main(["claude", "status"]) == 0
    out = capsys.readouterr().out
    assert "3 tools" in out
    assert "start_process" in out


def test_status_unhealthy_without_config(computer_bin: Path, claude_binary: Path) -> None:
    assert main(["claude", "status"]) == 3


def test_status_unhealthy_when_command_is_missing(
    computer_bin: Path, claude_binary: Path, config_path: Path, autostart_path: Path
) -> None:
    seed_config(
        config_path, {"mcpServers": {"desktop-commander": {"command": "/nonexistent/computer", "args": ["mcp", "serve"]}}}
    )
    assert main(["claude", "status"]) == 3


def test_status_unhealthy_when_server_lacks_dc_tools(
    tmp_path: Path, computer_bin: Path, claude_binary: Path, config_path: Path
) -> None:
    empty = fake_computer(tmp_path / "empty", "--no-tools")
    seed_config(config_path, {"mcpServers": {"desktop-commander": {"command": str(empty), "args": ["mcp", "serve"]}}})
    assert main(["claude", "status"]) == 3


def test_status_unhealthy_when_handshake_times_out(
    tmp_path: Path, computer_bin: Path, claude_binary: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hang = fake_computer(tmp_path / "hang", "--hang")
    seed_config(config_path, {"mcpServers": {"desktop-commander": {"command": str(hang), "args": ["mcp", "serve"]}}})
    monkeypatch.setattr("computerlib.targets.claude.commands.status.HANDSHAKE_TIMEOUT_SECONDS", 1.0)
    assert main(["claude", "status"]) == 3


def test_status_unhealthy_when_autostart_disabled(
    computer_bin: Path, claude_binary: Path, autostart_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert main(["claude", "install"]) == 0
    autostart_path.write_text(autostart_path.read_text().replace("enabled=true", "enabled=false"))
    capsys.readouterr()
    assert main(["claude", "status"]) == 3
    assert "autostart" in capsys.readouterr().out


def test_status_never_writes_the_config(computer_bin: Path, claude_binary: Path, config_path: Path) -> None:
    seed_config(config_path, {"keep": 1})
    before = config_path.read_text()
    assert main(["claude", "status"]) == 3
    assert config_path.read_text() == before
    assert not list(config_path.parent.glob("*.bak-*"))


@pytest.fixture
def real_computer(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """`computer` as the real CLI (so status runs the real `mcp serve`), with fake node/npm for the install."""
    bin_dir = tmp_path / "bin"
    fake_node(bin_dir)
    fake_npm(bin_dir)
    computer = write_executable(bin_dir / "computer", f'#!/bin/sh\nexec "{sys.executable}" -m computerlib.cli "$@"\n')
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr(sys, "argv", [str(computer), "claude"])
    return computer


def test_status_runs_the_real_serve_end_to_end(real_computer: Path, claude_binary: Path) -> None:
    assert main(["claude", "install"]) == 0
    assert main(["claude", "status"]) == 0


def test_custom_data_home_travels_in_the_entry_so_login_finds_the_install(
    real_computer: Path, claude_binary: Path, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xdg = tmp_path / "xdg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    assert main(["claude", "install"]) == 0
    assert entry(config_path) == {"command": str(real_computer), "args": ["mcp", "serve"], "env": {"XDG_DATA_HOME": str(xdg)}}
    # The login session has no XDG_DATA_HOME; the entry's env must be enough.
    monkeypatch.delenv("XDG_DATA_HOME")
    assert main(["claude", "status"]) == 0


def test_status_does_not_leak_the_callers_data_home_into_the_probe(
    real_computer: Path, claude_binary: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert main(["claude", "install"]) == 0
    monkeypatch.delenv("XDG_DATA_HOME")
    assert main(["claude", "uninstall"]) == 0
    seed_config(
        tmp_path / "home/.config/Claude/claude_desktop_config.json",
        {"mcpServers": {"desktop-commander": {"command": str(real_computer), "args": ["mcp", "serve"]}}},
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert main(["claude", "status"]) == 3


def test_install_without_claude_launcher_writes_config_then_fails_with_5(
    computer_bin: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("computerlib.targets.claude.autostart.find_binary", lambda: None)
    assert main(["claude", "install"]) == 5
    assert entry(config_path) == {"command": str(computer_bin), "args": ["mcp", "serve"]}


def test_install_keeps_changes_claude_wrote_while_npm_was_running(
    tmp_path: Path, claude_binary: Path, config_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bin_dir = tmp_path / "bin"
    fake_node(bin_dir)
    fake_npm(
        bin_dir, side_effect=f"mkdir -p '{config_path.parent}'; echo '{{\"preferences\": {{\"late\": true}}}}' > '{config_path}'"
    )
    computer = fake_computer(bin_dir)
    monkeypatch.setenv("PATH", f"{bin_dir}:/usr/bin:/bin")
    monkeypatch.setattr(sys, "argv", [str(computer), "claude"])
    assert main(["claude", "install"]) == 0
    assert json.loads(config_path.read_text())["preferences"] == {"late": True}


def test_status_reports_an_unreadable_config_instead_of_crashing(
    computer_bin: Path, claude_binary: Path, config_path: Path
) -> None:
    config_path.mkdir(parents=True)
    assert main(["claude", "status"]) == 3


def test_status_reports_a_non_utf8_autostart_file_instead_of_crashing(
    computer_bin: Path, claude_binary: Path, autostart_path: Path
) -> None:
    autostart_path.parent.mkdir(parents=True)
    autostart_path.write_bytes(b"[Desktop Entry]\nName=Claude \xe9\nX-GNOME-Autostart-enabled=true\n")
    assert main(["claude", "install"]) == 0
    assert main(["claude", "status"]) == 0


def test_install_keeps_user_keys_in_our_entry_and_other_env_vars(
    computer_bin: Path, claude_binary: Path, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_config(
        config_path,
        {"mcpServers": {"desktop-commander": {"command": "old", "args": [], "disabled": False, "env": {"FOO": "1"}}}},
    )
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert main(["claude", "install"]) == 0
    assert entry(config_path) == {
        "command": str(computer_bin),
        "args": ["mcp", "serve"],
        "disabled": False,
        "env": {"FOO": "1", "XDG_DATA_HOME": str(tmp_path / "xdg")},
    }
    monkeypatch.delenv("XDG_DATA_HOME")
    assert main(["claude", "install"]) == 0
    assert entry(config_path) == {"command": str(computer_bin), "args": ["mcp", "serve"], "disabled": False, "env": {"FOO": "1"}}


def test_install_drops_an_env_that_only_held_our_variable(
    computer_bin: Path, claude_binary: Path, config_path: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "xdg"))
    assert main(["claude", "install"]) == 0
    monkeypatch.delenv("XDG_DATA_HOME")
    assert main(["claude", "install"]) == 0
    assert entry(config_path) == {"command": str(computer_bin), "args": ["mcp", "serve"]}


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_status_reports_an_unreadable_autostart_file_instead_of_crashing(
    computer_bin: Path, claude_binary: Path, autostart_path: Path
) -> None:
    assert main(["claude", "install"]) == 0
    autostart_path.chmod(0)
    try:
        assert main(["claude", "status"]) == 3
    finally:
        autostart_path.chmod(0o644)


def test_status_shows_why_the_server_failed(
    computer_bin: Path, claude_binary: Path, config_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed_config(config_path, {"mcpServers": {"desktop-commander": {"command": str(computer_bin), "args": ["--crash"]}}})
    assert main(["claude", "status"]) == 3
    assert "boom from the server" in capsys.readouterr().out


def test_install_reenables_only_the_main_section_of_a_disabled_autostart(
    computer_bin: Path, claude_binary: Path, autostart_path: Path
) -> None:
    autostart_path.parent.mkdir(parents=True)
    autostart_path.write_text(
        "[Desktop Entry]\nType=Application\nHidden=true\n\n[Desktop Action x]\nName=x\nX-GNOME-Autostart-enabled=false\n"
    )
    assert main(["claude", "install"]) == 0
    text = autostart_path.read_text()
    assert text.index("X-GNOME-Autostart-enabled=true") < text.index("[Desktop Action x]")
    assert "Hidden=true" not in text
    assert "X-GNOME-Autostart-enabled=false" in text


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores file modes")
def test_install_turns_an_unwritable_config_directory_into_exit_8(
    computer_bin: Path, claude_binary: Path, config_path: Path
) -> None:
    config_path.parent.mkdir(parents=True)
    config_path.parent.chmod(0o500)
    try:
        assert main(["claude", "install"]) == 8
    finally:
        config_path.parent.chmod(0o755)


def test_install_reenables_a_numeric_disabled_autostart_without_mangling_other_bytes(
    computer_bin: Path, claude_binary: Path, autostart_path: Path
) -> None:
    autostart_path.parent.mkdir(parents=True)
    autostart_path.write_bytes(b"[Desktop Entry]\nName=Claude \xe9\nX-GNOME-Autostart-enabled=0\n")
    assert main(["claude", "install"]) == 0
    assert autostart_path.read_bytes() == b"[Desktop Entry]\nName=Claude \xe9\nX-GNOME-Autostart-enabled=true\n"

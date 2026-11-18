"""`stop`, `end`, `end --revoke`, `uninstall`: access must really be off, and a command that could not do it must say so and fail.

Regression targets: systemctl failures were swallowed (exit 0, "stopped and disabled"), `--revoke` ignored every deletion error
(a symlinked data dir stayed intact and was reported revoked), and a password file that already existed with mode 0644 kept it.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest
from agenttools_errors import AgentToolError

from computerlib.targets.chatgpt.password import ensure_password, read_password
from computerlib.targets.chatgpt.removal import remove_tree
from tests.chatgpt.cli_helpers import init_args, layout, run_cli, stand_in_for_the_network
from tests.chatgpt.conftest import FakeMachine

BRIDGE = "computer-chatgpt-bridge.service"
SHIM = "computer-chatgpt-shim.service"


def _running_stack(capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch) -> Path:
    stand_in_for_the_network(monkeypatch)
    run_cli(capsys, *init_args())
    data = layout().data_dir
    data.mkdir(parents=True)
    (data / "db").write_text("clients")
    assert run_cli(capsys, "start", "--no-wait").code == 0
    return data


@pytest.mark.parametrize("command", ["stop", "end", "uninstall"])
def test_a_failing_systemctl_is_a_failure_not_a_success(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch, command: str
) -> None:
    _running_stack(capsys, monkeypatch)
    monkeypatch.setenv("FAKE_SYSTEMD_FAIL", "stop,disable")
    result = run_cli(capsys, command)
    assert result.code == 1
    assert "still on" in result.err and SHIM in result.err and "Access denied" in result.err
    assert "stopped" not in result.out and "removed units" not in result.out
    assert machine.unit_state(SHIM, "active") == "active"


def test_a_failed_end_never_goes_on_to_revoke(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _running_stack(capsys, monkeypatch)
    monkeypatch.setenv("FAKE_SYSTEMD_FAIL", "disable")
    result = run_cli(capsys, "end", "--revoke")
    assert result.code == 1 and "revoked" not in result.out
    assert (data / "db").exists()


def test_an_unreachable_manager_is_not_mistaken_for_stopped_units(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    _running_stack(capsys, monkeypatch)
    monkeypatch.setenv("FAKE_SYSTEMD_FAIL", "stop,disable,show")
    result = run_cli(capsys, "end")
    assert result.code == 1 and "unknown" in result.err


def test_units_that_are_already_off_make_a_failing_systemctl_harmless(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    stand_in_for_the_network(monkeypatch)
    run_cli(capsys, *init_args())
    monkeypatch.setenv("FAKE_SYSTEMD_FAIL", "stop,disable")
    # `show` still works and reports the never-started units as inactive and disabled, so nothing is left on
    assert run_cli(capsys, "end").code == 0


def test_revoke_deletes_the_oauth_state_and_the_client_table(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _running_stack(capsys, monkeypatch)
    clients = layout().clients_file
    clients.parent.mkdir(parents=True, exist_ok=True)
    clients.write_text("{}")
    result = run_cli(capsys, "end", "--revoke")
    assert result.code == 0 and "revoked" in result.out
    assert not data.exists() and not clients.exists()


def test_revoke_refuses_a_symlinked_data_dir_and_deletes_nothing(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    data = _running_stack(capsys, monkeypatch)
    real = tmp_path / "elsewhere"
    data.rename(real)
    data.symlink_to(real)
    result = run_cli(capsys, "end", "--revoke")
    assert result.code == 1
    assert "symbolic link" in result.err and "revoked" not in result.out
    assert (real / "db").read_text() == "clients"
    assert data.is_symlink()


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores directory permissions")
def test_revoke_that_cannot_delete_fails_with_the_permission_exit_code(
    capsys: pytest.CaptureFixture[str], machine: FakeMachine, monkeypatch: pytest.MonkeyPatch
) -> None:
    data = _running_stack(capsys, monkeypatch)
    data.chmod(0o500)  # entries cannot be unlinked
    try:
        result = run_cli(capsys, "end", "--revoke")
    finally:
        data.chmod(0o700)
    assert result.code == 8
    assert "could not remove" in result.err and "revoked" not in result.out
    assert (data / "db").exists()


def test_remove_tree_never_removes_home_root_or_a_parent_of_home(tmp_path: Path) -> None:
    home = Path(os.environ["HOME"])
    for unsafe in (home, home.parent, Path("/")):
        with pytest.raises(AgentToolError, match="refusing to remove"):
            remove_tree(unsafe)
    assert home.exists()


def test_remove_tree_reports_whether_anything_was_there(tmp_path: Path) -> None:
    (tmp_path / "d" / "sub").mkdir(parents=True)
    assert remove_tree(tmp_path / "d") is True
    assert remove_tree(tmp_path / "d") is False


def test_an_existing_world_readable_env_file_becomes_private_before_the_password_is_written(tmp_path: Path) -> None:
    path = tmp_path / "auth.env"
    path.write_text("OTHER=1\n")
    path.chmod(0o644)
    assert ensure_password(path) is True
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert read_password(path)
